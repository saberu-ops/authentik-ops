#!/usr/bin/env python3
"""Validate authentik-ops agent entry files, Markdown links, the docs index, numbered records, and runbook sections."""

from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
ROOT_MARKDOWN_ALLOWLIST = {"AGENTS.md", "CLAUDE.md", "README.md", "RUNBOOK.md"}
AGENT_ENTRY_NAMES = {"AGENTS.md", "CLAUDE.md", "GEMINI.md", ".cursorrules", "copilot-instructions.md"}
RUNBOOK_SECTIONS = [
    "检查",
    "新主机安装",
    "修改配置",
    "升级",
    "备份",
    "从备份恢复",
    "密钥管理与轮换",
    "将现有部署目录转换为 Git checkout",
    "删除前置检查",
]
LINK_PATTERN = re.compile(r"\[[^\]]+\]\(([^)\s]+)\)")
DOCS = ROOT / "docs"
DOCS_INDEX = DOCS / "README.md"
# 方案、评审、结果记录的文件名为 NNNN-小写短名.md，编号在各自目录内唯一且永不复用。
# 0001–0003 号方案和 0001–0010 号评审已被废弃的草稿使用；结果记录沿用所属方案的编号。
NUMBERED_DIRS = {"plans": 4, "reviews": 11, "changelogs": 4}
NUMBERED_NAME = re.compile(r"^(\d{4})-[a-z0-9]+(?:-[a-z0-9]+)*\.md$")
PLAN_STATUS = re.compile(r"^- 状态：(DRAFT|APPROVED|COMPLETE)\b", re.M)


def without_fenced_code(text: str) -> str:
    kept, fence = [], None
    for line in text.splitlines():
        # 允许任意缩进：列表项内的代码块也要排除。
        match = re.match(r"^\s*(`{3,}|~{3,})", line)
        if match and (fence is None or match.group(1)[0] == fence):
            fence = None if fence else match.group(1)[0]
            continue
        if fence is None:
            kept.append(line)
    return "\n".join(kept)


def anchor_slug(heading: str) -> str:
    # 空格的处理与 GitHub 一致：去掉标点后，每个空格各换成一个连字符，不合并。
    heading = re.sub(r"[`*_~]", "", heading.strip().lower())
    heading = re.sub(r"[^\w\- ]", "", heading)
    return heading.replace(" ", "-")


def headings(path: Path, level: str = "#{1,6}") -> list[str]:
    return re.findall(rf"^{level}\s+(.+?)\s*$", without_fenced_code(path.read_text()), re.M)


def git_ignored(path: Path) -> bool:
    # 资料不能链接被忽略的文件（密钥、运行时数据），否则别的 checkout 上链接失效。不在 Git 工作树中时不检查。
    try:
        result = subprocess.run(["git", "-C", str(ROOT), "check-ignore", "-q", "--no-index", str(path)],
                                capture_output=True, check=False)
    except FileNotFoundError:
        return False
    return result.returncode == 0


def main() -> int:
    errors: list[str] = []

    actual = {p.name for p in ROOT.glob("*.md")}
    if actual != ROOT_MARKDOWN_ALLOWLIST:
        errors.append(f"根目录 Markdown 与允许列表不一致: 多出 {sorted(actual - ROOT_MARKDOWN_ALLOWLIST)}，"
                      f"缺少 {sorted(ROOT_MARKDOWN_ALLOWLIST - actual)}")

    # Claude 只加载 CLAUDE.md：它必须是只导入 AGENTS.md 的薄适配器，且 @ 导入不能位于代码块中。
    claude = ROOT / "CLAUDE.md"
    if claude.is_file():
        if without_fenced_code(claude.read_text()).split() != ["@AGENTS.md"]:
            errors.append("CLAUDE.md 必须只包含代码块之外的一行 @AGENTS.md")
        if not (ROOT / "AGENTS.md").is_file():
            errors.append("CLAUDE.md 导入的 AGENTS.md 不存在")

    nested = sorted(str(p.relative_to(ROOT)) for p in ROOT.rglob("*")
                    if p.name in AGENT_ENTRY_NAMES and p.parent != ROOT and ".git" not in p.parts)
    if nested:
        errors.append(f"不允许的 agent 入口文件: {nested}")

    # 链接与锚点：根目录与 docs/ 下的全部 Markdown，相对路径按所在目录解析。
    docs_pages = sorted(DOCS.rglob("*.md")) if DOCS.is_dir() else []
    markdown = sorted(ROOT.glob("*.md")) + docs_pages
    anchors = {p.resolve(): {anchor_slug(h) for h in headings(p)} for p in markdown}
    linked: dict[Path, set[Path]] = {}
    for doc in markdown:
        name = doc.relative_to(ROOT)
        targets = linked.setdefault(doc.resolve(), set())
        for target in LINK_PATTERN.findall(without_fenced_code(doc.read_text())):
            if re.match(r"^[a-z]+:", target):
                continue
            file_part, _, anchor = target.partition("#")
            path = (doc.parent / file_part).resolve() if file_part else doc.resolve()
            if not path.is_relative_to(ROOT):
                errors.append(f"{name}: 链接指向仓库之外 {target}")
            elif not path.exists():
                errors.append(f"{name}: 链接目标不存在 {target}")
            elif git_ignored(path):
                errors.append(f"{name}: 链接指向被 Git 忽略的文件 {target}")
            elif anchor and path.suffix == ".md" and anchor not in anchors.get(path, set()):
                errors.append(f"{name}: 锚点不存在 {target}")
            else:
                targets.add(path)

    # 资料索引：docs/ 下每份资料都要从 docs/README.md 直接链接，README.md 要链接索引。
    if docs_pages:
        index = DOCS_INDEX.resolve()
        if not DOCS_INDEX.is_file():
            errors.append("缺少资料索引 docs/README.md")
        else:
            missing = sorted(str(p.relative_to(ROOT)) for p in docs_pages
                             if p.resolve() != index and p.resolve() not in linked.get(index, set()))
            if missing:
                errors.append(f"docs/README.md 未收录: {missing}")
            if index not in linked.get((ROOT / "README.md").resolve(), set()):
                errors.append("README.md 需要链接 docs/README.md")

    # 方案、评审、结果记录：文件名带四位编号，不重复、不复用；方案开头有生命周期状态。
    numbers: dict[str, set[str]] = {}
    for sub, first in NUMBERED_DIRS.items():
        seen: dict[str, str] = {}
        for page in sorted((DOCS / sub).rglob("*.md")):
            if page.parent != DOCS / sub:
                errors.append(f"{page.relative_to(ROOT)}: docs/{sub} 下不允许子目录")
                continue
            match = NUMBERED_NAME.match(page.name)
            if not match:
                errors.append(f"docs/{sub}/{page.name}: 文件名须为 NNNN-小写短名.md")
                continue
            number = match.group(1)
            if int(number) < first:
                errors.append(f"docs/{sub}/{page.name}: 编号不得小于 {first:04d}（更小的编号已被废弃草稿使用）")
            if number in seen:
                errors.append(f"docs/{sub}: 编号 {number} 重复（{seen[number]}、{page.name}）")
            seen[number] = page.name
            header = without_fenced_code("\n".join(page.read_text().splitlines()[:15]))
            if sub == "plans" and not PLAN_STATUS.search(header):
                errors.append(f"docs/plans/{page.name}: 开头须有“- 状态：DRAFT/APPROVED/COMPLETE”")
        numbers[sub] = set(seen)
    orphans = sorted(numbers["changelogs"] - numbers["plans"])
    if orphans:
        errors.append(f"docs/changelogs: 编号没有对应的方案 {orphans}")

    present = headings(ROOT / "RUNBOOK.md", "##")
    for section in RUNBOOK_SECTIONS:
        if section not in present:
            errors.append(f"RUNBOOK.md 缺少章节: ## {section}")

    for error in errors:
        print(f"FAIL  {error}", file=sys.stderr)
    if not errors:
        print("ok    文档：入口文件、链接与锚点、资料索引、方案/评审/结果记录编号、RUNBOOK 必需章节")
    return 1 if errors else 0


if __name__ == "__main__":
    sys.exit(main())
