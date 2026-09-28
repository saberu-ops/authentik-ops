#!/usr/bin/env bash
# 仓库静态检查：解析 compose 配置并校验安全不变量、文档与入口文件。不启动服务、不读取线上 .env，
# 但会运行一次性离线容器校验 Caddyfile（在部署主机上运行即属于主机操作）。
# 需要 docker compose 与 python3；shellcheck、systemd-analyze 可选。
# 用法: scripts/check.sh [--no-containers]   --no-containers 跳过需要运行容器的检查
set -euo pipefail

containers=1
case "${1:-}" in
  "") ;;
  --no-containers) containers=0 ;;
  *) echo "用法: scripts/check.sh [--no-containers]" >&2; exit 2 ;;
esac

cd "$(dirname "$(readlink -f "${BASH_SOURCE[0]}")")/.."
failed=0
ok() { echo "ok    $*"; }
bad() { echo "FAIL  $*" >&2; failed=1; }
skip() { echo "skip  $*"; }

work="$(mktemp -d)"
trap 'rm -rf "${work}"' EXIT
cp compose.yml compose.override.yml Caddyfile "${work}/"

# 1) 原样使用模板必须被拒绝（密钥为空）
cp .env.example "${work}/.env"
if (cd "${work}" && docker compose config --quiet 2>/dev/null); then
  bad ".env.example 原样使用时 compose 仍可启动（密钥应为空）"
else
  ok ".env.example 原样使用时 compose 拒绝启动"
fi

# 2) 填入测试密钥后解析，校验不变量
sed -e 's|^PG_PASS=.*|PG_PASS=check|' -e 's|^AUTHENTIK_SECRET_KEY=.*|AUTHENTIK_SECRET_KEY=check|' \
  .env.example > "${work}/.env"
printf "AUTHENTIK_BOOTSTRAP_PASSWORD_HASH='pbkdf2_sha256\$1\$salt\$hash'\n" >> "${work}/.env"
if ! (cd "${work}" && docker compose config --format json) > "${work}/config.json"; then
  bad "docker compose config 解析失败"
  exit 1
fi
python3 - "${work}" <<'PY' || failed=1
import json, os, sys

work = os.path.realpath(sys.argv[1])
cfg = json.load(open(os.path.join(work, "config.json")))
services = cfg["services"]
problems = []

def check(cond, message):
    print(("ok    " if cond else "FAIL  ") + message, file=sys.stdout if cond else sys.stderr)
    if not cond:
        problems.append(message)

check(cfg["name"] == "authentik", "项目名固定为 authentik（数据库卷 authentik_database）")

host_paths = {"/var/log/caddy"}
binds = [(name, m["source"]) for name, s in services.items()
         for m in s.get("volumes", []) if m.get("type") == "bind"]
outside = [(n, src) for n, src in binds
           if not os.path.realpath(src).startswith(work + os.sep) and src not in host_paths]
check(not outside, f"bind mount 均位于部署目录内（例外: /var/log/caddy）{outside or ''}")
check(all(src != "/var/run/docker.sock" for _, src in binds), "未挂载 docker.sock")

rooted = [n for n, s in services.items() if str(s.get("user", "")) in ("root", "0")]
check(not rooted, f"没有服务以 root 用户运行 {rooted or ''}")

public = [(n, p.get("published"), p.get("host_ip") or "0.0.0.0")
          for n, s in services.items() if n != "caddy"
          for p in s.get("ports", []) if p.get("host_ip") not in ("127.0.0.1", "::1")]
check(not public, f"除 caddy 外的端口只绑定 loopback {public or ''}")

# config 输出把字面 $ 序列化为 $$；容器实际收到单个 $。
bootstrap = services["server"]["environment"].get("AUTHENTIK_BOOTSTRAP_PASSWORD_HASH", "")
check(bootstrap.replace("$$", "$") == "pbkdf2_sha256$1$salt$hash", ".env 中单引号包裹的密码哈希不被插值")

sys.exit(1 if problems else 0)
PY

# 3) Caddyfile
# config --images <服务> 会连同依赖服务的镜像一起输出，因此从 JSON 取单个服务的镜像。
caddy_image="$(python3 -c 'import json, sys; print(json.load(open(sys.argv[1]))["services"]["caddy"]["image"])' \
  "${work}/config.json")"
if [[ ${containers} -eq 0 ]]; then
  skip "Caddyfile 校验（--no-containers）"
elif docker run --rm --network none --memory 128m -v "${work}/Caddyfile:/etc/caddy/Caddyfile:ro" \
  "${caddy_image}" caddy validate --adapter caddyfile --config /etc/caddy/Caddyfile >/dev/null 2>&1; then
  ok "Caddyfile 校验通过（${caddy_image}）"
else
  bad "Caddyfile 校验失败"
fi

# 4) Shell 脚本
for script in scripts/*.sh; do
  if bash -n "${script}"; then ok "bash -n ${script}"; else bad "bash -n ${script}"; fi
done
if command -v shellcheck >/dev/null; then
  if shellcheck scripts/*.sh; then ok "shellcheck"; else bad "shellcheck"; fi
else
  skip "shellcheck 未安装"
fi

# 5) systemd 单元（ExecStart 指向本仓库脚本以便校验）
if command -v systemd-analyze >/dev/null; then
  mkdir "${work}/units"
  for unit in systemd/*; do
    sed "s|/opt/authentik|${PWD}|g" "${unit}" > "${work}/units/$(basename "${unit}")"
  done
  # verify 也会报告主机上其他单元的问题，只看本仓库单元相关的输出。
  systemd-analyze verify "${work}"/units/* >/dev/null 2>"${work}/units.err" || true
  if grep -q 'authentik-backup' "${work}/units.err"; then
    bad "systemd-analyze verify: $(grep 'authentik-backup' "${work}/units.err")"
  else
    ok "systemd-analyze verify"
  fi
else
  skip "systemd-analyze 未安装"
fi

# 6) 文档、agent 入口文件与 RUNBOOK 章节；tests/ 中的单元测试
python3 -B scripts/check_docs.py || failed=1
if python3 -B -m unittest discover -s tests -q; then ok "单元测试"; else bad "单元测试"; fi

# 7) 忽略规则：密钥与运行时状态不会被 Git 跟踪
# worktree 里的 .git 是文件而不是目录，所以由 git 自己判断是否位于工作树中。
if git rev-parse --is-inside-work-tree >/dev/null 2>&1; then
  for path in .env .env.20260101T000000Z.bak .akadmin-initial-password data/x certs/x caddy-data/x; do
    if git check-ignore -q --no-index "${path}"; then ok "git 忽略 ${path}"; else bad "git 未忽略 ${path}"; fi
  done
  if git check-ignore -q --no-index .env.example; then bad ".env.example 被忽略"; else ok "git 跟踪 .env.example"; fi
else
  skip "不是 Git checkout，跳过忽略规则检查"
fi

exit "${failed}"
