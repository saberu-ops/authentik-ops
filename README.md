# authentik-ops

`auth.saberu.app` 认证入口的部署仓库：Docker Compose 运行 [authentik](https://goauthentik.io/)，
Caddy 在同一 Compose 项目中负责 TLS 与反向代理。面向负责这台认证服务的运维人员。

## 部署模型

部署主机上的 `/opt/authentik` 就是本仓库的 Git checkout。配置只通过 Git 修改；
密钥和运行时状态只存在于部署主机，被 `.gitignore` 排除，不进入仓库。

| 类别 | 内容 | 所在位置 |
|---|---|---|
| 配置（Git 跟踪） | `compose.yml`、`compose.override.yml`、`Caddyfile`、`systemd/`、`scripts/`、`.env.example` | 本仓库 |
| 密钥（主机本地） | `.env`、`.akadmin-initial-password`（首次安装后删除）、部署密钥 | 部署主机（见 [RUNBOOK.md「密钥管理与轮换」](RUNBOOK.md#密钥管理与轮换)） |
| 运行时状态（主机本地） | 数据库卷、`data/`、`certs/`、`custom-templates/`、`caddy-data/`、`caddy-config/`、`/var/log/caddy` | 部署主机 |
| 备份（主机本地） | 数据库 dump 与文件包 | 部署主机（见 [RUNBOOK.md「备份」](RUNBOOK.md#备份)） |

## 新主机快速开始

前置条件（Docker、DNS、端口、部署密钥）和完整步骤见
[RUNBOOK.md「新主机安装」](RUNBOOK.md#新主机安装)。核心只有两步：

```bash
sudo env GIT_SSH_COMMAND='ssh -i /root/.ssh/authentik-ops-deploy -o StrictHostKeyChecking=accept-new' \
  git clone git@github.com:saberu-ops/authentik-ops.git /opt/authentik
sudo /opt/authentik/scripts/bootstrap.sh --admin-email <管理员邮箱>
```

`bootstrap.sh` 会生成 `.env` 密钥与 akadmin 初始密码、创建目录、安装备份定时器、启动服务并检查健康状态。
迁移已有实例时不要生成新密钥，按 [「从备份恢复」](RUNBOOK.md#从备份恢复) 操作。

## 架构

`caddy` 接管宿主机 80/443，把公网请求转发到 `server`。服务、镜像版本和端口绑定以
`compose.yml`、`compose.override.yml` 为准。

- `caddy`：TLS 终止、反向代理，访问日志写入 `/var/log/caddy`
- `server`：authentik Web/API，只在 loopback 上发布端口
- `worker`：authentik 后台任务
- `postgresql`：authentik 数据库，不发布端口

## 安全边界

以下不变量由 `scripts/check.sh` 强制检查：

- `.env.example` 中的密钥留空：原样复制时 compose 拒绝启动，不会用公开值运行。
- 除 Caddy 外的端口只绑定 loopback：Docker 发布的端口会[绕过 ufw](https://docs.docker.com/engine/network/packet-filtering-firewalls/#docker-and-ufw)。
- worker 不以 root 运行、不挂载 `docker.sock`，authentik 因此不能自动部署 Docker outpost（见
  [RUNBOOK.md「Outpost 与 docker.sock」](RUNBOOK.md#outpost-与-dockersock)）。
- 挂载路径不越出部署目录（`/var/log/caddy` 除外）。

备份包含 `.env` 与数据库，只保存在本机且未加密（见 [「备份」](RUNBOOK.md#备份)）。

## 修改与检查

在开发机修改后运行静态检查，再提交：

```bash
scripts/check.sh
```

它解析 compose 配置并校验上述不变量，校验 `Caddyfile`、shell 脚本、systemd 单元、忽略规则，
并通过 `scripts/check_docs.py` 检查 agent 入口文件、文档链接与锚点、资料索引、方案与评审编号，以及 RUNBOOK 必需章节。
需要 Docker Compose 与 python3；`shellcheck`、`systemd-analyze` 存在时自动运行。
它会运行一次性离线容器校验 `Caddyfile`，在部署主机上运行即属于主机操作；`--no-containers` 跳过这一项。
不要在开发机的工作树里执行 `docker compose up`。

部署主机上的更新、升级、备份、恢复、密钥轮换与回滚见 [RUNBOOK.md](RUNBOOK.md)。

## 资料

项目资料索引见 [docs/README.md](docs/README.md)。当前的部署和运维方式以本文件和 [RUNBOOK.md](RUNBOOK.md) 为准。
