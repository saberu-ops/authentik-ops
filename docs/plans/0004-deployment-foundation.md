# 方案 0004：部署基础重建

- 状态：APPROVED（2026-09-28，操作者批准）
- 日期：2026-09-28
- 依据：[评审 0011](../reviews/0011-rebuild-direction-decisions.md)，记录各项决策的理由与来源
- 关联：[方案 0005](0005-app-security-baseline.md) 使用本方案的实例入口、`apply` 框架和备份恢复能力

## 目标

在现有主机上，用一个专用的无特权用户，以 rootless Docker 运行 authentik。完成后应满足：

- 公网只能经 Cloudflare Tunnel 进入，源站不开放任何入站端口；
- 代码（本仓库）、实例配置、数据和备份各自独立；
- 操作者用一个入口脚本完成初始化、检查、启动、`apply`、备份、恢复和更新，每个操作都能验证，失败后能恢复；
- 本地定制文件在更新代码后保留，并包含在备份中；
- 每晚生成加密的异地备份，每周自动验证一次能否恢复；
- 主机损坏时，可以在新主机上用对应版本的代码和异地备份恢复。

## 现状

以下为 `main` 41a6635 在 2026-09-28 的状态。

- **部署方式：**
  - 部署目录是 root 管理的 `/opt/authentik` checkout。
  - `sudo scripts/bootstrap.sh` 负责生成 `.env`、安装系统级备份定时器并启动服务。
  - Docker 以 rootful 模式运行。Caddy 占用公网 80/443 并自行申请证书；server 在本机回环地址发布 9000 和 9443 端口。
- **备份：** `scripts/backup-postgres.sh` 由 root 的定时器每日运行，备份保存在本机 `/var/backups/authentik`，保留 7 天，不加密，也没有异地副本。
- **镜像：** 按标签引用（authentik 2026.8.2、`postgres:16-alpine`、`caddy:2.11.4-alpine`），标签可能被重新指向其他镜像。
- **已知问题：** authentik 默认信任所有私网网段发来的转发头。经本机回环端口进入的连接，其来源显示为 Docker 网关，所以本机任何进程都能伪造客户端 IP（依据 2026.8.2 源码推断）。
- **本机状态：**
  - 只读快照见评审 0011。
  - 旧实例从未投入使用，已于 2026-09-28 执行停止，停止后的状态尚待核对，见[操作记录 R001](../implementation/records.md#r001-停止旧实例)。

## 设计

### 运行身份与主机准备

**专用用户**

- 默认名为 `authentik`，初始化前可以改名。
- 不设密码，只能通过 SSH 密钥或 `machinectl shell` 登录。
- 不加入 sudo 组和 docker 组，使用独立的 subuid/subgid 段，并开启 linger。
- rootless Docker 作为该用户的 systemd 用户服务运行。
- 不能用 `su` 或 `sudo -u` 切换到这个用户：这样切换后没有用户级 systemd 会话，`systemctl --user` 无法工作。

**管理员准备与用户初始化**

2026-09-28 操作者批准的范围调整：一次性管理员准备由 root 完成，其余初始化和日常运维交给专用用户。操作者审核评审 0012 时确认了这一权限设计。

主机准备和用户初始化分成两个入口：

| 入口 | 执行身份 | 负责的工作 |
|---|---|---|
| `scripts/host-prepare.sh` | 管理员以 root 执行 `--apply`；普通用户可以运行检查 | 系统依赖、创建账号并分配 ID、linger、服务根目录 |
| `scripts/user-prepare.py` | 专用用户 | 服务目录内部、Docker 用户配置、rootless 用户服务及结果核实 |

两个入口默认只检查并打印结果，加 `--apply` 才执行操作。

**管理员入口 `host-prepare.sh --apply`**

先完成全部检查；全部兼容，且没有未核实项时，依次执行：

1. 用 apt 安装缺少的 `uidmap`、`systemd-container`、`restic`、`python3-yaml`、`python3-jsonschema`。
2. 账号不存在时，用 Debian 的 `useradd` 创建账号，包括：
   - 家目录 `/home/<用户>`；
   - bash 作为登录 shell；
   - 同名主组；
   - 锁定的密码；
   - 由 `useradd` 分配的 65536 个 subuid 和 65536 个 subgid。
   
   创建后重新检查一遍。
3. 为该用户开启 linger。
4. 服务根目录（默认 `/srv/authentik`）不存在时，先在其直接父目录中建一个临时目录，设好属主和 0750 权限，再原子改名为服务根目录。也可以用 `--no-service-dir` 跳过这一步，改由专用用户在自己的家目录中创建。
5. 复查全部项目。

遇到以下任一情况，在任何修改之前报告并停止：

- cgroup v2 或非特权用户命名空间不可用，或用户服务没有委派 memory、pids、cpu 三个控制器。
- 缺少 Docker 软件包（`docker-ce`、`docker-ce-rootless-extras`、`docker-compose-plugin`）。Docker 由管理员单独安装。
- 已有账号存在以下任一问题：
  - 不是普通 UID；
  - 属于 sudo 组或 docker 组；
  - 家目录不存在，或不属于该账号；
  - 登录 shell 不是 `/bin/bash`；
  - 密码没有锁定；
  - subuid 或 subgid 格式无效、与其他账号重叠，或合计不足 65536。
- 需要新建账号，但以下条件有一项不满足：
  - `/home` 只由 root 控制；
  - `/etc/subuid` 和 `/etc/subgid` 已存在；
  - 没有同名组；
  - 目标家目录尚未存在。
- 服务根目录有以下任一问题：
  - 直接父目录不存在；
  - 父目录或任一上级目录不属于 root，或者组和其他用户可写；
  - 服务根目录本身是符号链接；
  - 服务根目录已被其他账号占用。
- 执行 `--apply` 的脚本副本不属于 root，或者组和其他用户可写。管理员应先把审阅过的脚本安装到 root 持有的位置，再从那里运行。

退出码：

- 0：主机准备已核实；
- 1：有缺失项或不兼容；
- 2：有未核实项，例如普通用户无法读取密码锁定状态。

**用户入口 `user-prepare.py --apply`**

先核对以下各项；全部通过后，依次执行：

- 核对的内容：
  - 当前用户身份；
  - 服务目录和 `daemon.json`；
  - 用户级 systemd 会话；
  - Docker 官方的前提检查 `dockerd-rootless-setuptool.sh check --force`；
  - 已在运行的 Docker 用户服务报告为 rootless。
- 执行的操作：
  1. 在 `$XDG_CONFIG_HOME/docker/daemon.json`（默认 `~/.config/docker/daemon.json`）中合并写入 `log-driver: local`，保留其他字段；先经 `dockerd --validate` 校验，再原子替换。
  2. 创建服务目录内部的 `code/`、`instance/{settings,local,state}/`、`backup/`，并把服务根目录权限设为 0750。
  3. 缺少 Docker 用户服务时，运行官方的 `dockerd-rootless-setuptool.sh install --force`。
  4. 启用并启动 Docker 用户服务。如果配置中或实际运行的日志驱动不是 local，就重启该服务；重启会影响该用户已有的容器。
  5. 通过该用户自己的 Unix socket 复查，确认 rootless、日志驱动 local、systemd 与 cgroup v2。

遇到以下任一情况，在修改之前报告并停止：

- 以 root 运行，或者当前用户不是指定的专用用户；
- 当前用户属于 sudo 组或 docker 组；
- `HOME` 与该账号的家目录不一致；
- 目录或 `daemon.json` 不属于当前用户，或者是符号链接；
- `daemon.json` 无法解析；
- 缺少所需命令、`XDG_RUNTIME_DIR` 或用户级 systemd 会话；
- 官方前提检查失败；
- 正在运行的 Docker 不是 rootless。

写入配置之后，如果启动或重启失败，已写入的文件会保留；修正原因后重跑，脚本会按磁盘上的配置和实际运行状态继续执行。

失败时的提示信息，只包含该命令可以安全输出的原因：

- 官方前提检查：它输出的 `[ERROR]`、`[WARNING]` 行；
- 配置校验：出错的字段名和类型，不输出字段的值；
- 用户服务启动或重启：服务的状态字段 ActiveState、SubState、Result、ExecMainStatus；详细日志请用 `journalctl --user -u docker.service` 查看；
- 连接 Docker：连接失败或权限被拒的类别。

**执行顺序与完成标准**

1. 管理员先审阅检查模式的输出，再单独授权执行 `--apply`。
2. 通过 SSH 或 `machinectl shell` 登录专用用户（`su` 和 `sudo -u` 不会建立用户级 systemd 会话），先审阅用户入口的检查结果，再执行 `--apply`。
3. 完成标准：管理员检查返回 0；用户检查返回 0，并报告 rootless、local、systemd 与 cgroup v2。

执行进度记录在实施清单的 A3。账号创建和真实的用户服务生命周期，在 A3 单独授权后验证；验证通过后，再把这套操作写入 RUNBOOK。

不接触主机的回归测试：

```bash
python3 -B -m unittest discover -s tests -p test_prepare.py -v
```

这组测试用临时目录和模拟的系统命令，只验证脚本的判断与失败处理。

**rootless 带来的取舍**

- 没有 AppArmor，seccomp 仍然生效。
- 缺少 cgroup 委派时，资源限制只给出警告就被忽略，所以部署后要核对实际生效的值。
- 禁止使用 `network_mode: host`。
- 镜像与 rootful 守护进程分开存储。

### 目录与数据

```text
<服务目录>/              专用用户所有，0750
├── code/                本仓库的 checkout，固定到发布提交，可整体替换
├── instance/            实例配置，独立于 Git
│   ├── .env             参数与机密，0600
│   ├── settings/        方案 0005 的安全设置
│   ├── local/           本地定制
│   └── state/           生成物与生效记录，可重建
└── backup/              restic 缓存、本地数据库副本与临时文件，不作长期保存
Docker 命名卷：database（PostgreSQL 数据）、data（authentik /data）
```

- **命名卷：** 声明为 `external: true`，只在初始化或恢复时显式创建，日常启动不会自动建出空卷。
- **代码目录：** 不存放任何实例文件。更换代码版本后，用同一个实例目录重建容器。
- **`/certs`：** 不再挂载。官方说明它只用于证书发现，属于可选项。
- **`/templates`：** 只在 `local/templates/` 存在时挂载，因为它可以覆盖任意页面模板。
- **`/data/user_settings.py`：** authentik 启动时会执行这个文件，而 `/data` 是应用自己可以写入的目录。因此用代码仓库里的一个只读空文件覆盖挂载到这个位置，使它无法被植入代码。

### 网络与入口

```text
浏览器 → Cloudflare 边缘 ⇠(出站隧道)⇠ cloudflared ×2 → caddy:80 → server:9000
```

**网络划分**

- `edge`：cloudflared 与 caddy。
- `app`：caddy、server、worker。允许出站，供 HIBP、Turnstile 验证、邮件等使用。
- `db`：server、worker 与 postgresql，设为 `internal: true`。
- `edge` 和 `app` 使用固定子网，caddy 与 cloudflared 使用固定地址。

**cloudflared**

- 使用一个在 Cloudflare 面板中远程管理的 Tunnel，专供本实例。它不与本机其他 cloudflared 共用，只有生产环境的两个副本连接它。
- 同一个 Tunnel 的所有连接器会分摊流量，所以测试一律另建 Tunnel，测试结束后删除。
- 令牌只经 `.env` 以环境变量传入，不出现在命令行上。
- 运行两个副本，镜像固定摘要，健康检查使用它的就绪端点。

**Caddy**

- 只监听内部 HTTP，只信任 cloudflared 的固定地址。
- 把 `CF-Connecting-IP` 写入 `X-Forwarded-For`，强制 `X-Forwarded-Proto: https`，删除其他可被客户端控制的转发头。
- 访问日志写到标准输出。

**authentik**

- `AUTHENTIK_LISTEN__TRUSTED_PROXY_CIDRS` 只包含 Caddy 的固定地址。

**对外开放的三种状态**

| 状态 | 含义 | 进入条件 |
|---|---|---|
| 未路由 | Tunnel 没有配置公网主机名，外部无法访问 | 初始状态 |
| 仅操作者 | 公网主机名已路由；Cloudflare 规则只放行操作者（按来源 IP，或使用 Cloudflare Access）；外部探测只放行 `/-/health/ready/` | 私有实例运行正常；Tunnel 的连接器只有生产的两个副本；已从外部证明其他人的访问会被拦截 |
| 公开 | 解除访问限制 | 方案 0004 和方案 0005 的上线门槛全部通过；再次核对连接器列表 |

WebAuthn 要求使用正式域名和有效证书，所以涉及登录的开发与验收，都在“仅操作者”状态下进行。进入这个状态之前，只做不需要登录的工作。

**宿主端口与应急入口**

- 不发布任何宿主端口。
- Compose profile `breakglass` 在 `127.0.0.1` 上发布 authentik server 的 9000 端口（不经过 Caddy），只在应急时通过 SSH 端口转发使用，用完即关闭。
- 这个端口的来源不在可信代理范围内，所以请求带来的转发头不会被采信。
- 这个入口只用于：用恢复链接登录后，在管理界面修复配置、撤销设备。
- WebAuthn 在 localhost 上无法使用生产域名，所以登记新设备必须经公网主机名进行。

### 配置与机密

- `.env` 保存两类内容，文件权限 0600，属主为专用用户：
  - 参数：域名、卷名、网络地址段等；
  - 机密：数据库密码、`AUTHENTIK_SECRET_KEY`、Tunnel 令牌、SMTP 密码、Turnstile 密钥，以及可以留空的 MaxMind 账号与许可密钥。
- **GeoIP 数据：**
  - MaxMind 配置留空时，不启动 geoipupdate，使用镜像自带的 GeoIP 库。
  - 填写后，由 Compose profile 中的官方 geoipupdate 容器把数据下载到独立的卷。
  - `check` 确认下载的文件有效之后，authentik 才改用这份数据；在此之前继续使用自带库。这样不会出现数据缺失的空档。
- 镜像引用和摘要只由代码仓库中的 Compose 文件维护，实例不能覆盖。更换版本，就是更换代码版本。
- Compose 只把每个服务需要的变量写进该服务的 `environment`，不再把整个 `.env` 通过 `env_file` 交给所有服务。
- 密钥只在初始化时生成一次。如果命名卷已存在而缺少 `.env`，拒绝启动，并转入恢复流程。
- `.env` 和 restic 密码另存离线副本，由操作者保管。
- 首次初始化使用 `AUTHENTIK_BOOTSTRAP_PASSWORD_HASH`。何时删除 BOOTSTRAP 值由方案 0005 规定。

### 本地定制

**三个定制入口**

- `instance/local/compose.local.yml`：可选。存在时，入口脚本在命令中追加 `-f` 加载它。
- `instance/local/caddy/*.caddy`：操作者在这里编辑。Caddy 不直接导入这个目录，而是导入 `instance/state/caddy/` 中已通过断言的副本；目录为空时不报错。
- `instance/local/templates/`：存在时挂载到 `/templates`。

入口脚本只读取、从不改写这些文件；它们随实例目录一起备份。

**Compose 覆盖文件的允许范围**

覆盖文件只能设置以下内容，其他任何键都会被拒绝：

- 已有服务的资源限制：`mem_limit`、`cpus`、`pids_limit`；
- `logging` 和 `labels`；
- 允许清单中的非安全环境变量，例如日志级别、工作进程数。允许清单本身是安全不变量，由代码维护。

明确不允许的包括：新增服务、网络、卷、端口或挂载；设置 `cap_add`、`security_opt`、`devices`、`privileged`、`user`、`userns_mode`、`network_mode`、`pid` 或 `ipc`。

**最终 Compose 配置的正向断言**

`check` 每次执行时，都对合并后的最终配置确认以下各项：

- authentik 的可信代理只包含 Caddy 的地址；内嵌 outpost 已关闭；
- `db` 网络上只有 server、worker 和 postgresql；
- `database` 和 `data` 两个卷只挂载到指定的服务；
- 没有特权、额外能力、设备或放宽的 seccomp；
- 没有使用 host 或其他服务的网络、PID、IPC 命名空间；
- 所有镜像都带摘要；
- 除 `breakglass` 外没有宿主端口；
- `/data/user_settings.py` 是只读的空文件挂载。

**Caddy 本地片段**

- 本地片段先复制到候选目录，与主 Caddyfile 一起检查：
  - 通过 `caddy validate`；
  - 转换成 JSON（`caddy adapt`）后确认以下各项：
    - 可信代理只有 cloudflared 的地址；
    - 客户端 IP 只取自 `CF-Connecting-IP`；
    - 只有一个转发到 `server:9000` 的上游，并且转发头的改写与设计一致；
    - 路径屏蔽规则位于转发之前；
    - 安全响应头齐全。
- 检查全部通过后，才原子替换 `instance/state/caddy/` 并重载 Caddy。
- 检查不通过时：
  - Caddy 继续使用上次通过的副本，并发出告警；
  - 容器重启后仍然读取这份副本，所以未通过检查的片段无论何时都不会生效。
- `check` 还会经内部网络实际请求 Caddy，核对响应头和路径屏蔽确实生效。
- Compose 覆盖文件只在重建容器时生效，而 `up`、`update`、`restore` 都会先做断言。容器崩溃后自动重启时，沿用的是已有的容器定义，不会读入新的覆盖文件。

### 入口脚本

入口脚本暂定为 `scripts/authentik-ops`，名称在实现时确定。它接收 `--instance <目录>`，所有操作都在实例锁（`flock`）内执行。

| 子命令 | 作用 |
|---|---|
| `init` | 生成 `.env`，创建命名卷和网络，首次启动；此时处于“未路由”状态 |
| `check` | 只读检查，不修改任何东西：确认 rootless；断言最终的 Compose 与 Caddy 配置；检查机密和卷可读、资源限制实际生效、GeoIP 库存在；执行方案 0005 的安全检查项；报告漂移 |
| `up`、`down` | 启动或停止 |
| `apply` | 执行方案 0005 的配置生效；另由定时任务每日重新执行一次当前生效版本，用于纠正漂移 |
| `backup` | 不停机备份到 restic；加 `--local` 时只在本机保存一份数据库导出，供 `apply` 前使用 |
| `restore` | 从备份恢复；加 `--drill` 时为演练目标生成专用 `.env`（见下文“验证”），不接入任何 Tunnel |
| `update` | 先备份，再切换代码版本（包括镜像摘要）并启动，最后检查健康状态并回读受管对象；之后由操作者用硬件密钥登录验证一次 |

`down`、`update`、`restore` 停止容器时，给 worker 留出足够的停止时间，例如 60 秒；由 Compose 的 `stop_grace_period` 设置。旧实例停止时，worker 在默认的 10 秒内没有退出而被强制终止（见 R001）。

**检查失败时各自阻止什么**

| 失败类型 | 阻止的操作 | 不阻止的操作 |
|---|---|---|
| Compose 或 Caddy 配置断言失败 | 启动、`update`、`restore`；新的 Caddy 片段生效（继续使用上次通过的副本，并告警） | 每日重新执行蓝图 |
| 设置校验失败，或偏离已过期（方案 0005） | 生效新的设置版本 | 每日重新执行当前已生效的版本 |
| 方案 0005 的安全检查项失败，例如管理员设备没有型号、缺少 Turnstile 密钥、日常账号持有管理角色 | 切换到“公开”；会发出告警 | 启动、`apply` |
| 检测到漂移 | 无，只报告 | 由每日重新执行纠正 |

### 备份与恢复

**每晚备份**

由用户级定时器执行，服务不停：

1. 用 `docker compose exec -T` 运行 `pg_dump -Fc`，输出直接交给 `restic backup --stdin-from-command`。导出失败时不会生成快照。
2. 在容器内对 `data` 卷执行 `tar`，输出交给 restic。
3. 备份实例目录，包括 `.env`、`settings` 和 `local`。
4. 写入一份清单：代码提交、镜像摘要、PostgreSQL 版本和时间。
5. 向外部心跳服务报告开始和结果。

**为什么不停写**

`pg_dump` 在应用并发写入时导出的也是一致快照。`/data` 里只有很少变化的上传文件；如果两步之间正好有文件变化，每周的恢复验证会发现。

**备份仓库**

- 位于异地，且只能追加。首选第二台主机上的 restic rest-server，以 `--append-only` 模式运行。
- 清理旧备份只在另一台机器上进行：执行 `restic forget --keep-within <期限>` 和 prune。按 restic 对只可追加仓库的建议，只按时间保留。
- 本机持有的凭据无法删除备份。
- 如果只能使用对象存储，改用支持 Object Lock 的方案，并重新评估备份工具。

**验证**

- **每周：** 自动把最新的数据库备份恢复到一个临时 PostgreSQL 容器，并执行基本查询。
- **每月：** 执行一次 `restic check --read-data-subset`。
- **每季度：** 在演练目标上按 RUNBOOK 执行 `restore --drill` 完整恢复，并测量 RPO 与 RTO。
  - 演练目标可以是本机专用用户下的另一个项目，也可以是另一台主机。
  - `--drill` 生成演练专用的 `.env`：
    - 去掉 Tunnel 令牌；
    - 项目名、卷名和网络地址段都换成演练专用的值，避免与生产冲突；
    - 缺少令牌时 cloudflared 拒绝启动。
  - 演练实例不接入 Tunnel，因此无法用 WebAuthn 登录。改为经 API 或 `ak` 回读账号、WebAuthn 设备记录、策略和上传文件，再经 `breakglass` 用恢复链接登录，确认管理界面可用。
  - 演练结束后销毁演练实例，包括容器、卷和演练用的 `.env`。
- **上线前一次切换演练（仍在“仅操作者”状态）：** 验证完整的灾难恢复路径。
  1. 用 `restore --drill` 恢复出演练实例；
  2. 停止生产的 cloudflared，确认 Tunnel 的连接器列表为空；
  3. 为演练实例显式提供生产令牌，并启动它的 cloudflared；确认连接器列表只有演练实例的副本；
  4. 用硬件密钥登录验证；
  5. 停止演练实例的 cloudflared，重新启动生产的 cloudflared；确认连接器列表只有生产的两个副本；
  6. 销毁演练实例。

**恢复目标**

- 初始目标：RPO 24 小时（每次更新前另做一次备份），RTO 4 小时。
- 由操作者确认或调整。

**新主机恢复步骤**

1. 完成管理员准备和专用用户初始化；
2. 获取对应提交的代码；
3. 取回 `.env`；
4. 确认原实例已经停止，或者先在 Cloudflare 轮换 Tunnel 令牌并断开旧连接；
5. 执行 `restore`；
6. 核对连接器列表只有新实例的副本；
7. 核对数据与配置；
8. 开放。

备份、恢复和更新的操作步骤，在对应能力验证通过时写入 RUNBOOK。演练按 RUNBOOK 执行，以此同时验证文档本身。

### 版本、更新与供应链

- **固定摘要：** 所有镜像（authentik、PostgreSQL、Caddy、cloudflared）都在 Compose 中按 `标签@sha256:摘要` 固定。更新通过仓库变更完成，可以由 Dependabot 自动生成 PR。
- **校验：**
  - 在更新 PR 的 CI 中，按固定的摘要校验 authentik 镜像的来源证明，并限定签名工作流：

    ```text
    gh attestation verify oci://ghcr.io/goauthentik/server@sha256:<摘要> \
      --repo goauthentik/authentik \
      --signer-workflow goauthentik/authentik/.github/workflows/release-publish.yml
    ```

  - 校验失败时，不合并该 PR，也不更新。
  - `gh` 是否接受摘要形式的引用，在关键实测中确认。
  - PostgreSQL 和 Caddy 的官方镜像目前没有签名，只能固定摘要。
- **升级路径：** 按官方要求，不跳过大版本，先升到当前系列的最新补丁。PostgreSQL 大版本变化由 `update` 拒绝，另行设计。
- **回退：** 官方不支持降级。数据库迁移之后要回退，只能用更新前的备份加旧版本代码恢复。
- **起始版本：** authentik 2026.8.3。官方只为 2026.5.x 和 2026.8.x 提供安全修复。
- **补丁时限（参考 CISA BOD 26-04）：**
  - 已被利用、或可导致完全控制的公网漏洞：72 小时内；
  - 其他安全更新：7 天内；
  - PostgreSQL 小版本：每季度。

### 监控与日志

- 从外部探测公网的 `/-/health/ready/`，它同时检查数据库连接。“仅操作者”规则为这个路径放行。
- 以下定时任务都在用户级运行，并向本机以外的心跳服务报到：
  - 备份；
  - 恢复验证；
  - 每日 `apply`；
  - 磁盘检查（使用率超过 75% 告警）；
  - 内存检查。
- rootless 守护进程使用 `local` 日志驱动，因为默认的 json-file 驱动不会轮转日志。
- 不开放 9300 指标端口。

### 旧实例与旧流程

- 旧的 rootful 实例从未投入使用，不迁移数据。在阶段 1 盘点容器、网络、卷和本地文件后删除，删除需单独批准。
- 开发 checkout 不再作为部署目录。
- main 上的 root 版 `bootstrap.sh`、系统级备份单元，以及 RUNBOOK 中对应的旧段落，在阶段 7 移除。

## 范围

**本期交付：**

- 主机准备脚本；
- 重写 Compose；
- 入口脚本及其检查；
- restic 备份、恢复与自动验证；
- 监控心跳；
- CI：GitHub Actions 运行静态检查、测试和镜像来源校验；
- 按能力逐项更新 README、RUNBOOK 和验证资料。

**不在本期：**

- 多实例与高可用；
- PostgreSQL 大版本升级；
- 数据库 TLS：数据库在同机的内部网络上，以后单独规划；
- SSH、防火墙、1Panel 等主机加固；
- 磁盘加密；
- 升级 rootful Docker。

## 前提与待实测

| 事项 | 如果不成立 |
|---|---|
| rootful 守护进程运行时，`dockerd-rootless-setuptool.sh install --force` 能正常完成 | 按 Docker 文档排查后重试 |
| cloudflared 经 rootless 网络（slirp4netns）能用 QUIC 连接，使用单独的测试 Tunnel 验证 | 改用 `--protocol http2` |
| authentik（UID 1000）能读写命名卷 | 调整卷的初始化方式 |
| 资源限制实际生效 | 补齐 cgroup 委派后重测 |
| 在 `/data/user_settings.py` 挂载只读空文件，不影响 authentik 启动 | 改为每次启动前用辅助容器检查该文件不存在 |
| `caddy adapt` 的输出足以断言可信代理、客户端 IP 来源和转发头改写 | 改为限定本地片段只能使用的指令 |
| 能通过 `breakglass` 入口用恢复链接登录，并在管理界面修复配置 | 调整应急步骤 |
| 固定版本的 cloudflared 提供就绪检查命令 | 改用指标端口做健康检查 |
| `gh attestation verify` 接受摘要形式的引用 | 改为先校验标签，再比对摘要是否一致 |
| 启用 geoipupdate 时，新数据可以在不中断 GeoIP 的情况下替换自带库 | 改为在维护窗口内切换：先下载并校验，再重建 server 与 worker |
| 操作者提供异地备份目标和心跳服务 | 缺失期间备份只在本地，不宣称具备灾备能力 |

## 验收

| 条件与操作 | 应看到的结果 | 证明方式 |
|---|---|---|
| 从外网检查主机端口 | 没有任何 authentik 相关端口在监听 | 外部端口检查、`ss` |
| 处于“仅操作者”状态时，从其他来源访问 | 被 Cloudflare 拦截，只有健康检查路径可以访问 | 外部请求记录 |
| 进入“仅操作者”和“公开”状态前，查看 Tunnel 的连接器 | 只有生产的两个副本 | Cloudflare 连接器列表 |
| 从非 cloudflared 地址向 Caddy 发送伪造的 `CF-Connecting-IP` 或 `X-Forwarded-For` | authentik 记录的客户端 IP 不受影响 | 事件记录 |
| 经 Tunnel 访问 | authentik 记录的客户端 IP 等于访问者的 IP | 事件记录 |
| 以专用用户运行 | `docker info` 显示 rootless；容器进程在宿主机上属于专用用户的 subuid | `docker info`、`ps` |
| 让入口脚本连接 rootful 守护进程 | 拒绝执行 | 测试 |
| 设置内存和 PID 限制 | 实际生效的值与设置一致 | `docker inspect` |
| 更换代码版本 | `local/` 下的文件不变且仍然生效 | 测试 |
| 覆盖文件设置允许范围以外的键，例如 `cap_add`、可信代理变量、新增服务 | 被 `check` 拒绝 | 测试 |
| 本地 Caddy 片段修改可信代理、客户端 IP 来源或转发头 | 被 `check` 拒绝；每日 `apply` 和容器重启后，Caddy 仍使用上次通过的副本，并发出告警 | 测试 |
| 某个镜像未固定摘要 | 被 `check` 拒绝 | 测试 |
| MaxMind 配置留空；之后填写 | 留空时不启动 geoipupdate，使用自带库；填写后数据开始更新，切换期间 GeoIP 查询不中断 | 测试 |
| 在容器内写入 `/data/user_settings.py` | 失败，因为该路径只读 | 测试 |
| 安全检查项失败 | 服务照常启动，每日 `apply` 照常执行，但不能切换到“公开” | 测试 |
| 重复执行 `init` | 密钥不变 | 测试 |
| 命名卷存在而缺少 `.env` | 拒绝启动 | 测试 |
| 数据库导出失败 | 不生成快照，心跳报告失败 | 故障注入 |
| 在演练目标上按 RUNBOOK 执行 `restore --drill` | 账号、WebAuthn 设备记录、策略和上传文件一致；恢复链接可以登录管理界面；耗时和数据时间点满足 RPO/RTO | 演练记录 |
| 启动演练实例 | 不接入任何 Tunnel，生产的连接器列表不变；演练结束后实例已销毁 | 连接器列表、演练记录 |
| 上线前的切换演练 | 每一步的连接器列表都符合预期；演练实例接管正式主机名后，硬件密钥登录成功；切回后只有生产的两个副本，演练实例已销毁 | 连接器列表、演练记录 |
| 用本机凭据删除备份 | 删除失败 | 测试 |
| 执行补丁更新 | 更新成功，健康检查和回读通过，操作者登录验证通过 | 演练记录 |
| 模拟更新失败 | 用更新前的备份恢复成功 | 演练记录 |
| 镜像来源证明校验失败 | 更新 PR 的 CI 失败，不执行更新 | CI 记录 |
| 停止备份任务，或让磁盘使用率超过阈值 | 外部服务发出告警 | 测试 |
| 推送分支 | CI 运行静态检查和测试 | CI 记录 |

## 实施阶段

| 阶段 | 产出 | 进入下一阶段的条件 |
|---|---|---|
| 1. 主机准备 | 管理员与用户准备入口；专用用户和 rootless Docker 就绪；旧实例已删除 | 检查项全部通过 |
| 2. 关键实测 | 两份方案中不需要登录的实测结论；测试项目和测试 Tunnel 已删除 | 每项都有结论；需要调整的设计已写回方案 |
| 3. 实例基础 | Compose、入口脚本、检查、CI | 私有实例在运行，处于“未路由”状态 |
| 4. 仅操作者路由 | 受限的公网主机名，转发头与可信代理 | 连接器只有生产副本；已从外部证明其他人被拦截；真实 IP 验证通过 |
| 5. 运维能力 | 备份、恢复、更新与监控；RUNBOOK 对应段落 | 首次季度式演练满足 RPO/RTO，并且按 RUNBOOK 完成 |
| 6. 上线 | 与方案 0005 联合验收，完成切换演练，然后切换到“公开” | 两个方案的上线门槛全部满足 |
| 7. 收尾 | 移除旧流程；形成结果记录（changelog） | 文档与实际行为一致 |

具体任务和进度见[实施清单](../implementation/checklist.md)。

## 风险

- **Cloudflare 或 Tunnel 故障：** 服务不可用。现状同样依赖 Cloudflare。
  - 故障期间，只能经 `breakglass` 修复配置，不能登记新设备。
- **Tunnel 令牌泄露：** 攻击者可以接入自己的连接器，接收真实流量。
  - 令牌只存放在 `.env` 中。
  - 连接器列表在切换状态时核对，并纳入月度复核。
  - 一旦怀疑泄露，在 Cloudflare 轮换令牌并断开现有连接。
- **主机被攻破：** 加密密钥在本机，攻击者能读取备份；但仓库只能追加，历史备份不会被删除。
- **rootless 的网络与权限问题：** 在阶段 2 实测。
