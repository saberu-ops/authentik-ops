"""scripts/check_docs.py 的回归测试：在临时副本中注入违规，确认都能被发现。"""

from __future__ import annotations

import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def repo_files() -> list[str]:
    # 只复制跟踪和未忽略的文件：部署目录里的 .env、data/ 等被忽略的机密与数据不能进入临时目录。
    out = subprocess.run(["git", "-C", str(ROOT), "ls-files", "-z", "--cached", "--others", "--exclude-standard"],
                         capture_output=True, check=True).stdout
    return [name for name in out.decode().split("\0") if name and (ROOT / name).is_file()]


class CheckDocsTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.repo = Path(self.tmp.name)
        for name in repo_files():
            target = self.repo / name
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(ROOT / name, target)
        subprocess.run(["git", "init", "-q", str(self.repo)], check=True)

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def write(self, name: str, text: str) -> None:
        path = self.repo / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)

    def index(self, *targets: str) -> None:
        with (self.repo / "docs/README.md").open("a") as index:
            index.write("\n" + "".join(f"- [条目]({target})\n" for target in targets))

    def check(self) -> subprocess.CompletedProcess[str]:
        return subprocess.run([sys.executable, "-B", str(self.repo / "scripts/check_docs.py")],
                              capture_output=True, text=True)

    def assert_fails(self, *messages: str) -> None:
        result = self.check()
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        for message in messages:
            self.assertIn(message, result.stderr)

    def test_current_docs_pass(self) -> None:
        result = self.check()
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_bad_links(self) -> None:
        self.write(".env", "")
        self.index("../.env", "../../outside.md", "missing.md", "plans/0004-deployment-foundation.md#不存在")
        self.assert_fails("链接指向被 Git 忽略的文件 ../.env", "链接指向仓库之外 ../../outside.md",
                          "链接目标不存在 missing.md", "锚点不存在 plans/0004-deployment-foundation.md#不存在")

    def test_links_in_indented_code_are_ignored(self) -> None:
        with (self.repo / "docs/README.md").open("a") as index:
            index.write("\n- 列表\n\n    ```text\n    [不检查](missing.md)\n    ```\n")
        self.assertEqual(self.check().returncode, 0)

    def test_unindexed_document(self) -> None:
        self.write("docs/extra.md", "# 孤立\n")
        self.assert_fails("docs/README.md 未收录: ['docs/extra.md']")

    def test_numbering(self) -> None:
        self.write("docs/plans/0004-duplicate.md", "# 重复\n\n- 状态：DRAFT\n")
        self.write("docs/plans/0002-reused.md", "# 复用\n\n- 状态：DRAFT\n")
        self.write("docs/plans/BadName.md", "# 坏名\n")
        self.write("docs/plans/sub/0009-nested.md", "# 子目录\n")
        self.write("docs/reviews/0010-reused.md", "# 复用\n")
        self.write("docs/changelogs/0099-orphan.md", "# 无对应方案\n")
        self.index("plans/0004-duplicate.md", "plans/0002-reused.md", "plans/BadName.md", "plans/sub/0009-nested.md",
                   "reviews/0010-reused.md", "changelogs/0099-orphan.md")
        self.assert_fails("编号 0004 重复", "0002-reused.md: 编号不得小于 0004", "BadName.md: 文件名须为",
                          "docs/plans 下不允许子目录", "0010-reused.md: 编号不得小于 0011",
                          "docs/changelogs: 编号没有对应的方案 ['0099']")

    def test_plan_status_inside_code_block_does_not_count(self) -> None:
        self.write("docs/plans/0006-fenced.md", "# 状态在代码块里\n\n```text\n- 状态：DRAFT\n```\n")
        self.index("plans/0006-fenced.md")
        self.assert_fails("0006-fenced.md: 开头须有")


if __name__ == "__main__":
    unittest.main()
