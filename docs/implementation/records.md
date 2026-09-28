# 实施操作记录

本文件保存实际执行的操作、验证结果和遗留问题。进度只在[实施清单](checklist.md)中维护。

本文件不写入密码、令牌、恢复链接或完整的敏感配置。原始日志保存在 Git 之外，这里只记它们的位置。

每条记录包含以下内容：

- 对应的任务；
- 时间与执行者；
- 操作目标与范围；
- 代码提交与镜像摘要；
- 操作前的检查；
- 关键操作；
- 实际结果与验证，并注明证据层次：仓库静态检查、容器行为、部署主机、Cloudflare 设置或外网；
- 证据位置；
- 恢复方法；
- 遗留问题与下一步。

## R001 停止旧实例

- **对应任务：** A0。
- **时间与执行者：** 2026-09-28 18:19–18:20（JST）。执行者为 AI 代理（Claude Code），已获操作者批准。
- **目标：** 本机 rootful Docker（default context）上 `authentik` 项目的 4 个容器。它们的 Compose 工作目录是开发 checkout `~/workspace/projects/authentik-ops`，即 `draft` 分支。
- **版本：** 通过 `docker inspect` 查得，摘要取前 12 位。

  | 容器 | 镜像 | 摘要 |
  |---|---|---|
  | `authentik-server-1`、`authentik-worker-1` | `ghcr.io/goauthentik/server:2026.8.2` | `sha256:ff8489a5af4f…` |
  | `authentik-postgresql-1` | `postgres:16-alpine` | `sha256:cf78e76683b9…` |
  | `authentik-caddy-1` | `caddy:2.11.4-alpine` | `sha256:5f5c8640aae0…` |

- **操作前检查（18:19）：**
  - 4 个容器已运行 5 天，其中 server、worker、postgresql 状态为 healthy；
  - Caddy 在 0.0.0.0 和 [::] 上发布了 80、443 端口；
  - 所有容器的重启策略都是 `unless-stopped`；
  - 没有备份定时器；
  - 操作者此前表示这个实例未投入使用，可以删除。
- **操作：**

  ```text
  docker stop authentik-caddy-1
  docker stop authentik-server-1 authentik-worker-1
  docker stop authentik-postgresql-1
  ```

- **结果：** 三条命令都返回成功，依次输出了 4 个容器的名字。
- **验证：** 停止后的只读核对被本会话的权限规则拦截，改由操作者在 2026-09-28 执行（证据层次：部署主机）。
  - `docker ps -a` 显示 4 个容器都已退出：caddy、server、postgresql 的退出码为 0；worker 的退出码为 137，说明它没能在默认的 10 秒内自行退出，被强制终止。实例不再使用，没有影响。
  - `ss -Htln '( sport = :80 or sport = :443 )'` 没有输出，80 和 443 端口已无监听。
  - 外部访问认证域名、数据卷是否仍在，这两项尚未核对。
- **证据位置：** 本记录摘录了命令及其输出，没有其他原始日志。
- **恢复方法：** `docker start authentik-postgresql-1 authentik-server-1 authentik-worker-1 authentik-caddy-1`。
- **遗留问题：**
  - 容器、网络、数据卷和本地文件留待 A4 删除。
  - worker 被强制终止，说明新的入口脚本停止容器时要留出更长的时间，已写入方案 0004。


## R002 准备入口的权限拆分与离线验证

- **对应任务：** A2；方案 0004 的管理员准备与用户初始化范围调整。
- **时间与执行者：** 2026-09-28，Codex；操作者在确认一次性管理员准备、其余工作交给专用用户后要求继续。
- **目标与范围：** `rebuild` 工作树中的两个准备入口、回归测试和对应资料；未执行主机准备或用户服务修改。
- **代码基线：** `41a6635aa511cb22b8fa39f12e48e4ea9a7b4d23`，在已有未提交改动上按范围修改，没有 stage、commit 或 push。
- **验证方法：** 在临时副本中运行回归测试；系统命令以模拟替代，文件操作仅作用于临时目录。
  覆盖有效/不足/冲突 ID 区间、未知状态不能通过、预检拒绝后无修改、目录授权失败与目标冲突、
  用户检查不写入、身份和 rootful 拒绝、配置保留及校验失败、服务重启失败后重跑。
- **结果与证据边界：** 准备入口 14 个回归测试及已有 6 个文档测试均通过；Shell 语法、Python 源码编译、文档检查及合成环境的 Compose 不变量通过。
  完整 `scripts/check.sh` 未在部署主机执行；未运行 Caddy 容器、真实 `--apply` 或用户服务。shellcheck 当前不可用，CI 未运行。
- **证据位置：**
  - 长期依据有两项：一是本记录中的测试范围、结果、验证缺口和代码哈希；二是仓库中可以重复执行的 `tests/test_prepare.py`。
  - 本机 `/tmp/authentik-user-prepare-z10z2ui0/` 中的临时副本、改动前哈希和验证日志只是辅助材料，重启后可能被清除。
- **恢复与遗留：** 未改变真实账号、映射、目录属主或服务，没有主机状态需要回滚。
  A3 仍须单独授权后，在真实用户会话验证新建、重跑与实际 rootless 服务状态。

- **本次准备实现的 SHA-256：**
  - `scripts/host-prepare.sh`：`ef6986562327a150edcc275ad4d4bef55f087ab64027c0de9332131cdb090020`。
  - `scripts/user-prepare.py`：`d894de81ce457bc4f9f7e7837c5985f0c6601510a45f3bb9dcac8570c27511f4`。
  - `tests/test_prepare.py`：`0c30d731991aa7c78097b596d36b4ea4a5b33787cc0b4b6ba02152b022c1668f`。
