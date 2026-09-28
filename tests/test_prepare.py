"""准备阶段的权限边界与失败恢复；系统命令均替换为模拟，不创建真实账号或服务。"""
from __future__ import annotations

import importlib.util
import json
import os
from pathlib import Path
import subprocess
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("user_prepare", ROOT / "scripts/user-prepare.py")
user_prepare = importlib.util.module_from_spec(spec)
spec.loader.exec_module(user_prepare)


class HostPrepareTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name)

    def shell(self, code, *args):
        return subprocess.run(["bash", "-c", 'source "$1"\nshift\n' + code, "test",
                               str(ROOT / "scripts/host-prepare.sh"), *map(str, args)],
                              capture_output=True, text=True)

    def test_subid_classification(self):
        cases = {
            "single": ("authentik:100000:65536\nother:200000:65536\n", "ok"),
            "split": ("authentik:100000:32768\n1001:200000:32768\n", "ok"),
            "short_overlap": ("authentik:100000:100\nother:100000:65536\n", "overlap"),
            "self_overlap": ("authentik:100000:65536\n1001:100000:65536\n", "overlap"),
            "malformed": ("authentik:bad:65536\n", "invalid"),
            "overflow": ("authentik:4294967200:65536\n", "invalid"),
            "short": ("authentik:100000:100\n", "missing"),
            "empty": ("", "missing"),
        }
        for name, (data, expected) in cases.items():
            with self.subTest(name=name):
                path = self.base / name
                path.write_text(data)
                result = self.shell('subid_status "$1" authentik 1001', path)
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertEqual(result.stdout.strip(), expected)
        result = self.shell('subid_status "$1" authentik 1001', self.base / "absent")
        self.assertEqual(result.stdout.strip(), "missing")

    def test_incompatible_or_unverified_preflight_prevents_all_mutations(self):
        for blocked, unknown, expected in [(1, 0, 1), (0, 1, 2)]:
            with self.subTest(blocked=blocked, unknown=unknown):
                result = self.shell(f'''
assert_apply_allowed() {{ :; }}
preflight() {{ blocked={blocked}; unverified={unknown}; missing=1; account_exists=0; apt_missing=(uidmap); }}
apt-get() {{ echo MUTATION; }}
useradd() {{ echo MUTATION; }}
loginctl() {{ echo MUTATION; }}
create_service_dir() {{ echo MUTATION; }}
main --apply --no-service-dir
''')
                self.assertEqual(result.returncode, expected, result.stderr)
                self.assertNotIn("MUTATION", result.stdout)

    def test_unknown_check_never_claims_ready(self):
        result = self.shell('''
preflight() { blocked=0; unverified=1; missing=0; }
main --no-service-dir
''')
        self.assertEqual(result.returncode, 2)
        self.assertNotIn("主机准备已核实", result.stdout)

    def test_existing_privileged_user_blocked(self):
        result = self.shell('''
getent() { printf 'authentik:x:0:0::/root:/bin/bash\n'; }
id() { printf '0\n'; }
USER_NAME=authentik; blocked=0
check_account
printf 'blocked=%s\n' "$blocked"
''')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("blocked=1", result.stdout)

    def test_directory_chown_failure_is_retryable(self):
        target = self.base / "service"
        result = self.shell('''
USER_NAME=fixture; SERVICE_DIR="$1"
chown() { return 1; }
create_service_dir
''', target)
        self.assertNotEqual(result.returncode, 0)
        self.assertFalse(target.exists())
        self.assertEqual(list(self.base.iterdir()), [])
        # The fixture uses the current uid/gid, never actual root ownership.
        result = self.shell('''
USER_NAME=fixture; SERVICE_DIR="$1"
id() { if [[ "$1" == -u ]]; then command id -u; else command id -g; fi; }
create_service_dir
chown() { echo UNEXPECTED; return 1; }
create_service_dir
''', target)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertNotIn("UNEXPECTED", result.stdout)
        self.assertEqual(target.stat().st_mode & 0o777, 0o750)

    def test_parent_writable_by_normal_user_is_blocked(self):
        # 临时目录属于当前普通用户：检查后可以被替换，必须在修改前拒绝。
        result = self.shell('''
USER_NAME=fixture; SERVICE_DIR="$1"; account_exists=0; blocked=0
check_service_dir
printf 'blocked=%s\n' "$blocked"
''', self.base / "service")
        self.assertIn("blocked=1", result.stdout)
        self.assertIn("直接父目录", result.stderr)

    def test_symlinked_ancestor_is_blocked(self):
        # 需要一个属于 root 的符号链接目录（merged-usr 系统上的 /bin 等），才能单独验证符号链接判断。
        link = next((path for path in ("/bin", "/sbin", "/lib") if os.path.islink(path)
                     and os.lstat(path).st_uid == 0 and os.path.isdir(path)), None)
        if link is None:
            self.skipTest("没有属于 root 的符号链接目录")
        result = self.shell('''
USER_NAME=fixture; SERVICE_DIR="$1"; account_exists=0; blocked=0
check_service_dir
printf 'blocked=%s\n' "$blocked"
''', f"{link}/authentik-test")
        self.assertIn("blocked=1", result.stdout)

    def test_full_entry_rejects_symlinked_service_dir(self):
        target = self.base / "target"
        target.mkdir()
        (self.base / "link").symlink_to(target)
        result = subprocess.run([str(ROOT / "scripts/host-prepare.sh"), "--service-dir", str(self.base / "link")],
                                capture_output=True, text=True)
        self.assertEqual(result.returncode, 1)
        self.assertIn("符号链接", result.stderr)

    def test_full_entry_rejects_root_user(self):
        result = subprocess.run([str(ROOT / "scripts/host-prepare.sh"), "--user", "root", "--no-service-dir"],
                                capture_output=True, text=True)
        self.assertEqual(result.returncode, 1)
        self.assertIn("root 不是普通用户", result.stderr)

    def test_directory_collision_never_overwrites_target(self):
        target = self.base / "service"
        result = self.shell('''
USER_NAME=fixture; SERVICE_DIR="$1"
chown() { mkdir -- "$SERVICE_DIR"; touch "$SERVICE_DIR/existing"; }
create_service_dir
''', target)
        self.assertNotEqual(result.returncode, 0)
        self.assertTrue((target / "existing").exists())


class UserPrepareTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name)
        self.root = self.base / "service"
        self.config = self.base / "config/daemon.json"
        self.runtime = self.base / "runtime"
        self.runtime.mkdir()
        self.calls = []
        self.loaded = False
        self.active = False
        self.enabled = False
        self.log_driver = "json-file"
        self.rootless = True
        self.fail_restart = False
        self.fail_validate = False
        account = SimpleNamespace(pw_uid=os.geteuid(), pw_dir=str(self.base))
        for mock in [patch.object(user_prepare, "identity", return_value=account),
                     patch.object(user_prepare, "config_path", return_value=self.config),
                     patch.object(user_prepare.shutil, "which", return_value="/mock/command"),
                     patch.object(user_prepare, "run", side_effect=self.command),
                     patch.dict(os.environ, {"XDG_RUNTIME_DIR": str(self.runtime)})]:
            mock.start()
            self.addCleanup(mock.stop)

    def command(self, *args, required=True, reason=None):
        self.calls.append(args)
        out, rc = "", 0
        if args[:3] == ("systemctl", "--user", "show"):
            out = "loaded" if self.loaded else "not-found"
        elif args[:3] == ("systemctl", "--user", "is-active"):
            out, rc = ("active", 0) if self.active else ("inactive", 3)
        elif args[:3] == ("systemctl", "--user", "is-enabled"):
            out, rc = ("enabled", 0) if self.enabled else ("disabled", 1)
        elif args[:2] == ("dockerd-rootless-setuptool.sh", "install"):
            self.loaded = self.active = True
            self.log_driver = json.loads(self.config.read_text())["log-driver"]
        elif args[:3] == ("systemctl", "--user", "enable"):
            self.enabled = self.active = True
            self.log_driver = json.loads(self.config.read_text())["log-driver"]
        elif args[:3] == ("systemctl", "--user", "restart"):
            if self.fail_restart:
                raise user_prepare.PreparationError("simulated restart failure")
            self.log_driver = json.loads(self.config.read_text())["log-driver"]
        elif args[0] == "docker":
            self.assertEqual(args[1:3], ("--host", f"unix://{self.runtime}/docker.sock"))
            out = json.dumps({"SecurityOptions": ["name=rootless"] if self.rootless else [],
                              "LoggingDriver": self.log_driver, "CgroupDriver": "systemd", "CgroupVersion": "2"})
        elif args[0] == "dockerd" and self.fail_validate:
            raise user_prepare.PreparationError("simulated config rejection")
        return subprocess.CompletedProcess(args, rc, out, "")

    def apply(self, apply=True):
        return user_prepare.prepare("fixture", self.root, apply)

    def seed_config(self):
        self.config.parent.mkdir()
        self.config.write_text(json.dumps({"data-root": "/srv/fixture/docker", "log-driver": "json-file"}))

    def test_check_is_read_only(self):
        self.assertEqual(self.apply(False), 1)
        self.assertFalse(self.root.exists())
        self.assertFalse(self.config.exists())
        self.assertFalse(any("install" in args or "enable" in args or "restart" in args for args in self.calls))

    def test_apply_then_repeat_preserves_config_without_service_mutation(self):
        self.seed_config()
        self.assertEqual(self.apply(), 0)
        self.assertEqual(json.loads(self.config.read_text()), {"data-root": "/srv/fixture/docker", "log-driver": "local"})
        self.assertEqual(self.root.stat().st_uid, os.geteuid())
        self.assertTrue((self.root / "instance/state").is_dir())
        before = self.config.stat().st_mtime_ns
        self.calls.clear()
        self.assertEqual(self.apply(), 0)
        self.assertEqual(before, self.config.stat().st_mtime_ns)
        self.assertFalse(any("install" in args or "enable" in args or "restart" in args for args in self.calls))

    def test_invalid_config_stops_before_writes(self):
        self.config.parent.mkdir()
        self.config.write_text("broken")
        with self.assertRaises(user_prepare.PreparationError):
            self.apply()
        self.assertEqual(self.config.read_text(), "broken")
        self.assertFalse(self.root.exists())
        self.assertEqual(self.calls, [])

    def test_failed_validation_keeps_original(self):
        self.seed_config()
        before = self.config.read_bytes()
        self.fail_validate = True
        with self.assertRaises(user_prepare.PreparationError):
            self.apply()
        self.assertEqual(before, self.config.read_bytes())
        self.assertEqual(list(self.config.parent.iterdir()), [self.config])
        self.assertFalse(self.root.exists())

    def test_failed_restart_is_retried_even_if_config_already_written(self):
        self.seed_config()
        self.loaded = self.active = self.enabled = True
        self.fail_restart = True
        with self.assertRaises(user_prepare.PreparationError):
            self.apply()
        self.assertEqual(json.loads(self.config.read_text())["log-driver"], "local")
        self.fail_restart = False
        self.calls.clear()
        self.assertEqual(self.apply(), 0)
        self.assertIn(("systemctl", "--user", "restart", "docker"), self.calls)

    def test_rootful_socket_stops_before_writes(self):
        self.loaded = self.active = True
        self.rootless = False
        with self.assertRaises(user_prepare.PreparationError):
            self.apply()
        self.assertFalse(self.config.exists())
        self.assertFalse(self.root.exists())

    def test_conflicting_directory_stops_before_writes(self):
        self.root.mkdir()
        (self.root / "instance").write_text("existing file")
        with self.assertRaises(user_prepare.PreparationError):
            self.apply()
        self.assertEqual((self.root / "instance").read_text(), "existing file")
        self.assertEqual(self.calls, [])

    def test_root_and_other_identity_are_rejected(self):
        # Use the real identity function, restored temporarily from a fresh module.
        other = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(other)
        with patch.object(other.os, "geteuid", return_value=0):
            with self.assertRaisesRegex(other.PreparationError, "root"):
                other.identity("fixture")
        account = SimpleNamespace(pw_uid=2001)
        with patch.object(other.os, "geteuid", return_value=2002), patch.object(other.pwd, "getpwnam", return_value=account):
            with self.assertRaisesRegex(other.PreparationError, "登录"):
                other.identity("fixture")


class FailureReasonTest(unittest.TestCase):
    """失败原因只保留各子命令中不含配置值和环境变量的部分。"""

    @staticmethod
    def result(stdout="", stderr=""):
        return subprocess.CompletedProcess(("cmd",), 1, stdout, stderr)

    def test_validate_keeps_field_and_type_only(self):
        typed = self.result(stderr="unable to configure the Docker daemon with file /tmp/x.json: json: cannot unmarshal "
                                   "number into Go struct field Config.CommonConfig.LogConfig.log-driver of type string")
        self.assertEqual(user_prepare.validate_reason(typed), "cannot unmarshal number into Go struct field "
                                                              "Config.CommonConfig.LogConfig.log-driver of type string")
        unknown = self.result(stderr="unable to configure the Docker daemon with file /tmp/x.json: the following "
                                     "directives don't match any configuration option: bogus-key")
        self.assertEqual(user_prepare.validate_reason(unknown),
                         "the following directives don't match any configuration option: bogus-key")
        valued = self.result(stderr='invalid proxy "http://user:secret@proxy.example"')
        self.assertNotIn("secret", user_prepare.validate_reason(valued))

    def test_setuptool_keeps_error_and_warning_lines(self):
        output = self.result(stdout="[INFO] Checking\n[ERROR] Missing system requirements.\nsudo apt-get install -y uidmap\n")
        self.assertEqual(user_prepare.setuptool_reason(output), "[ERROR] Missing system requirements.")

    def test_service_reason_keeps_state_fields_only(self):
        shown = subprocess.CompletedProcess(("systemctl",), 0, "ActiveState=failed\nSubState=failed\nResult=exit-code\n"
                                                              "ExecMainStatus=1\nEnvironment=TOKEN=secret\n", "")
        with patch.object(user_prepare, "run", return_value=shown):
            reason = user_prepare.service_reason(self.result())
        self.assertIn("ActiveState=failed", reason)
        self.assertIn("ExecMainStatus=1", reason)
        self.assertNotIn("secret", reason)

    def test_session_and_docker_reasons_do_not_echo_output(self):
        self.assertNotIn("secret", user_prepare.session_reason(self.result(stdout="TOKEN=secret")))
        refused = self.result(stderr="Cannot connect to the Docker daemon at unix:///run/user/1001/docker.sock. secret")
        self.assertEqual(user_prepare.docker_reason(refused), "无法连接该用户的 Docker socket")


if __name__ == "__main__":
    unittest.main()
