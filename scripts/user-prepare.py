#!/usr/bin/env python3
"""以专用用户准备服务目录和 rootless Docker（方案 0004「管理员准备与用户初始化」）。

默认按顺序核对身份、目录、Docker 配置、用户会话、官方前提检查和 Docker 用户服务，列出缺失项。
--apply 在全部检查通过后依次：合并写入 daemon.json 的 log-driver=local（经 dockerd --validate 校验后原子替换），
创建服务目录内部的 code/、instance/{settings,local,state}/、backup/，按需安装、启用或重启 Docker 用户服务，
最后经该用户自己的 socket 复查 rootless、local、systemd 与 cgroup v2。
"""

from __future__ import annotations

import argparse
from collections.abc import Callable
import grp
import json
import os
from pathlib import Path
import pwd
import re
import shutil
import stat
import subprocess
import tempfile


class PreparationError(Exception):
    pass


Reason = Callable[[subprocess.CompletedProcess[str]], str]


def run(*args: str, required: bool = True, reason: Reason | None = None) -> subprocess.CompletedProcess[str]:
    # 每次明确指定自己的 socket，不受调用环境的其他 Docker context/TLS 设置影响。
    env = os.environ.copy()
    for key in ("DOCKER_HOST", "DOCKER_CONTEXT", "DOCKER_TLS_VERIFY", "DOCKER_CERT_PATH"):
        env.pop(key, None)
    result = subprocess.run(args, env=env, text=True, capture_output=True)
    if required and result.returncode:
        # 原始输出可能含配置值或环境变量；只输出该子命令对应的 reason 提炼出的安全原因。
        detail = reason(result) if reason else ""
        raise PreparationError(f"{' '.join(args[:2])} 失败（退出码 {result.returncode}）" + (f"：{detail}" if detail else ""))
    return result


def setuptool_reason(result: subprocess.CompletedProcess[str]) -> str:
    # 官方前提检查的 [ERROR]/[WARNING] 行只描述缺失的前提和建议，不含配置值。
    lines = [line.strip() for line in (result.stdout + "\n" + result.stderr).splitlines()
             if line.lstrip().startswith(("[ERROR]", "[WARNING]"))]
    return "；".join(lines[-5:]) or "请在该用户会话中运行 dockerd-rootless-setuptool.sh check --force 查看"


# dockerd 配置校验的错误只提取字段名和类型；可能包含配置值（例如带凭据的代理地址）的内容一律不输出。
VALIDATE_REASONS = [
    re.compile(r"cannot unmarshal \w+ into Go struct field [\w.-]+ of type [\w.\[\]]+"),
    re.compile(r"the following directives [a-z' ]+: [\w, .-]+"),
    re.compile(r"unknown log opt '[\w.-]+' for [\w-]+ log driver"),
]


def validate_reason(result: subprocess.CompletedProcess[str]) -> str:
    for pattern in VALIDATE_REASONS:
        match = pattern.search(result.stderr)
        if match:
            return match.group(0)
    return "无法归类的配置错误；请在该用户会话中运行 dockerd --validate --config-file <文件> 查看"


def service_reason(_: subprocess.CompletedProcess[str]) -> str:
    # 只取 systemd 的状态字段，不取包含环境和参数的其他属性。
    fields = ("ActiveState", "SubState", "Result", "ExecMainStatus")
    shown = run("systemctl", "--user", "show", "docker.service", *(f"--property={name}" for name in fields),
                required=False).stdout.splitlines()
    state = ", ".join(line for line in shown if line.partition("=")[0] in fields)
    return f"{state}；详细日志：journalctl --user -u docker.service -n 50"


def docker_reason(result: subprocess.CompletedProcess[str]) -> str:
    if "Cannot connect to the Docker daemon" in result.stderr:
        return "无法连接该用户的 Docker socket"
    if "permission denied" in result.stderr.lower():
        return "无权访问该用户的 Docker socket"
    return "请在该用户会话中运行 docker info 查看"


def session_reason(_: subprocess.CompletedProcess[str]) -> str:
    # show-environment 的输出是完整环境变量，任何情况下都不输出。
    return "无法连接用户级 systemd；请通过 SSH 或 machinectl shell 登录该用户"


def identity(user: str) -> pwd.struct_passwd:
    if os.geteuid() == 0:
        raise PreparationError("用户初始化不能以 root 运行")
    try:
        account = pwd.getpwnam(user)
    except KeyError as exc:
        raise PreparationError("专用用户不存在；需先完成管理员准备") from exc
    if account.pw_uid != os.geteuid():
        raise PreparationError(f"请登录专用用户 {user} 后运行")
    names = {grp.getgrgid(gid).gr_name for gid in os.getgroups()}
    if names & {"sudo", "docker"}:
        raise PreparationError("专用用户不能属于 sudo 或 docker 组")
    if Path(os.environ.get("HOME", "")).resolve() != Path(account.pw_dir).resolve():
        raise PreparationError("HOME 与专用用户家目录不一致；请使用该用户的登录会话")
    return account


def directory(path: Path, uid: int) -> bool:
    """返回目录是否存在；存在时仅接受用户持有、可写的真实目录。"""
    if path.is_symlink():
        raise PreparationError(f"目录不能是符号链接：{path}")
    if path.exists():
        if not path.is_dir() or path.stat().st_uid != uid or not os.access(path, os.W_OK | os.X_OK):
            raise PreparationError(f"目录须归当前用户所有且可写：{path}")
        return True
    return False


def inspect_directories(root: Path, uid: int) -> list[Path]:
    paths = [root, root / "code", root / "instance", root / "instance/settings",
             root / "instance/local", root / "instance/state", root / "backup"]
    for path in paths:
        directory(path, uid)
    if not root.exists():
        parent = root.parent
        while not parent.exists() and not parent.is_symlink():
            parent = parent.parent
        if not directory(parent, uid):
            raise PreparationError("服务目录的现有父目录须归当前用户所有")
    return paths


def config_path(account: pwd.struct_passwd) -> Path:
    base = Path(os.environ.get("XDG_CONFIG_HOME") or str(Path(account.pw_dir) / ".config"))
    if not base.is_absolute():
        raise PreparationError("XDG_CONFIG_HOME 须为绝对路径")
    return base / "docker/daemon.json"


def read_config(path: Path, uid: int) -> dict:
    parent = path.parent
    while not parent.exists() and not parent.is_symlink():
        parent = parent.parent
    directory(parent, uid)
    if path.is_symlink() or (path.exists() and (not path.is_file() or path.stat().st_uid != uid)):
        raise PreparationError("daemon.json 须为当前用户持有的普通文件，不能是符号链接")
    if not path.exists():
        return {}
    try:
        value = json.loads(path.read_text())
    except (ValueError, OSError) as exc:
        raise PreparationError("daemon.json 读取或 JSON 解析失败，未修改文件") from exc
    if not isinstance(value, dict):
        raise PreparationError("daemon.json 顶层须为对象")
    return value


def write_config(path: Path, value: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    mode = stat.S_IMODE(path.stat().st_mode) if path.exists() else 0o600
    fd, name = tempfile.mkstemp(prefix=".daemon-", suffix=".json", dir=path.parent)
    candidate = Path(name)
    try:
        with os.fdopen(fd, "w") as stream:
            json.dump(value, stream, indent=2)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.chmod(candidate, mode)
        run("dockerd", "--validate", "--config-file", str(candidate), reason=validate_reason)
        os.replace(candidate, path)
    finally:
        candidate.unlink(missing_ok=True)


def docker_info(socket: str) -> dict:
    result = run("docker", "--host", socket, "info", "--format", "{{json .}}", reason=docker_reason)
    try:
        info = json.loads(result.stdout)
    except ValueError as exc:
        raise PreparationError("Docker 未返回有效的检查结果") from exc
    security = info.get("SecurityOptions", [])
    if not any(item == "rootless" or item.startswith("name=rootless") for item in security):
        raise PreparationError("指定 socket 的 Docker 未报告 rootless，已停止")
    if info.get("CgroupDriver") != "systemd" or str(info.get("CgroupVersion")) != "2":
        raise PreparationError("rootless Docker 尚未使用 systemd / cgroup v2")
    return info


def prepare(user: str, root: Path, apply: bool) -> int:
    account = identity(user)
    uid = account.pw_uid
    if not root.is_absolute():
        raise PreparationError("服务目录须为绝对路径")
    root = Path(os.path.abspath(root))
    paths = inspect_directories(root, uid)
    cfg_path = config_path(account)
    before = read_config(cfg_path, uid)
    desired = dict(before, **{"log-driver": "local"})
    for command in ("docker", "dockerd", "dockerd-rootless-setuptool.sh", "systemctl"):
        if shutil.which(command) is None:
            raise PreparationError(f"缺少 {command}；需管理员完成主机前提")
    runtime = os.environ.get("XDG_RUNTIME_DIR", "")
    if not runtime or not Path(runtime).is_absolute() or not directory(Path(runtime), uid):
        raise PreparationError("缺少有效的 XDG_RUNTIME_DIR；请通过 SSH 或 machinectl 登录专用用户")
    run("systemctl", "--user", "show-environment", reason=session_reason)
    # 在已有运行时目录和用户会话中执行官方前提检查，结果只用于判断。
    run("dockerd-rootless-setuptool.sh", "check", "--force", reason=setuptool_reason)
    socket = f"unix://{runtime}/docker.sock"
    load = run("systemctl", "--user", "show", "docker.service", "-p", "LoadState", "--value",
               reason=session_reason).stdout.strip()
    if load not in {"loaded", "not-found"}:
        raise PreparationError("docker 用户服务状态不兼容，需人工检查")
    active_result = run("systemctl", "--user", "is-active", "docker.service", required=False)
    if active_result.returncode not in {0, 3, 4}:
        raise PreparationError("无法核实 Docker 用户服务状态")
    active = active_result.returncode == 0
    info = docker_info(socket) if active else None
    enabled = run("systemctl", "--user", "is-enabled", "docker.service", required=False).stdout.strip() == "enabled"
    missing = [str(path) for path in paths if not path.exists()]
    if root.exists() and stat.S_IMODE(root.stat().st_mode) != 0o750:
        missing.append("服务根目录权限 0750")
    if before != desired:
        missing.append("daemon.json 的 log-driver=local")
    if load == "not-found":
        missing.append("安装 rootless Docker 用户服务")
    if not active or not enabled:
        missing.append("启用并启动 Docker 用户服务")
    restart = before != desired or (info is not None and info.get("LoggingDriver") != "local")
    if restart and active:
        missing.append("重启当前用户的 Docker 服务以生效日志配置（会影响其容器）")
    for item in missing:
        print(f"缺失    {item}")
    if not apply:
        if missing:
            print("结果：用户准备尚未完成。--apply 将创建上述目录、调整用户配置和用户服务。")
            return 1
        print("结果：用户准备已核实（rootless、local、systemd / cgroup v2）；尚未部署 authentik。")
        return 0

    # 所有兼容性检查完成后才开始写入；配置校验失败时保留原文件。
    if before != desired:
        write_config(cfg_path, desired)
    for path in paths:
        if not path.exists():
            path.mkdir(parents=True, exist_ok=True, mode=0o750)
    if stat.S_IMODE(root.stat().st_mode) != 0o750:
        root.chmod(0o750)
    if load == "not-found":
        run("dockerd-rootless-setuptool.sh", "install", "--force", reason=setuptool_reason)
    if not active or not enabled:
        run("systemctl", "--user", "enable", "--now", "docker", reason=service_reason)
    if restart and active:
        run("systemctl", "--user", "restart", "docker", reason=service_reason)
    # 安装/启动/重启失败时保留用户文件，重跑会比较磁盘配置和实际日志驱动。
    return prepare(user, root, False)


def main() -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="遇到以下情况，在修改之前报告并停止：以 root 运行或不是指定用户；属于 sudo 或 docker 组；"
               "HOME 与家目录不一致；目录或 daemon.json 不归当前用户或为符号链接；daemon.json 无法解析；"
               "缺少命令、XDG_RUNTIME_DIR 或用户级 systemd 会话；官方前提检查失败；运行中的 Docker 不是 rootless。"
               "重启 Docker 用户服务会影响该用户已有的容器。")
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--user", default="authentik")
    parser.add_argument("--service-dir", type=Path, default=Path("/srv/authentik"))
    args = parser.parse_args()
    try:
        return prepare(args.user, args.service_dir, args.apply)
    except (PreparationError, OSError) as exc:
        print(f"FAIL    {exc}")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
