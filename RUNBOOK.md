# Authentik / Caddy 运维手册

除特别说明外，命令都在部署主机上执行。部署目录默认是 `/opt/authentik`（本仓库的 Git checkout，属主 root）。

## 基本信息

| 事实 | 以此为准 |
|---|---|
| 公网域名 | `Caddyfile` 的站点地址 |
| Compose 项目名（决定数据库卷名 `<项目名>_database`） | `compose.yml` 的 `name:` |
| 服务、镜像版本、端口绑定 | `compose.yml`、`compose.override.yml` |
| 备份触发时间 | `systemd/authentik-backup.timer` |
| 备份内容、位置、保留天数 | `scripts/backup-postgres.sh` |
| 主机本地状态与密钥 | [README.md「部署模型」](README.md#部署模型) |

在部署目录中不要执行 `git clean -x`、`git clean -X` 或 `git stash --all`：被忽略的 `.env` 和数据目录会被删除或移走。

## 检查

```bash
cd /opt/authentik
sudo docker compose ps
sudo ss -ltnup '( sport = :80 or sport = :443 )'
sudo docker compose logs --tail=100 caddy server worker
curl -fsS https://auth.saberu.app/-/health/live/
curl -fsS https://auth.saberu.app/-/health/ready/
systemctl list-timers authentik-backup.timer
```

健康：所有服务为 running（带健康检查的为 healthy），两个健康检查返回 HTTP 200，定时器有下次触发时间。

## 新主机安装

### 前置条件

- 满足 [authentik Docker Compose 安装要求](https://docs.goauthentik.io/install-config/install/docker-compose/)（CPU、内存、Compose v2）
- [Docker Engine](https://docs.docker.com/engine/install/)、`git`、`curl`
- `auth.saberu.app` 的 A/AAAA 记录已指向新主机
- 入站放行 `80/tcp`、`443/tcp`、`443/udp`，本机没有其他进程监听 80/443
- 若要迁移已有实例：改按「从备份恢复」操作，不要在新主机生成新密钥

### 步骤

1. 创建 root 持有的只读部署密钥，并添加到 GitHub 仓库 `saberu-ops/authentik-ops` 的 Settings → Deploy keys（不勾选写权限）：

   ```bash
   sudo ssh-keygen -t ed25519 -N '' -C "authentik-ops deploy $(hostname -s)" -f /root/.ssh/authentik-ops-deploy
   sudo cat /root/.ssh/authentik-ops-deploy.pub
   ```

2. 克隆到部署目录：

   ```bash
   sudo env GIT_SSH_COMMAND='ssh -i /root/.ssh/authentik-ops-deploy -o StrictHostKeyChecking=accept-new' \
     git clone git@github.com:saberu-ops/authentik-ops.git /opt/authentik
   sudo git -C /opt/authentik config core.sshCommand 'ssh -i /root/.ssh/authentik-ops-deploy'
   ```

3. 运行 bootstrap（执行内容见 `scripts/bootstrap.sh` 头部与各步骤注释）：

   ```bash
   sudo /opt/authentik/scripts/bootstrap.sh --admin-email <管理员邮箱>
   ```

   脚本可重复执行，不覆盖已存在的 `.env`、目录和 systemd 单元；生成新 `.env` 时同时生成 akadmin 初始密码。

### 验证与收尾

1. 脚本末尾 `docker compose ps` 全部 running/healthy，且输出 `/-/health/ready/ 正常`。
2. 用 `akadmin` 和 `/opt/authentik/.akadmin-initial-password` 中的密码登录 `https://auth.saberu.app/`，
   把密码存入密码管理器（或登录后修改），然后删除明文：

   ```bash
   sudo shred -u /opt/authentik/.akadmin-initial-password
   ```

3. 手动运行一次备份，确认输出 `backup ok`：

   ```bash
   sudo systemctl start authentik-backup.service
   sudo journalctl -u authentik-backup.service -n 20 --no-pager
   ```

4. 需要发送邮件时，按「密钥管理与轮换」中的 SMTP 步骤写入 `AUTHENTIK_EMAIL__*` 并测试。

### 失败处理

- 前置检查失败（缺少 Docker、80/443 被占用、`.env` 不完整）：脚本在启动服务前退出，修正后重跑。
- 提示“数据库卷已存在，新生成的密钥与之不匹配”：主机上已有数据，按「从备份恢复」找回原 `.env`，不要删除卷。
- 启动或健康检查失败：查看 `sudo docker compose logs --tail=200 caddy server worker postgresql`，
  常见原因是 DNS 未指向本机或防火墙未放行（Caddy 无法签发证书）。修正后重跑脚本。
- 全新主机安装失败且确认数据库里没有任何需要保留的数据时，才可以彻底重来：

  ```bash
  cd /opt/authentik
  sudo docker compose down --volumes
  sudo rm -f .env .akadmin-initial-password
  sudo scripts/bootstrap.sh --admin-email <管理员邮箱>
  ```

## 修改配置

配置只在 Git 中修改，不在部署主机上直接编辑被跟踪的文件。

1. 在开发机修改，运行 `scripts/check.sh`，提交并推送到 `main`。
2. 在部署主机拉取：

   ```bash
   cd /opt/authentik
   sudo git status --short        # 不应有被跟踪文件的改动；有则先停下，把改动收回仓库
   sudo git fetch
   sudo git diff --stat HEAD origin/main
   sudo git pull --ff-only
   sudo docker compose config --quiet
   ```

3. 按改动类型应用：

   | 改动 | 命令 |
   |---|---|
   | `compose.yml` / `compose.override.yml` | `sudo docker compose up -d --wait` |
   | `Caddyfile` | `sudo docker compose up -d --force-recreate caddy` |
   | `systemd/` | `sudo install -m 0644 systemd/authentik-backup.service systemd/authentik-backup.timer /etc/systemd/system/ && sudo systemctl daemon-reload` |
   | `.env`（主机本地） | `sudo docker compose up -d --wait` |

   `Caddyfile` 以单文件方式挂载，`git pull` 写入新文件会更换 inode，运行中的容器仍看到旧内容，
   因此不能用 `caddy reload`，必须重建 caddy 容器（入口会中断数秒）。

4. 按「检查」确认。

执行 `up -d` 前可预判哪些服务会被重建：两组输出中哈希不同的服务会被重建。

```bash
sudo docker compose config --hash '*'
sudo docker compose ps -q | xargs sudo docker inspect \
  --format '{{index .Config.Labels "com.docker.compose.service"}} {{index .Config.Labels "com.docker.compose.config-hash"}}'
```

回滚：在开发机 `git revert` 有问题的提交并推送，再重复第 2–4 步。紧急情况下可在部署主机
`sudo git checkout <上一个正常提交>` 后应用，恢复后执行 `sudo git checkout main && sudo git pull --ff-only`。

## 升级

1. 阅读 [authentik release notes](https://docs.goauthentik.io/releases/)，确认升级路径与破坏性变更。
2. 手动备份并确认成功（见「备份」）。
3. 在开发机修改版本：authentik 为 `compose.yml` 中 `AUTHENTIK_TAG` 的默认值，PostgreSQL 为 `postgresql.image`，
   Caddy 为 `compose.override.yml` 中的 `caddy.image`。运行 `scripts/check.sh`，提交推送。
4. 在部署主机按「修改配置」拉取，然后：

   ```bash
   sudo grep -n '^AUTHENTIK_TAG=' .env   # 若存在会覆盖 compose.yml 的默认版本，应删除该行
   sudo docker compose pull
   sudo docker compose up -d --wait
   ```

PostgreSQL 镜像跟随 16.x 小版本，`docker compose pull` 即会更新小版本。跨大版本（如 17）需要先 dump、换新卷再恢复，
不要只改镜像标签。

## 备份

`authentik-backup.timer` 每日调用 `scripts/backup-postgres.sh`：数据库以 `pg_dump -Fc` 导出，`.env`、compose 文件、
`Caddyfile` 与持久化目录打包；不含 `caddy-data/`（证书可重新签发）。备份目录、文件权限、保留天数以脚本为准。
写入先用 `.partial` 临时文件，校验通过后才改名。

```bash
sudo systemctl start authentik-backup.service
sudo journalctl -u authentik-backup.service -n 20 --no-pager
sudo ls -lh /var/backups/authentik/postgres /var/backups/authentik/files
```

限制：备份与服务在同一台主机、同一块磁盘上，且未加密；主机丢失时备份一并丢失。
迁移或灾备前，把同一时间戳的一对文件复制到异地的加密存储。异地存放位置与加密方式尚未确定，由运维负责人决定。

## 从备份恢复

适用于迁移到新主机，或在原主机回滚数据。需要同一时间戳的 `authentik-<TS>.dump` 与
`authentik-files-<TS>.tar.gz`。恢复会用备份覆盖数据库中的全部数据。

1. 准备部署目录：
   - 新主机：按「新主机安装」完成前置条件和第 1–2 步，**不要**运行第 3 步。
   - 原主机：先停止应用服务 `cd /opt/authentik && sudo docker compose stop server worker`。

2. 将两个备份文件放到主机上（例如 `/root/restore/`），恢复 `.env` 与持久化目录。
   先查看成员路径：旧版脚本生成的包以 `authentik/` 开头，当前脚本生成的包直接是 `.env`、`data/` 等。

   ```bash
   cd /opt/authentik
   F=/root/restore/authentik-files-<TS>.tar.gz
   sudo tar -tzf "$F" | head
   # 成员直接是 .env、data/ 时：
   sudo tar -xzf "$F" -C /opt/authentik .env data certs custom-templates
   # 成员以 authentik/ 开头时：
   sudo tar -xzf "$F" -C /opt/authentik --strip-components=1 \
     authentik/.env authentik/data authentik/certs authentik/custom-templates
   sudo chown -R 1000:1000 data certs custom-templates
   sudo chmod 600 .env
   ```

   `compose.yml` 等配置以 Git 为准，不从备份包中恢复。

3. 新主机：准备目录与备份定时器但不启动服务（检测到已恢复的 `.env`，不会生成新密钥）：

   ```bash
   sudo scripts/bootstrap.sh --no-start
   ```

4. 只启动数据库并导入：

   ```bash
   sudo docker compose up -d --wait postgresql
   sudo cat /root/restore/authentik-<TS>.dump | sudo docker compose exec -T postgresql \
     sh -c 'pg_restore -U "$POSTGRES_USER" -d "$POSTGRES_DB" --clean --if-exists --no-owner'
   ```

   命令应无错误输出；出现 `errors ignored on restore` 时逐条确认后再继续。

5. 启动全部服务：`sudo docker compose up -d --wait`，然后按「检查」确认，并用已有账号登录核对数据。

6. 迁移场景：确认 DNS 已指向新主机后，在旧主机执行 `cd /opt/authentik && sudo docker compose down`
   （不加 `--volumes`），避免两台主机同时签发证书、处理登录。旧主机数据在新主机稳定运行一段时间后再按「删除前置检查」处理。

## 密钥管理与轮换

负责人：`auth.saberu.app` 的运维负责人（持有部署主机 root）。密钥只存放在部署主机上，不写入 Git、工单或聊天记录；
检查时只比对键名或哈希，不打印值。

| 密钥 | 位置 | 轮换影响 |
|---|---|---|
| `PG_PASS` | `.env` | 应用重建期间数据库连接短暂失败 |
| `AUTHENTIK_SECRET_KEY` | `.env` | 用于 cookie 签名，更换后所有活动会话失效，用户需重新登录（[配置文档](https://docs.goauthentik.io/install-config/configuration/)） |
| `AUTHENTIK_EMAIL__PASSWORD` | `.env` | 需先在邮件服务器上修改 |
| `AUTHENTIK_BOOTSTRAP_PASSWORD_HASH`、`.akadmin-initial-password` | `.env`、部署目录 | 只在首次启动时读取；初始密码登录后删除明文，之后在 authentik 界面管理管理员密码 |
| 部署密钥 | `/root/.ssh/authentik-ops-deploy` | 只影响 `git fetch/pull` |

`.env` 也会进入每日备份包，旧值会留在已有备份中，直到按保留策略删除。

以下命令都在 `cd /opt/authentik` 后执行；新值在 root shell 内生成并直接写入 `.env`，不出现在命令行参数、终端输出或 sudo 日志中。

**AUTHENTIK_SECRET_KEY**

```bash
sudo bash -c 'v="$(head -c 60 /dev/urandom | base64 -w0)" && sed -i "s|^AUTHENTIK_SECRET_KEY=.*|AUTHENTIK_SECRET_KEY=$v|" .env'
sudo docker compose up -d --wait
```

**PG_PASS**：先改数据库用户密码，再写入 `.env`，最后重建应用容器。明文密码的 `ALTER ROLE` 语句会在
PostgreSQL 开启语句日志时被记录（[ALTER ROLE 文档](https://www.postgresql.org/docs/16/sql-alterrole.html)），
因此先确认 `log_statement` 为 `none`，否则停下。

```bash
sudo docker compose exec -T postgresql sh -c 'psql -tA -U "$POSTGRES_USER" -d "$POSTGRES_DB" -c "SHOW log_statement"'
```

```bash
sudo bash -c 'set -eo pipefail
  v="$(head -c 36 /dev/urandom | base64 -w0)"
  printf "ALTER ROLE CURRENT_USER WITH PASSWORD '"'"'%s'"'"';\n" "$v" |
    docker compose exec -T postgresql sh -c "psql -q -v ON_ERROR_STOP=1 -U \"\$POSTGRES_USER\" -d \"\$POSTGRES_DB\""
  sed -i "s|^PG_PASS=.*|PG_PASS=$v|" .env'
sudo docker compose up -d --wait
```

**SMTP 密码**：先在 mailcow 中修改，然后用 `sudoedit /opt/authentik/.env` 更新 `AUTHENTIK_EMAIL__PASSWORD`，再执行：

```bash
sudo docker compose up -d --wait
sudo docker compose exec worker ak test_email <收件地址>
```

**部署密钥**：生成新密钥并添加为仓库 Deploy key，确认新密钥可用：

```bash
sudo ssh-keygen -t ed25519 -N '' -C "authentik-ops deploy $(hostname -s)" -f /root/.ssh/authentik-ops-deploy-new
sudo cat /root/.ssh/authentik-ops-deploy-new.pub
sudo git -C /opt/authentik -c core.sshCommand='ssh -i /root/.ssh/authentik-ops-deploy-new' fetch
```

在 GitHub 删除旧 Deploy key 后，用新密钥替换旧文件并再次确认：

```bash
sudo shred -u /root/.ssh/authentik-ops-deploy /root/.ssh/authentik-ops-deploy.pub
sudo mv /root/.ssh/authentik-ops-deploy-new /root/.ssh/authentik-ops-deploy
sudo mv /root/.ssh/authentik-ops-deploy-new.pub /root/.ssh/authentik-ops-deploy.pub
sudo git -C /opt/authentik fetch
```

每次轮换后：按「检查」确认，并手动运行一次备份，使最新备份包含新值。
怀疑 `.env` 或备份包泄露时，轮换上表全部密钥；密钥若进入了 Git 历史，先轮换，再按工作区 Git 恢复流程处理，
不要自行改写已推送的历史。

## 将现有部署目录转换为 Git checkout

适用于早期手工复制文件、没有 `.git` 的部署目录。转换后被跟踪文件变为仓库版本，`.env` 与数据目录保持不变。
应用新配置时可能重建部分服务，登录服务会短暂中断；执行前用「修改配置」中的哈希比较命令确认影响范围。

1. 备份并确认成功（见「备份」），保留转换前的配置与 systemd 单元用于回滚：

   ```bash
   cd /opt/authentik
   TS="$(date -u +%Y%m%dT%H%M%SZ)"
   sudo mkdir "/root/authentik-pre-git-$TS"
   sudo cp -a compose.yml compose.override.yml Caddyfile README.md RUNBOOK.md scripts \
     /etc/systemd/system/authentik-backup.service /etc/systemd/system/authentik-backup.timer \
     "/root/authentik-pre-git-$TS/"
   ```

2. 按「新主机安装」第 1 步准备部署密钥，然后接入 Git：

   ```bash
   sudo env GIT_SSH_COMMAND='ssh -i /root/.ssh/authentik-ops-deploy -o StrictHostKeyChecking=accept-new' \
     git clone --no-checkout git@github.com:saberu-ops/authentik-ops.git "/root/authentik-ops-$TS"
   sudo mv "/root/authentik-ops-$TS/.git" /opt/authentik/.git
   sudo rmdir "/root/authentik-ops-$TS"
   sudo git -C /opt/authentik config core.sshCommand 'ssh -i /root/.ssh/authentik-ops-deploy'
   sudo git -C /opt/authentik reset          # 按 HEAD 填充索引，不改动文件
   sudo git -C /opt/authentik status --short # 预期：被跟踪文件显示 M 或 D，.env 与数据目录不出现
   sudo git -C /opt/authentik diff           # 审阅线上文件与仓库版本的差异
   ```

   如果 `diff` 中出现仓库里没有、且需要保留的线上改动，先停下，把改动提交到仓库后重新执行本步骤。
   如果 git 报 `dubious ownership`，说明部署目录属主不是 root：执行 `sudo chown root:root /opt/authentik` 后重试。

3. 以仓库版本为准，并确认目录属主：

   ```bash
   cd /opt/authentik
   sudo git checkout -- .
   sudo git status --short                   # 预期为空
   sudo stat -c '%u %n' data certs custom-templates   # 都应为 1000，否则 sudo chown -R 1000:1000 <目录>
   sudo grep -n '^AUTHENTIK_TAG=' .env       # 若存在，删除该行，让 compose.yml 决定版本
   ```

4. 预判重建范围（见「修改配置」），然后应用配置与 systemd 单元：

   ```bash
   sudo docker compose config --quiet
   sudo docker compose up -d --wait
   sudo install -m 0644 systemd/authentik-backup.service systemd/authentik-backup.timer /etc/systemd/system/
   sudo systemctl daemon-reload
   ```

5. 按「检查」确认，并手动运行一次备份确认 `backup ok`。
6. 运行正常后可清理：`sudo shred -u .env.*.bak`（旧 `.env` 备份含密钥），并删除其他 `*.bak` 与
   `/root/authentik-pre-git-$TS`。

回滚（`TS` 为第 1 步使用的时间戳）：

```bash
cd /opt/authentik
sudo mv .git "/root/authentik-ops-git-rollback-$TS"
sudo cp -a "/root/authentik-pre-git-$TS/compose.yml" "/root/authentik-pre-git-$TS/compose.override.yml" \
  "/root/authentik-pre-git-$TS/Caddyfile" /opt/authentik/
sudo cp -a "/root/authentik-pre-git-$TS/scripts/." /opt/authentik/scripts/
sudo cp -a "/root/authentik-pre-git-$TS/authentik-backup.service" "/root/authentik-pre-git-$TS/authentik-backup.timer" \
  /etc/systemd/system/
sudo systemctl daemon-reload
sudo docker compose up -d --wait
```

## Outpost 与 docker.sock

worker 不以 root 运行、不挂载 `/var/run/docker.sock`，authentik 因此无法自动部署和管理 Docker outpost。
[内置 outpost](https://docs.goauthentik.io/add-secure-apps/outposts/embedded/) 作为 authentik server 部署的一部分运行，
不受此影响。需要额外的 Proxy/LDAP/RADIUS outpost 时，优先按 authentik 文档手动部署。

确需自动部署时，在 worker 下加回 `user: root` 和 `/var/run/docker.sock:/var/run/docker.sock` 挂载，
并同步调整 `scripts/check.sh` 中对应的检查。这相当于把宿主机 root 权限交给 worker 容器，应作为单独的决策记录在提交说明中。

## 回滚到宿主机 Caddy

仅适用于曾由系统 Caddy 服务提供此站点、且 `systemctl cat caddy` 与 `/etc/caddy/Caddyfile` 仍然存在的主机。
如果容器 Caddy 启动失败：

1. 停止 Compose 中的 Caddy：`sudo docker compose stop caddy`。
2. 启动宿主机 Caddy：`sudo systemctl enable --now caddy`。
3. 确认宿主机 Caddy 使用 `/etc/caddy/Caddyfile`，并用 `sudo ss -ltnp` 检查 `80/443` 的监听者。
4. 迁移期间的配置备份保存在 `/etc/caddy/Caddyfile.<UTC时间戳>.docker-migration.bak`。

回滚只恢复入口代理，不删除 authentik 容器、数据库卷或 `data/`。

## 删除前置检查

不要仅因端口空闲而删除 authentik。删除前确认没有依赖 `auth.saberu.app` 的登录、OAuth/OIDC、SAML、LDAP 或 outpost；
先完成一次备份并把备份复制到主机之外，保留 `data/`、`certs/` 和 `.env`。确认后再执行有明确目标的
`sudo docker compose down`；不要默认使用 `--volumes`，否则会删除数据库卷。
