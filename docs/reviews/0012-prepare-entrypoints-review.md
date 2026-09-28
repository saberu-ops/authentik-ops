# 评审 0012：主机准备与用户初始化入口

- 日期：2026-09-28
- 性质：代码与资料评审，只读。评审过程中没有修改被评审的文件，也没有执行任何主机准备操作。
- 对象：清单 A2 的第二版实现，由 Codex 完成，见[操作记录 R002](../implementation/records.md#r002-准备入口的权限拆分与离线验证)。
- 用途：供操作者审核；本记录的结论决定 A3 能否开始。
- 输入冻结：`rebuild` 工作区，基线提交 `41a6635`，以下文件均未提交，sha256 为：

  | 文件 | sha256 |
  |---|---|
  | `scripts/host-prepare.sh` | `ef6986562327a150edcc275ad4d4bef55f087ab64027c0de9332131cdb090020` |
  | `scripts/user-prepare.py` | `d894de81ce457bc4f9f7e7837c5985f0c6601510a45f3bb9dcac8570c27511f4` |
  | `tests/test_prepare.py` | `0c30d731991aa7c78097b596d36b4ea4a5b33787cc0b4b6ba02152b022c1668f` |
  | `RUNBOOK.md` | `c61d995f95ceb9fedf08a1143315453d12e718c94bdb7eba9715b62b1de6973e` |
  | `README.md` | `eac9a9d99756bd3e46e7242ba4ddfd0d82083f5d3c58869fdbc49c1875e1dfd0` |
  | `docs/plans/0004-deployment-foundation.md` | `d739a3b63f5ff9ed2c331fc2721922a8c407422d8c24ecb1900cca3f8e061b32` |
  | `docs/implementation/records.md` | `ea26c91fd289152f2838be80eee42759b21760eef8fcabe8351e2345992e8391` |

## 结论

这一版把主机准备拆成两个入口，权限分工合理，所有修改都放在检查全部完成之后。上一轮评审提出的 5 项问题都已解决。

但有两项 P1 需要先修正，A3 才能开始：

- RUNBOOK 写入了尚未在真实主机上验证的流程；
- 脚本和资料的说明写成了“不做什么”，违背操作者的明确要求。

另有 3 项 P2 可以同时处理。

## 两个入口的行为

**`scripts/host-prepare.sh`**

- 由 root 执行，需要加 `--apply`，并且脚本须从 root 持有、其他用户不可改写的副本运行。
- 先完成全部检查，然后依次执行：
  1. 用 apt 安装 `uidmap`、`systemd-container`、`restic`、`python3-yaml`、`python3-jsonschema`；
  2. 用 `useradd` 创建专用用户，同时建立家目录、bash 和同名组，并各分配 65536 个 subuid/subgid；
  3. 为该用户开启 linger；
  4. 创建服务根目录：先在临时目录中设好属主和 0750 权限，再原子改名为 `/srv/authentik`。
- 退出码：0 表示已核实，1 表示缺失或不兼容，2 表示有未核实项。

**`scripts/user-prepare.py`**

- 由专用用户执行，需要加 `--apply`。
- 先核对身份、目录、Docker 配置和用户会话，并运行 Docker 官方的前提检查，然后依次执行：
  1. 把 `log-driver: local` 合并进 `daemon.json`，经 `dockerd --validate` 校验后原子替换；
  2. 创建 `code/`、`instance/{settings,local,state}/`、`backup/`；
  3. 按需安装、启用或重启 rootless Docker 用户服务；
  4. 通过该用户自己的 socket 复查，确认 rootless、日志驱动为 local、使用 systemd 与 cgroup v2。

## 做得好的地方

- **权限分工：** root 只做系统层面的准备，目录内部和 Docker 配置由专用用户完成，符合最小权限。
- **先检查后修改：** 所有修改都在检查全部通过之后才执行。已有账号不兼容时停止，由管理员处理。
- **服务目录安全：** 服务根目录位于只有 root 能写的父目录下，用原子改名发布；目标路径被抢占时不覆盖。
- **ID 映射校验更严格：** subuid/subgid 的格式错误、数值溢出、与其他用户重叠、同一账号自身重叠，都能识别。
- **Docker 配置可重跑：** 合并时保留原有字段；校验失败时原文件不变；启动失败后可以重跑。
- **上一轮问题的复测：** 普通用户可写的上级目录、`--user root` 两种情况，本次复测都会被拒绝，退出码为 1。

## 问题

### P1-1 RUNBOOK 写入了尚未验证的流程

- **位置：**
  - `RUNBOOK.md` 第 5–36 行，新增“重建准备入口”一节；
  - `README.md` 第 74 行：“当前完成的是离线验证，实际主机执行状态以实施清单为准”。
- **问题：**
  - AGENTS.md 的约定是“README.md and RUNBOOK.md describe only implemented, verified behavior”。这两个入口目前只有模拟测试，真实的账号创建和用户服务都要到 A3 才验证。
  - 这一节插在仍然描述 `/opt/authentik` rootful 部署的旧 RUNBOOK 开头，新旧两套流程混在一起。
  - README 中写入了会过时的进度说明。
- **建议：**
  - 撤回 RUNBOOK 和 README 的这两处新增，操作步骤保留在方案 0004 中。
  - A3 验证通过后，再按验证结果写入 RUNBOOK。

### P1-2 说明写成了“不做什么”

操作者 2026-09-28 的要求：“脚本的做的内容，不要给出不做什么，而应该给出会做什么”。

- **位置：**
  - `scripts/host-prepare.sh` 第 2 行，文件开头只剩一句概述；第 16–17 行：“本脚本不安装 Docker、不修改既有 ID 映射，不接管不兼容账号或目录”。
  - `scripts/user-prepare.py` 第 202 行：“不创建账号、不使用 sudo、不部署 authentik”。
  - 方案 0004 第 62、66、69、72、82 行，例如第 82 行：“它不生成实例机密、不启动 authentik，不与现有 rootful bootstrap 混用”。
  - `RUNBOOK.md` 第 24 行：“它不删除账号、卷、实例数据或已有配置键”。
- **建议：**
  - 脚本开头、`--help` 和方案中，按顺序列出每一步会执行的操作；
  - 另列一份“只检查并报告、由操作者处理”的项目；
  - 必须说明的边界，改用正面的写法，例如“已有账号不兼容时报错停止”。

### P2-1 命令失败时错误原因被吞掉

- **位置：** `scripts/user-prepare.py` 第 22–31 行。`run()` 失败时只报告命令名和退出码。
- **问题：**
  - 用坏配置实测 `dockerd --validate`，它会给出具体原因：“cannot unmarshal number into Go struct field … log-driver of type string”，但用户只能看到“dockerd --validate 失败”。
  - Docker 官方前提检查列出的缺失项，也同样看不到。
  - 到 A3 真实执行时，出了问题会很难排查。
- **建议：** 对输出中不含机密的命令（官方前提检查、配置校验、systemctl），打印错误输出的最后几行。

### P2-2 回归测试缺少两个关键场景

- **位置：** `tests/test_prepare.py`，共 14 个测试。
- **问题：** 缺少以下两个场景的测试：
  - 上一轮评审的 P0：服务目录的上级目录可被普通用户写入，或者含符号链接时，必须拒绝；
  - 从入口端到端测试 `--user root` 被拒绝。
  
  本次只做了手工复测。
- **建议：** 补上这两个测试，防止以后改动时再次出错。

### P2-3 证据位置的描述不准确

- **位置：** `docs/implementation/records.md` 第 70 行，证据位置写的是 `/tmp/authentik-user-prepare-z10z2ui0/`。
- **问题：** `/tmp` 在重启后会被清空，而这一行容易被理解为证据只存放在那里。
  - 实际上，R002 本身已经记录了测试范围、结果、验证缺口和代码哈希（按操作者的审核意见更正）。
- **建议：** 写明长期依据是仓库内的记录和可重复执行的测试，`/tmp` 中的日志只是辅助材料。

## 需要操作者确认

方案 0004 已经批准，这一版在第 48 行新增了一段：“2026-09-28 批准的范围调整：操作者要求创建专用用户后，把其能够完成的工作交给该用户”。

本评审所在的对话中没有这条指示，请操作者确认它的来源。如果属实，这项调整本身合理，不影响前面的评审结论。

## 操作者审核意见（2026-09-28）

- **整体结论：** 认可。一次性的管理员准备由 root 完成，其余初始化和运维交给专用用户，这个权限设计合理。这也确认了方案 0004 第 48 行记载的范围调整。
- **两项 P1 成立：**
  - 尚未实际验证的流程留在方案中，进度放在实施清单；
  - 脚本说明按顺序写清楚“执行哪些操作、遇到什么条件停止”。
- **P2-1：** 针对具体子命令保留安全的错误原因，不笼统打印 systemctl 或配置校验的全部输出。
- **P2-2：** 权限回归测试需覆盖三种情况：
  - 父目录可被普通用户写入；
  - 符号链接；
  - 从完整入口拒绝 `--user root`。
- **P2-3：** 按上文更正后的描述处理。

## 处理结果（2026-09-28）

各项处理后，文件 sha256 前缀如下：

| 文件 | sha256 前缀 |
|---|---|
| `scripts/host-prepare.sh` | `b37563be3bc02f24` |
| `scripts/user-prepare.py` | `3e9b731a83b520d4` |
| `tests/test_prepare.py` | `72dd2ce6488cb473` |

- **P1-1：**
  - 撤回 `RUNBOOK.md` 中的“重建准备入口”一节，以及 README 中的对应说明；RUNBOOK 恢复为基线版本。
  - 执行顺序、完成标准和回归测试入口移到方案 0004「管理员准备与用户初始化」。
  - 进度仍在实施清单 A2、A3 维护。
- **P1-2：** 以下三处都改为按顺序列出执行的操作，另列遇到哪些条件时在修改前停止：
  - 两个脚本的开头说明和 `--help`；
  - 方案 0004 的对应一节。
- **P2-1：** `user-prepare.py` 按子命令提炼失败原因，每类只保留以下内容：

  | 子命令 | 保留的失败原因 |
  |---|---|
  | 官方前提检查 | `[ERROR]`、`[WARNING]` 行 |
  | 配置校验 | 字段名和类型，已用真实的 `dockerd` 输出核对 |
  | 用户服务启动或重启 | ActiveState、SubState、Result、ExecMainStatus 四个状态字段，并附上 `journalctl` 的查看方法 |
  | Docker 连接 | 失败类别 |
  | 用户会话 | 不输出任何原始内容 |

- **P2-2：** 新增 4 个权限测试和 4 个错误原因测试，现有测试共 28 个，全部通过。权限测试覆盖：
  - 父目录可被普通用户写入；
  - 属于 root 的符号链接上级目录；
  - 从完整入口拒绝符号链接服务目录；
  - 从完整入口拒绝 `--user root`。
- **P2-3：** R002 的证据位置已改为：长期依据是仓库内的记录和可重复执行的测试，`/tmp` 中的日志是辅助材料。

## 已执行的检查

- `python3 -B -m unittest discover -s tests`：20 个测试通过，其中准备入口 14 个、文档检查 6 个。
- `bash -n scripts/host-prepare.sh`、Python 语法检查、`python3 scripts/check_docs.py`：通过。
- 以普通用户运行 `scripts/host-prepare.sh` 检查模式：退出码 1。列出的缺失项有 `uidmap`、`systemd-container`、`restic`、专用用户和服务根目录，与主机现状一致；运行后主机没有任何变化。
- 以 `spartan` 运行 `scripts/user-prepare.py`：提示“专用用户不存在”，退出码 1。
- 以普通用户运行 `dockerd --validate --config-file <临时文件>`：合法配置输出 “configuration OK”；非法配置返回退出码 1 并给出原因。这说明用户入口依赖的配置校验可以在无特权情况下运行。
- 复测上一轮的 P0 和 `--user root`：都被拒绝。

## 未覆盖

- 本机没有安装 shellcheck；CI 需要推送后才会运行。
- 真实的 `--apply`、账号创建和 rootless 用户服务的生命周期，都要到 A3 单独授权后才能验证。
