# 评审 0011：以 main 为基线的重建方向决策

- 日期：2026-09-28
- 性质：调研与决策记录。
  - 操作者要求“需要我决定的部分按照最佳实践来”，因此由 AI 代理作出以下决策。
  - 决策结果已写进[方案 0004](../plans/0004-deployment-foundation.md)和[方案 0005](../plans/0005-app-security-baseline.md)，以两份方案为准；本文只保存理由和来源。
  - 两份方案成稿后，先后经过一次独立评审和一次定向复审，本文已按结果修订。
- 输入：
  - 基线：`main` 41a6635。
  - 参考：临时分支 `draft` ea7d6b1 及其工作区的未提交修改。这些内容以后会删除，所以用到的原文摘录在文末。所用文件的 sha256 前 16 位：

    | 文件 | sha256 前 16 位 |
    |---|---|
    | `docs/plans/0002-code-instance-lifecycle.md` | `a00eb6f966f5be2e` |
    | `docs/plans/0003-authentik-app-security-baseline.md` | `cf27f788aa60b7f1` |
    | `docs/implementation/implementation-checklist.md` | `c186ec6c7786c843` |

  - 本机只读快照（2026-09-28 16:22–18:19）：
    - Docker 29.3.1，rootful 模式；
    - 旧实例的 4 个容器已运行 5 天，Caddy 占用公网 80/443 端口；
    - 没有备份定时器；
    - 未安装 `uidmap` 和 `systemd-container`；
    - 使用 cgroup v2，`user@.service` 已委派 memory、pids、cpu；
    - `/proc` 没有设置 `hidepid`；
    - 本机已有的 cloudflared 用 `--token-file` 读取令牌，并固定了镜像摘要。
  - 调研：2026-09-28 完成三组一手资料调研，来源见文末：
    - Docker rootless 与 Cloudflare Tunnel；
    - authentik 2026.8.2 的文档与源码；
    - 备份、供应链、监控与应急。
- 操作者原话：“以main分支为基线，当前分支内容为临时保存用的提交，后续要删掉。需要我决定的部分按照最佳实践来，整体也要基于安全，稳定性来考虑，不能完全不思考只按照设计资料来，一次性的root脚步可以接受”。

## 结论

保留草稿的大方向：代码、配置和数据分离，rootless，抗钓鱼 MFA，最小权限，可恢复。

实现方式按“安全优先、其次稳定、优先用原生和成熟工具”重新判断，减少了自研的部分。操作者在草稿评审中已经作出的安全决定，原样保留。下面逐项列出决定、草稿原来的做法和理由；草稿原文见文末摘录。

## 部署基础

1. **旧实例：立即停止，盘点后再删除。**
   - 草稿做法：清单中写的是先完成方案批准和输入准备，再处置旧实例（摘录 Q1）。
   - 理由：
     - 这个实例没有投入使用，也没有加安全基线，却一直对公网开放。
     - 停止随时可以恢复；重启策略是 `unless-stopped`，主机重启后也不会自己起来。
   - 执行情况：见[操作记录 R001](../implementation/records.md#r001-停止旧实例)。

2. **入口：改用 Cloudflare Tunnel，源站不开放任何入站端口；Caddy 只在内部提供 HTTP。**
   - 草稿做法：Caddy 直接开放 80/443，并信任 Cloudflare 的全部网段（摘录 Q2）。
   - 理由：
     - Cloudflare 官方对两种做法的评价不同：
       - Tunnel 评为“非常安全”；
       - 只放行 Cloudflare 网段评为“中等安全”，因为其他 Cloudflare 账号的流量也能进来；
       - 官方还指出，同机运行邮件服务会暴露源站 IP，而本机运行着 mailcow。
     - rootless 模式下直接开放低端口，要么调低整机的非特权端口下限，要么给 rootlesskit 加能力，两者都会影响整台主机。另外，默认的端口转发方式不保留客户端源 IP。
   - Tunnel 的配套措施：
     - Tunnel 专供本实例使用，只有生产的两个副本连接它。同一个 Tunnel 的连接器会分摊流量，所以测试一律另建 Tunnel（定向复审指出）。
     - 令牌不出现在命令行上，因为本机 `/proc` 没有设置 `hidepid`，其他用户能看到命令行。
     - 镜像固定摘要。
     - Caddy 只信任 cloudflared，客户端 IP 取 `CF-Connecting-IP`，并强制 https。
     - authentik 只信任 Caddy。
   - 对外开放分三个状态：未路由、仅操作者、公开。WebAuthn 必须使用正式域名，所以涉及登录的开发与验收，都在“仅操作者”状态下进行。
   - 可信代理的默认值有问题（按 2026.8.2 源码推断）：
     - authentik 默认信任所有私网网段发来的转发头；
     - 从本机回环端口进入的连接，来源显示为 Docker 网关；
     - 因此本机上任何进程都能伪造客户端 IP。
     
     新架构把可信代理只收窄到 Caddy 一个地址。

3. **运行方式：用专用的无特权用户运行 rootless Docker，由一次性 root 脚本做主机准备。**
   - 草稿做法：不自动安装 Docker、不修改主机权限、不切换运行模式（摘录 Q3）。
   - 理由：
     - 把认证服务与运行 mailcow、1Panel 的 rootful 守护进程隔离开。容器逃逸或守护进程被攻破时，攻击者只能拿到这个无特权用户的权限。
     - 操作者要求整个项目 rootless，并且接受一次性 root 脚本。
   - 注意事项：
     - 不能用 `su` 或 `sudo -u` 切换到该用户，否则用户级 systemd 无法工作；
     - 该用户不能加入 docker 组；
     - rootful 守护进程还要继续运行，所以安装 rootless 时要加 `--force`；
     - 本机已经委派了所需的 cgroup 控制器，脚本不改全局设置。
   - 代价：
     - rootless 模式下没有 AppArmor，但 seccomp 仍然生效；
     - 缺少 cgroup 委派时，资源限制只给出警告并被丢弃，部署后要核对实际值；
     - 禁止使用 host 网络。

4. **数据：数据库和 `/data` 用命名卷，备份时从容器内以流的方式导出。**
   - 草稿做法：主要使用绑定目录（摘录 Q4）。
   - 理由：
     - Docker 官方推荐用卷。
     - rootless 的 UID 映射让宿主机直接读写容器文件很不方便。

5. **机密：统一放在实例 `.env`，Compose 只把每个服务需要的变量传给它。镜像摘要由代码仓库维护，实例不能覆盖。**
   - 草稿做法：每个服务一份机密文件（摘录 Q5）。
   - 理由：
     - Compose 的文件型机密会静默忽略属主和权限设置。rootless 下，容器用户读不到权限为 0600 的机密文件。
     - authentik 的 `file://` 在读不到文件时只回退为空值，不会报错（源码推断）。
     - 在 rootless 下，只有这个专用用户能查看容器的环境变量。

6. **本地定制层纳入本期，只开放三个入口。覆盖文件采用允许清单；Caddy 片段按转换后的配置做断言。**
   - 三个入口：实例级 Compose 覆盖文件、Caddy 本地片段、自定义模板。
   - 草稿做法：方案草稿把它排除在本期之外，而操作者早先已经决定纳入（摘录 Q6、Q7）。
   - 为什么 Compose 覆盖文件用允许清单：
     - 只拒绝少数几项是不够的。覆盖文件还可以：
       - 改可信代理变量；
       - 重新打开内嵌 outpost；
       - 加 `cap_add`、放宽 seccomp、使用其他服务的命名空间；
       - 把新服务接入数据库网络。
     - 所以覆盖文件只允许设置资源限制、日志、标签，以及允许清单里的环境变量，并对最终配置做正向断言。
   - 为什么还要检查 Caddy 片段：定向复审指出，片段可以改掉可信代理和转发头的处理。所以 `check` 把最终 Caddyfile 转成 JSON，断言可信代理、客户端 IP 来源、上游和转发头的改写都与设计一致。
   - 为什么 Caddy 只导入副本：第二次定向复审指出，如果直接导入片段目录，每日 `apply` 的重载或容器重启会让没检查过的片段生效。所以 Caddy 只导入 `state/` 中已通过断言的副本。

7. **去掉 `/certs` 挂载；`/templates` 用到时才挂载；在 `/data/user_settings.py` 挂载只读空文件。**
   - 官方说明 `/certs` 只用于证书发现，是可选的。
   - `/templates` 可以覆盖任意页面模板。
   - authentik 启动时会执行 `/data/user_settings.py`，而 `/data` 是应用可写的目录。挂载只读空文件，可以防止有人在那里植入代码，比启动后再检查更可靠。

8. **起始版本 2026.8.3；所有镜像在 Compose 中固定摘要；在更新 PR 的 CI 中，按摘要校验 authentik 镜像的来源证明，并限定签名工作流。**
   - 官方只为 2026.5.x 和 2026.8.x 提供安全修复，并要求运行最新补丁。2026.8.3 修复了策略绑定缓存失效的问题。
   - 按标签校验，证明的是标签当时指向的镜像，不是实际部署的摘要，所以要按摘要校验。
   - 官方不支持降级，回退只能靠备份。

## 备份、监控与应急

9. **备份：不停机，用 restic 加密后写入只可追加的异地仓库，并定期自动验证。**
   - 草稿做法：设置停写窗口，自行实现加密的备份组（摘录 Q8）。
   - 为什么不停机：
     - PostgreSQL 官方说明 `pg_dump` 在并发读写时也能导出一致的备份；
     - authentik 官方的备份说明不要求停止服务；
     - `/data` 里只有很少变化的上传文件；
     - 停止服务会降低可用性。
   - 为什么选 restic：
     - 它是 Debian 自带的单个程序；
     - 在客户端加密；
     - 可以抽样检查数据；
     - `--stdin-from-command` 在导出失败时不生成快照；
     - 配合 rest-server 的 `--append-only` 模式，本机无法删除备份。
   - 清理：只在另一台机器上执行 `forget --keep-within`，这是 restic 对只可追加仓库的建议。
   - 验证：
     - 频率参考 NIST SP 800-34 与 CISA 的指南：每周自动恢复一次，每季度做一次完整演练。
     - 隔离的演练目标没有 Tunnel，无法用 WebAuthn 登录。所以季度演练改用回读数据，并经 `breakglass` 用恢复链接登录。
     - 上线前在“仅操作者”状态下做一次完整的切换演练，用硬件密钥登录验证（定向复审指出）。
     - 恢复出的 `.env` 带有生产 Tunnel 令牌。演练实例一旦接入生产 Tunnel，会分走真实流量，而旧备份中已撤销的会话和设备在它上面仍然有效（第二次定向复审指出）。因此：
       - `restore --drill` 会去掉令牌，并改用演练专用的名称；
       - 切换演练每一步都核对连接器列表；
       - 演练结束后销毁演练实例；
       - 正式恢复前，先确认原实例已停止，或先轮换令牌。

10. **监控：从外部探测，定时任务向本机以外的心跳服务报到，日志使用 `local` 驱动。**
    - 外部探测的地址是 `/-/health/ready/`，它同时检查数据库连接。
    - Docker 默认的 json-file 日志驱动不会轮转日志。

11. **应急：设两个应急账号；最后手段是主机层的恢复链接；`breakglass` 入口只用于修复。**
    - 草稿做法：在本机保留一个使用生产域名和 HTTPS 的应急入口，并信任“应急来源”（摘录 Q9）。
    - 为什么取消这个应急入口：
      - WebAuthn 要求 RP ID 与域名一致，并要求有效证书；Chrome 在证书出错时会禁用 WebAuthn。
      - 在 Tunnel 架构下，要在本机保留生产域名入口，就需要额外的证书和 DNS 令牌，还会多一条可以被伪造的信任路径。
    - `breakglass` 入口的用途与限制：
      - 它只在回环地址上发布 server 端口。
      - 用恢复链接登录后，只能在管理界面修复配置、撤销设备。
      - 不能在这里登记新设备，因为在 localhost 上无法使用生产域名。
    - 依据：
      - 微软的应急账号指南建议至少两个账号，FIDO2 密钥分地点存放，每次使用都告警，每 90 天演练一次。
      - `ak create_recovery_key` 生成的链接一次性、限时，登录时不经过流程和 MFA。

## 应用层安全

12. **配置即数据的思路不变，生效方式改用 authentik 原生蓝图。**
    - 草稿做法：执行期间先关闭入口，完成后再开放（摘录 Q10）。
    - 为什么不需要关闭入口：authentik 在一个数据库事务里执行单个蓝图，失败时整体回滚；嵌套蓝图失败时只记日志。所以把整条认证链放进同一个蓝图，失败时就不会停在半新半旧的状态。
    - 回退要生成删除条目：
      - 蓝图执行成功后，如果核验失败，再执行旧版本并不会删除新版本新增的对象，只有 `state: absent` 条目才会删除（独立评审指出）。
      - 所以渲染器为不再包含的对象生成删除条目；
      - 有变更时，执行前先备份。
    - 自动核验与手动登录：
      - 自动登录必须绕过验证码，或者在主机上保存软件密钥，都会削弱安全基线；
      - 所以自动核验只做回读和策略测试，登录由操作者手动验证。
    - 漂移：
      - 按 2026.8.2 源码，未改动的蓝图文件不会被重新执行；
      - 所以每日定时重新执行一次当前生效的版本来纠正漂移；
      - `check` 保持只读。
    - 检查失败按类型分别阻止不同操作，不能一律阻止 `apply`。定向复审指出：否则一次无害的元数据变化，就可能让每日的基线重新执行停下来。
      - 配置断言失败，阻止启动；
      - 设置校验失败或偏离过期，阻止新版本生效；
      - 安全检查项失败，阻止对外开放；
      - 每日重新执行当前生效版本，永远不受阻止。

13. **上线门槛以方案 0005 为准。与草稿相比有以下变化或说明：**
    - **信誉策略只按 IP 拒绝。**
      - 按用户名拒绝的话，任何人都能把已知账号锁住 24 小时（草稿做法见摘录 Q11）。
      - 这个策略在分数低于或等于阈值时算“通过”，用来拒绝时必须取反。
    - **管理员使用专用账号，并且只能在专用登记阶段登记设备。**
      - 独立评审发现：未知 AAGUID 登记后不带型号，而验证阶段会放过没有型号的凭据。
      - 所以在登记阶段就排除“未知设备”；普通登记流程通过策略拒绝管理员。
      - 管理员名下出现没有型号的设备，或者日常账号持有管理角色时，`check` 报错。
      - authentik 不校验证明链，型号限制只是纵深防御。
    - **会话与地理位置：操作者在草稿评审 0008 中的决定原样保留，另加一层保护。**
      - 保留的决定（摘录 Q12、Q14）：
        - 所有用户启用不可能旅行检测；
        - 管理员会话按 ASN 与网络绑定；
        - 普通用户不按网络绑定，因为切换网络会被登出。
      - 另加的一层：普通用户会话按国家绑定。不可能旅行检测只在登录时判断，挡不住已登录会话的重放；国家级绑定补上跨国重放这一块，又不影响在同一国家内切换网络。同一国家内的重放仍然挡不住，已列入方案 0005 的风险。
      - 本记录较早的版本曾把不可能旅行检测移出本期，并把管理员绑定降为只按 ASN。定向复审指出，这等于未经确认就推翻了操作者的决定，已恢复原样。
    - **高风险 API 路径整条屏蔽。**
      - 这与官方加固指南一致，也是操作者在草稿评审 0008 的决定（摘录 Q15、Q16）。
      - 本记录较早的版本曾改为“只拒绝写入”，第二次定向复审指出这又推翻了操作者的决定，已恢复。
      - 草稿当时的设想是，管理员改走本机回环端口查看这些对象；但在 Tunnel 架构下，经回环地址无法用硬件密钥登录。
      - 所以如果实测发现管理界面因此无法接入应用，列为需要操作者确认的事项（见下文）。
    - **暂不启用 Passkey 自动填充。** 它会跳过验证码和后续的 MFA 阶段。
    - **关闭内嵌 outpost。** 按源码推断，内嵌 outpost 存在时，SECRET_KEY 可以当作它的 API 令牌使用。
    - **akadmin 只停用，不删除也不改名，并从 `.env` 删除初始化值。**
      - 按源码推断，重新执行 bootstrap 蓝图时，会把 akadmin 重建为超级用户。
      - `check` 会按键名核对这些值已经删除。
    - **Turnstile 的域名限制放在 Cloudflare 侧。** authentik 不校验 Turnstile 返回的 hostname。
    - **GeoIP 数据：** 保留操作者“MaxMind 写在配置文件里，先留空，以后可以修改”的决定（摘录 Q17）。
      - 留空时使用镜像自带的数据库；填写后启用 geoipupdate。
      - 数据库缺失时，authentik 只记一条警告，所以由 `check` 负责发现，并报告库的构建时间。
      - 草稿要求自研持续失效检测和会话撤销（摘录 Q13）；现在改为依靠原生的“缺数据即结束会话”和“查询失败即拒绝”。

14. **不在本期，今后各自单独规划：**
    - 防账号枚举的时序分析；
    - 数据库 TLS；
    - 自动复核报告。

    这样处理符合“独立能力单独立方案”的共享规则。

## 资料与分支

15. **新工作在基于 main 的 `rebuild` 分支上进行，不合并 draft。**
    - 重新实现的草稿思路：域名参数化、清理转发头、文档检查覆盖 `docs/**`。
    - 放弃的草稿内容：信任 Cloudflare 网段、`check_proxy_trust.py`、root 定时器。
    - 删除 `draft` 分支的顺序：先撤销原 checkout 中的未提交修改并切到 `main`，再删除分支。不能删除原目录，因为仓库的 `.git` 在那里。

16. **资料约定写进 AGENTS.md，并由文档检查强制执行。**
    - 编号永不复用：新方案从 0004 开始，新评审从 0011 开始，避免和讨论中用过的“方案 0002/0003”混淆。文档检查会拒绝更小的编号。
    - 评审记录只在它改变设计，或需要跨会话、跨代理接续时才保存。
    - 进度只在实施清单中维护。
    - 结果记录（changelog）使用对应方案的编号。

## 审批时需要操作者确认的事项

以下两项超出了操作者原有的决定，或者只能由操作者选择，在审批方案（清单 A1）时一并确认：

1. **新增：普通用户会话按国家绑定。**
   - 这是在操作者原有决定之上额外加的一层保护。
   - 代价：用户出境后，IP 换到另一个国家时需要重新登录。
2. **有条件的选择：管理界面接入应用。** 如果实测发现，整条路径屏蔽后管理界面无法接入应用，二选一：
   - 对这些路径放行只读，记为偏离；
   - 应用接入改为在主机上用蓝图或 API 完成。

**确认结果（2026-09-28）：** 操作者批准了两份方案，并授权按最佳实践决定这两项：

- 第 1 项：采用国家级会话绑定。
- 第 2 项：先保持整条路径屏蔽，等清单 C0 实测有了结果再按证据选择。

## 尚未实测

下列结论目前来自文档或源码，将在两份方案的“前提与待实测”中验证：

- cloudflared 经 rootless 网络用 QUIC 连接（用单独的测试 Tunnel 验证）；
- authentik（UID 1000）对命名卷的读写；
- 资源限制的实际效果；
- 只读的 `user_settings.py` 挂载；
- `caddy adapt` 的输出能否支撑前面列出的断言；
- 单个蓝图失败时的回滚，以及 `absent` 条目的删除效果；
- 策略测试接口；
- 管理员专用登记阶段；
- 不可能旅行的阈值与容差；
- 经 `breakglass` 入口使用恢复链接；
- 镜像自带 GeoIP 库有多旧；启用 geoipupdate 时切换是否会中断 GeoIP；
- 整条路径屏蔽后，管理界面能否接入应用；
- `gh attestation verify` 是否接受摘要引用。

## 草稿原文摘录

以下摘自 `draft` 分支及其工作区（哈希见上文），供草稿删除后核对。引文保留原文措辞，只去掉了表格首尾的分隔符、序号和加粗标记。

- **Q1** 实施清单第 66 行：“当前目标为全新安装，按已确定方向在 A4 测试前处置原实例以释放资源。”
- **Q2** `draft` ea7d6b1 的 `Caddyfile` 第 4 行：`trusted_proxies static 173.245.48.0/20 103.21.244.0/22 …`（Cloudflare 网段）。
- **Q3** 生命周期方案第 23 行：“不自动安装 Docker、修改主机权限或切换 Docker 运行模式”。
- **Q4** 生命周期方案的存储表：“上传文件、文件证书、自定义模板 | 实例目录”。
- **Q5** 安全基线方案第 52 行：“实例机密文件 | 数据库、authentik、SMTP、Turnstile、MaxMind 等凭据 | 按服务最小读取”。
- **Q6** 生命周期方案第 96 行：“本期不加载任意实例 Compose 覆盖文件；确有定制需求时再确定扩展契约和验收范围。”
- **Q7** `draft` ea7d6b1 的评审 0005 第 11 行：“本地定制纳入本期（已决定）”。
- **Q8** 生命周期方案第 197 行：“首期采用经操作者确认停写窗口的保守流程”。
- **Q9** 安全基线方案第 136 行：“应急入口通过 SSH 转发或受控私网连接，保留生产域名、HTTPS 和正确 WebAuthn RP/origin”。
- **Q10** 安全基线方案第 158 行：“关闭或暂停受影响入口后，按依赖顺序发布蓝图、应用运行配置与重建必要容器。”
- **Q11** 安全基线方案第 74 行：“信誉 | 用户名/IP 检查、阈值、拒绝或挑战动作”。
- **Q12** `draft` ea7d6b1 的评审 0008 第 26 行：“普通用户不绑定网络 | 不绑定，改用不可能旅行检测和 MFA 弥补 | 按 ASN 绑定时，在 Wi-Fi 和移动网络之间切换就会被登出，这是可用性问题。可用性本身也是安全目标的一部分”。
- **Q13** 安全基线方案第 178 行：“管理员 GeoIP 数据缺失、过期、损坏或无查询结果 | 拒绝新管理员登录，告警；已有会话按配置时限撤销”；第 186 行：“未实现持续检查时不能宣称运行期门控完成”。
- **Q14** `draft` ea7d6b1 的评审 0008 第 18 行，“收紧后”一栏：“管理员会话绑定 ASN 和网络；引入 GeoIP（所有用户启用不可能旅行检测，管理员启用国家白名单）；普通用户保持登录缩短为最长 7 天”。
- **Q15** `draft` ea7d6b1 的评审 0008 第 17 行：“高风险 API 先只屏蔽写方法 | 按官方指南整条路径屏蔽 | 与官方基准保持一致；管理员查看这些对象时，改为经本机回环端口访问 | 管理界面的几个页面要走 Tailscale 或 SSH”。
- **Q16** 安全基线方案第 190 行：“推荐公网拒绝清单初始覆盖全部方法”。
- **Q17** `draft` ea7d6b1 的评审 0009 第 10 行（操作者输入）：“MaxMind 应该写在配置文件里，先留空，以后可以修改”。

## 来源（2026-09-28 查阅）

Docker 与 rootless：

- <https://docs.docker.com/engine/security/rootless/>
- <https://docs.docker.com/engine/security/rootless/tips/>
- <https://docs.docker.com/engine/security/rootless/troubleshoot/>
- <https://docs.docker.com/engine/release-notes/29/>
- <https://github.com/rootless-containers/rootlesskit/blob/master/docs/port.md>
- <https://rootlesscontaine.rs/getting-started/common/login/>
- <https://docs.docker.com/engine/storage/volumes/>
- <https://docs.docker.com/reference/compose-file/services/#secrets>
- <https://docs.docker.com/engine/logging/drivers/local/>

Cloudflare：

- <https://developers.cloudflare.com/cloudflare-one/networks/connectors/cloudflare-tunnel/>
- <https://developers.cloudflare.com/tunnel/reference/tunnel-tokens/>
- <https://developers.cloudflare.com/tunnel/reference/run-parameters/>
- <https://developers.cloudflare.com/tunnel/concepts/routing/>
- <https://developers.cloudflare.com/fundamentals/reference/http-headers/>
- <https://developers.cloudflare.com/fundamentals/security/protect-your-origin-server/>
- <https://developers.cloudflare.com/fundamentals/reference/connection-limits/>

authentik：

- <https://docs.goauthentik.io/install-config/install/docker-compose/>
- <https://docs.goauthentik.io/install-config/configuration/>
- <https://docs.goauthentik.io/install-config/reverse-proxy/>
- <https://docs.goauthentik.io/install-config/automated-install/>
- <https://docs.goauthentik.io/install-config/upgrade/>
- <https://docs.goauthentik.io/security/security-hardening/>
- <https://docs.goauthentik.io/security/policy/>
- <https://docs.goauthentik.io/customize/blueprints/>
- <https://docs.goauthentik.io/sys-mgmt/ops/geoip/>
- <https://docs.goauthentik.io/sys-mgmt/ops/backup-restore/>
- <https://docs.goauthentik.io/sys-mgmt/events/notifications/>
- <https://docs.goauthentik.io/troubleshooting/login/>
- 2026.8.2 源码：<https://github.com/goauthentik/authentik/tree/version/2026.8.2>，涉及以下文件：
  - `authentik/lib/config.py`
  - `authentik/blueprints/v1/importer.py`
  - `authentik/stages/authenticator_webauthn/`
  - `authentik/stages/authenticator_validate/stage.py`
  - `authentik/policies/reputation/models.py`
  - `authentik/api/authentication.py`
  - `blueprints/system/bootstrap.yaml`

备份、供应链、监控与应急：

- <https://www.postgresql.org/docs/16/app-pgdump.html>
- <https://restic.readthedocs.io/en/stable/>
- <https://github.com/restic/rest-server>
- <https://kopia.io/docs/advanced/ransomware-protection/>
- <https://nvlpubs.nist.gov/nistpubs/Legacy/SP/nistspecialpublication800-34r1.pdf>
- <https://www.cisa.gov/stopransomware/ransomware-guide>
- <https://docs.docker.com/build/building/best-practices/>
- <https://cli.github.com/manual/gh_attestation_verify>
- <https://healthchecks.io/docs/monitoring_cron_jobs/>
- <https://www.cisa.gov/news-events/directives/bod-26-04-prioritizing-security-updates-based-risk>
- <https://learn.microsoft.com/en-us/entra/identity/role-based-access-control/security-emergency-access>
- <https://pages.nist.gov/800-63-4/sp800-63b.html>
- <https://www.w3.org/TR/webauthn-3/>
