# 项目资料索引

根目录的 [README](../README.md) 和 [RUNBOOK](../RUNBOOK.md) 说明当前的部署和运维方式，只写已经实现并验证过的行为。

本目录保存方案、评审记录和实施资料。这些资料的写法、编号和存放规则见 [AGENTS.md「Project Documents」](../AGENTS.md#project-documents)。

## 方案

| 资料 | 内容 |
|---|---|
| [方案 0004](plans/0004-deployment-foundation.md) | 部署基础重建：rootless、Cloudflare Tunnel、实例与代码分离、备份恢复、更新与监控 |
| [方案 0005](plans/0005-app-security-baseline.md) | authentik 应用层安全基线：认证、管理员、权限、防滥用、通知与应急 |

## 评审与决策记录

| 资料 | 内容 |
|---|---|
| [评审 0011](reviews/0011-rebuild-direction-decisions.md) | 以 main 为基线的重建方向：各项决策、理由与来源 |
| [评审 0012](reviews/0012-prepare-entrypoints-review.md) | 主机准备与用户初始化入口（清单 A2）的代码与资料评审 |

## 实施

- [实施清单](implementation/checklist.md)：任务、依赖、完成检查。进度只在这里维护。
- [操作记录](implementation/records.md)：实际操作与结果。
