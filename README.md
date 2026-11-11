# TG115

TG115 是一个完全自建的个人文件传输工具：把文件转发给自己的 Telegram Bot，由 VPS
自动排队、下载并写入自己选择的网盘。每次部署选择一个目标：

- 115 网盘（默认，保留原有路径）；
- Google Drive。

两条路线都经过 CloudDrive2，界面和内部都只有这两个网盘选项：

```text
Telegram → VPS → CloudDrive2 → 115
                            → Google Drive
```

CloudDrive2 是共同传输层：115 和 Google Drive 都由用户本人在 CloudDrive2 管理页登录挂载，
rclone 只作为内部 WebDAV 客户端使用同一个 `cd2:` remote（`type=webdav`）。本程序不处理
Google OAuth token、client ID/secret 或 Google 文件夹 ID，也不直接请求 Google API。

## 新版操作流程

部署器按编号从上往下填写，只有一个主按钮：

1. **① 连接服务器**：VPS 地址、用户名、SSH 端口、密码或 SSH 私钥；可选点“测试连接”。
2. **② Telegram 信息**：Bot Token（@BotFather）、API ID / API Hash（my.telegram.org）、
   你的数字 ID（@userinfobot）。
3. **③ 保存位置**：选 115 或 Google Drive；文件夹默认 `115/telegram转存` 或
   `GoogleDrive/telegram转存`（不存在会自动创建，留空或只填网盘名时也用 `telegram转存`，
   不会存到网盘根目录）；确认 CloudDrive2 WebDAV 地址（CD2 装在同一台 VPS 时勾选，
   自动用 `http://clouddrive2:19798/dav`；否则填 `http://IP:19798/dav`，公网必须
   `https://域名/dav`），再填 WebDAV 用户名、密码。
4. **④ 部署并检查**（固定在窗口下方）：第 1 小步点“一键部署”；部署成功后第 2 小步
   “登录并挂载网盘”，挂载好后第 3 小步“检查是否可用”，通过才算真正可用。

已经部署过可以直接做 ④ 的第 2、3 小步。VPS 配置、本地预算、磁盘保留、传输方式、
sudo 密码、安装目录、时区，以及“修复网络”
“查看 VPS 状态”“清空日志”都收进默认折叠的“高级选项”，默认值不用改。出错时弹窗和
部署记录会写明“下一步：到「② Telegram 信息」改好‘Bot Token’……”，并自动滚动到出错的
输入框（高级选项里的会自动展开）。详见[小白使用说明](docs/README-小白使用说明.md)。

## 主要特性

- Windows 图形化一键部署器；
- Telegram 私聊 Bot 接收文件；
- SQLite 持久化队列，重启后自动恢复；
- 按 CPU、内存、磁盘、网络和目的端状态动态调节并发；
- VPS 配置预设：默认“最小配置”（1 核 / 1GB / 20GB 硬盘，2GB 本地任务预算、6GB 磁盘保留），
  另有“宽裕配置”（20GB 预算、20GB 保留）可选；
- 单文件超过本地预算时自动使用 Telegram → rclone → CloudDrive2 流式模式；
- CloudDrive2 WebDAV 写入、大小校验、远端改名和本地清理；
- 文件直接保存到配置目录，不自动创建年份或月份文件夹；
- 仅允许配置的 Telegram 数字 ID 使用；
- 明确区分“Bot 传输完成”和“网盘官方端已由用户确认”。

## 两种目标网盘

| 目标 | 传输路径 | VPS 额外要求 |
| --- | --- | --- |
| 115（默认） | Telegram → VPS → CloudDrive2 WebDAV → 115 | 由本机托管 CloudDrive2 时需要 `/dev/fuse`；复用已有 CD2 时不需要 |
| Google Drive | Telegram → VPS → CloudDrive2 WebDAV → Google Drive | 同上，取决于是否托管 CloudDrive2 |

配置项 `DESTINATION_TYPE` 取 `clouddrive2`（默认，代表 115）或 `gdrive`（代表经 CloudDrive2
的 Google Drive）。部署脚本和 Bot 都独立校验该值：缺失时按 `clouddrive2` 处理，也接受界面
别名 `115`（归一为 `clouddrive2`）和 `google_drive`（归一为 `gdrive`），未知取值直接报错。

两条路线都要求同一组 CloudDrive2 WebDAV 配置：

- `CD2_WEBDAV_URL_B64`、`CD2_WEBDAV_USERNAME_B64`、`CD2_WEBDAV_PASSWORD_B64`；
- `CD2_TARGET_PATH_B64`：本次选中网盘在 CD2 WebDAV 根目录下的相对路径，默认
  `115/telegram转存` 或 `GoogleDrive/telegram转存`。部署器界面不会写入空值（留空或只填
  网盘名时自动接上 `telegram转存`）；只有手写配置时空值才表示 WebDAV 根目录。目标文件夹
  不存在时，Bot 启动后的健康检查和“检查是否可用”会用 `rclone mkdir` 自动创建。

是否由 VPS 托管 CloudDrive2 只由用户勾选的 `DEPLOY_CLOUDDRIVE2`（默认 `true`）决定，与选择
哪个网盘无关。两条路线都可以打开 CloudDrive2 管理页、修复 CD2 网络并执行 WebDAV 验收；
关闭该选项时程序复用 VPS 上已有的 CloudDrive2，不安装、不启动、不修改它的容器和数据。

## 使用范围

当前正式支持本人和 Bot 的一对一私聊。部署器不会替用户登录 Telegram、CloudDrive2、
115 或 Google，也不会读取这些账号的密码。

完整部署步骤见 [小白使用说明](docs/README-小白使用说明.md)，其中“在 CloudDrive2 中挂载网盘”
一章同时适用于 115 和 Google Drive。

## 部署前准备（推荐）

| 项目 | 推荐准备 | 说明 |
| --- | --- | --- |
| VPS（最小配置，默认推荐） | 1 核 CPU、1GB 内存、20GB 硬盘 | Ubuntu 22.04/24.04 或 Debian 12 64 位。本地任务预算 2GB、磁盘最少保留 6GB；由本机托管 CloudDrive2 时需要 `/dev/fuse` |
| VPS（宽裕配置，可选） | 建议内存至少 2GB、硬盘约 50GB | 本地任务预算 20GB、磁盘最少保留 20GB；适合经常批量转发 |
| VPS 网络 | 稳定访问 Telegram、Docker 镜像仓库和 CloudDrive2 挂载的网盘，端口带宽建议 100Mbps 或以上 | 实际速度仍受 Telegram、VPS 线路和网盘状态共同影响 |
| CloudDrive2 | CloudDrive2 会员，并确认目标网盘挂载和 WebDAV 可用 | 会员登录和网盘挂载由本人在 CloudDrive2 管理页完成，部署器只使用单独设置的 WebDAV 用户名和密码 |
| 115（选择 115 时） | 可正常登录且剩余空间足够的 115 账号 | 115 会员不是程序代码的硬性要求，实际容量和服务权益以账号状态为准 |
| Google Drive（选择 Google Drive 时） | 有足够空间的 Google 账号，并已在 CloudDrive2 中挂载 | 不需要 OAuth token、client ID/secret 或文件夹 ID，本程序不访问 Google API |
| Telegram | Bot Token、API ID、API Hash、本人数字 ID | Bot Token 从 `@BotFather` 获取，API ID/API Hash 从 `my.telegram.org` 获取 |
| 本地电脑 | Windows 10/11 64 位 | 用于运行图形化部署器和建立 SSH 安全隧道 |

大量或长期批量传输时，建议使用宽裕配置或继续升级到 4 核 CPU、8GB 内存、80～100GB SSD。
完整填写项及默认值见[部署前填写信息清单](docs/填写信息清单.md)。

上表的内存和硬盘数字只是推荐：部署脚本不做固定最低内存、总磁盘或空闲空间的安装拦截，
机器比推荐值小也会继续安装。实际安装步骤出错仍会报错——APT 源、Docker、Compose 构建、
容器启动，以及托管 CloudDrive2 时的 `/dev/fuse` 缺失，都会中断部署并保留完整日志。
部署完成后，运行期磁盘保留线和资源调度继续生效。

## VPS 配置选项

部署器里有两个 VPS 配置选项，默认选中并推荐最小配置：

| 内部值 | 界面 | VPS | 本地任务预算 | 磁盘最少保留 |
| --- | --- | --- | --- | --- |
| `minimum` | 最小配置（推荐） | 1 核 / 1GB / 20GB 硬盘 | 2GB | 6GB |
| `standard` | 宽裕配置 | 建议内存至少 2GB、硬盘约 50GB | 20GB | 20GB |

- 两个选项只是推荐。切换时写入对应的预算和保留值；之后可以手工修改，部署时以输入框为准。
- 不会按所选配置检测 VPS 内存是否健康，也不会因此改内存放行阈值。内存阈值固定为 1024MB / 512MB，
  它是运行期调度参数，不是安装门槛；安装阶段不按固定最低内存、总磁盘或空闲空间拒绝部署。
- 20GB 硬盘上，2GB 只限制 Bot 普通落盘；6GB 是整块磁盘至少留出的空闲。单文件超过预算时继续自动流式传输。
- CloudDrive2 自己的缓存不计入 2GB 预算，因此仍要关注 `df -h` 的真实剩余空间。
- 运行期按实时可用内存放行：低于 512MB 时下载和上传窗口降为 0，任务停在“在排队”；
  512MB 至不足 1024MB 时，下载与上传窗口分别逐步收缩到 1。这是运行期保护，和安装门槛是两件事。

## 日志与磁盘占用

部署与 WebDAV 验收不再要求 Bot 容器处于 `healthy` 状态，Compose 已禁用 Bot 健康探针。
升级已有部署时，使用新版部署器重新执行一键部署，Compose 会更新容器配置。
容器启动失败仍会报错；WebDAV 验收继续实际写入、校验、改名并清理测试文件。

传输成功后会删除该任务的 VPS 本地下载文件，日志同样按任务和总量限制，不会在 20GB 硬盘上
持续增长：

- 应用共享日志 `logs/tg115.log` 按 2MB 轮转，最多保留 2 份，最近错误仍可查看；
- 两个容器（Bot 与 CloudDrive2）都使用 `json-file` 日志驱动，单个文件上限 5MB、最多 2 份；
- 每次传输的 rclone 输出单独写入 `logs/transfers/<task_id>.log`，单文件最多 1MB；
- 任务进入 `completed` 后立即删除该任务的 `transfers/<task_id>.log`；删除失败只记录警告，
  不会把已完成任务改回失败；
- 失败或未完成的任务可以保留自己的日志；启动时清理已完成任务的残留，`transfers/` 总体超过
  8MB 或 10 个文件时删除最旧的文件；
- 不会在成功后清空全部共享日志，也不会删除任务数据库记录（完成记录仍用于去重和 `/confirm`）。

## 快速开始

1. 下载 `TG115-Deployer-v1.5.0.exe`（本地开发版尚未发布，见“项目状态”）；
2. 对照 `SHA256SUMS.txt` 校验文件；
3. 双击部署器，按 ① 连接服务器、② Telegram 信息填写；“高级选项”里的 VPS 配置保持默认的
   “最小配置”（2GB 预算 / 6GB 保留），需要更大额度时改选“宽裕配置”或直接修改数字；
4. 在 ③ 保存位置选择目标网盘（界面只有两个选项）：
   - `115`：在 CloudDrive2 中挂载 115，文件默认存到 `115/telegram转存`；
   - `Google Drive`：先在 CloudDrive2 中登录并挂载 Google Drive，文件默认存到
     `GoogleDrive/telegram转存`；
   - 文件夹不存在时 Bot 会自动创建；留空或只填网盘名也不会存到网盘根目录；
5. 同在 ③：CloudDrive2 装在这台 VPS 上就保持勾选（默认，地址自动填好且不能改）；
   用别处已有的 CloudDrive2 时取消勾选，填它的 WebDAV 地址 `http://IP:19798/dav`；
   然后填 WebDAV 用户名、密码；
6. 在 ④ 点“一键部署”；完成后点“登录并挂载网盘”确认目标网盘已挂载，再点“检查是否可用”；
7. 在 Telegram 私聊自己的 Bot 并转发文件。

不要把 VPS 密码、SSH 私钥、Bot Token、Telegram API Hash、CloudDrive2 WebDAV 密码、115 登录
信息或 Google 账号密码提交到仓库、Issue、截图或聊天记录。

## Bot 消息

Bot 只主动发三类消息：开始传输（文件名、大小、目标网盘和目录）、传输完成（大小、路径、用时）、
任务失败（最终失败时一次，附原因和 `/retry` 提示）。进度不主动推送，用 `/task <编号>` 或 `/status`
查看百分比、已传大小、速度和剩余时间。磁盘触及安全线暂停下载时会额外提醒一次。

## 状态含义

- `在排队`：任务已经保存，等待系统动态放行；
- `正在从 Telegram 下载`：VPS 正在接收 Telegram 文件；
- `正在从 Telegram 流式写入 CloudDrive2`：大文件不完整落盘，边读取边写入；
- `正在写入 CloudDrive2`：VPS 正在向 CD2 WebDAV 写入；
- `Bot 传输已完成，115 官方端待确认` / `Bot 传输已完成，Google Drive 端待确认`：Bot 已完成远端校验和本地清理；
- `115 官方端已由你确认` / `Google Drive 端已由你确认`：用户在对应网盘官方客户端核验后执行了 `/confirm`。

`/confirm` 只记录人工确认，不会再次上传、移动或删除文件。WebDAV 验收只证明 CloudDrive2
接收并通过大小校验，不能证明 115 或 Google Drive 官方端已经入库。

普通文件先完整落盘，上传失败时可以保留本地副本；单文件超过本地预算时使用流式模式，
不会占用完整文件大小的本地额度，但中断后需要从头重新传输。CloudDrive2 自身仍可能使用
缓存，因此磁盘安全线继续生效。

### 传输方式（`TRANSFER_MODE`）

| 值 | 部署器选项 | 行为 |
| --- | --- | --- |
| `auto`（默认） | 自动：小文件先落盘，大文件流式 | 与旧版一致：单文件不超过本地任务预算时先落盘再上传，超过时流式 |
| `stream` | 全部流式 | 所有文件都走 Telegram → rclone → CloudDrive2 流式，不占本地任务预算 |

全部流式适合小硬盘 VPS：Bot 不再在本机保存完整文件，多个文件可以同时传输而不受本地预算限制。
代价是中断后整个文件从头重传、不保留本地副本可供重新上传；CloudDrive2 仍会缓存正在上传的数据，
所以磁盘最少保留和磁盘紧急保护照常生效。旧的 `.env` 没有这个键时按 `auto` 处理；
`/status` 会显示当前传输方式。

## 安全边界

CloudDrive2 是第三方闭源软件，FUSE 挂载需要较高容器权限。两条路线都依赖 CloudDrive2，
建议使用只运行本项目的独立 VPS，不要与钱包、数据库或其他重要服务共用。WebDAV 用户名和
密码等同于该 CloudDrive2 实例的访问权限，必须按 secret 保护。详见
[安全政策](SECURITY.md)和[第三方组件说明](docs/第三方组件说明.md)。

## 项目状态

当前仓库是**本地开发版，尚未发布**：`APP_VERSION` 仍为 `v1.5.0`，Google Drive 支持只在
本地完成代码、脚本和自动化测试验证，没有发布新的 Release，也没有在真实 VPS、真实
CloudDrive2 挂载或真实 Google 账号上完成端到端传输验收。发布前验证结果见
[验收与复核报告](docs/验收与复核报告.md)。

本项目与 Telegram、115、CloudDrive2、Google 及其运营方没有隶属、授权或官方合作关系。

## 许可证

[MIT](LICENSE)
