#!/usr/bin/env python3
"""Validate authentik-ops agent entry files, Markdown links, and runbook sections."""

from __future__ import annotations

import re
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


def without_fenced_code(text: str) -> str:
    kept, fence = [], None
    for line in text.splitlines():
        match = re.match(r"^\s{0,3}(`{3,}|~{3,})", line)
        if match and (fence is None or match.group(1)[0] == fence):
            fence = None if fence else match.group(1)[0]
            continue
        if fence is None:
            kept.append(line)
    return "\n".join(kept)


def anchor_slug(heading: str) -> str:
    heading = re.sub(r"[`*_~]", "", heading.strip().lower())
    heading = re.sub(r"[^\w\- ]", "", heading)
    return re.sub(r"[ -]+", "-", heading).strip("-")


def headings(path: Path, level: str = "#{1,6}") -> list[str]:
    return re.findall(rf"^{level}\s+(.+?)\s*$", without_fenced_code(path.read_text()), re.M)


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

    docs = sorted(ROOT.glob("*.md"))
    anchors = {p.name: {anchor_slug(h) for h in headings(p)} for p in docs}
    for doc in docs:
        for target in LINK_PATTERN.findall(without_fenced_code(doc.read_text())):
            if re.match(r"^[a-z]+:", target):
                continue
            file_part, _, anchor = target.partition("#")
            path = (ROOT / file_part) if file_part else doc
            if not path.exists():
                errors.append(f"{doc.name}: 链接目标不存在 {target}")
            elif anchor and path.suffix == ".md" and anchor not in anchors.get(path.name, set()):
                errors.append(f"{doc.name}: 锚点不存在 {target}")

    present = headings(ROOT / "RUNBOOK.md", "##")
    for section in RUNBOOK_SECTIONS:
        if section not in present:
            errors.append(f"RUNBOOK.md 缺少章节: ## {section}")

    for error in errors:
        print(f"FAIL  {error}", file=sys.stderr)
    if not errors:
        print("ok    文档：入口文件、链接与锚点、RUNBOOK 必需章节")
    return 1 if errors else 0


if __name__ == "__main__":
    sys.exit(main())
