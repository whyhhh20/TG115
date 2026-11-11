# Telegram → 115 / Google Drive 一键部署器：小白使用说明

> 版本：1.6.0 正式版；适用电脑：Windows 10 / Windows 11 64 位；适用 VPS：
> Ubuntu 或 Debian 64 位；默认推荐 VPS：1 核 CPU、1GB 内存、20GB 硬盘（最小配置），
> 宽裕配置建议内存至少 2GB、硬盘约 50GB。
>
> 每次部署选择一个目标网盘：`115`（默认）或 `Google Drive`。两条路线都经过 CloudDrive2：
>
> ```text
> Telegram → VPS → CloudDrive2 → 115
>                             → Google Drive
> ```
>
> CloudDrive2 是共同传输层。你要做的是：在 CloudDrive2 里挂载对应网盘，然后在部署器里
> 选择对应网盘并填写该网盘在 WebDAV 根目录下的相对路径。本程序不处理 Google token 或
> client ID/secret。

---

## 新版界面：从上往下按 ① → ④ 填

部署器窗口从上往下就是操作顺序，只有一个主按钮“一键部署”：

| 顺序 | 区域 | 要填 / 要点什么 |
| --- | --- | --- |
| ① | 连接服务器 | VPS 地址（IP）、用户名（通常 root）、SSH 端口（通常 22）、登录密码或 SSH 私钥。想先确认能连上，可以点“测试连接”（一键部署也会自动测）。 |
| ② | Telegram 信息 | Bot Token（找 `@BotFather` 发 `/newbot`）、API ID 和 API Hash（`my.telegram.org` → API development tools）、你的数字 ID（找 `@userinfobot`）。 |
| ③ | 保存位置 | 点选 `115` 或 `Google Drive`；文件夹默认 `115/telegram转存` / `GoogleDrive/telegram转存`（自动创建）；确认 CloudDrive2 WebDAV 地址；填 WebDAV 用户名和密码。 |
| ④ | 部署并检查（固定在窗口下方） | 第 1 小步点“一键部署（115 / Google Drive）”，约 5～15 分钟；部署成功后第 2 小步点“登录并挂载网盘”；挂载好后第 3 小步点“检查是否可用”。 |

已经部署过？不用重新安装，直接做 ④ 的第 2、3 小步。安装成功不等于可用，第 3 小步通过才算可用。

“高级选项”默认折叠，一般不用改：

- 容量与传输方式：VPS 配置（默认“最小配置”）、本地任务预算 2GB、磁盘最少保留 6GB、传输方式（默认“自动”）；
- 服务器细节：sudo 密码（用户名不是 root 时才填）、安装目录 `/opt/tg115`、时区 `Asia/Shanghai`；
- 排查工具：“修复网络”只修复托管 CD2 的网络；“查看 VPS 状态”只读查询容器状态和最近日志；“清空日志”只清空本地显示。

出错时，弹窗和下方“部署记录”会写一句“下一步”，例如“下一步：到「② Telegram 信息」改好‘Bot Token’，
再点刚才的按钮。”窗口会自动滚动到出错的输入框，并在框下方用红字标出；出错的是高级选项里的设置时会自动展开。
SSH 认证失败或连接超时会提示回到 ① 核对。

初始状态为“等待操作，网盘状态尚未检查”。安装完成、网络修复、网盘检查各自显示结果；失败保留“操作失败”，
不会再被“已完成”盖住。检查通过表示 CD2 接收和文件操作成功，最终是否进入目标网盘仍以官方端查看为准。

下文沿用的一些旧称对应新版按钮：“一键部署基础环境”就是 ④ 的“一键部署”，“打开 CloudDrive2 管理页”就是
“登录并挂载网盘”，“WebDAV 验收”就是“检查是否可用”，“测试 SSH”就是 ① 的“测试连接”。

## 一、它能自动做什么

按 ①②③ 填好后在 ④ 点击“一键部署”，程序会自动：

1. 通过密码或 SSH 私钥连接 VPS；
2. 显示并确认 VPS 主机密钥指纹；
3. 检查 Linux 系统和 CPU 架构（内存、磁盘只记录并提示，不作为安装门槛）；
4. 上传完整部署包；
5. 安装 Docker、Docker Compose 等运行环境（托管 CloudDrive2 时另外安装 FUSE）；
6. 按你的勾选安装并启动 CloudDrive2 容器，或复用 VPS 上已有的 CloudDrive2；
7. 构建并启动 Telegram Bot；
8. 建立 SQLite 持久化任务队列；
9. 按所选 VPS 配置写入本地任务预算和磁盘安全线（默认最小配置：2GB 预算、6GB 保留）；
10. 单文件超过预算时自动切换为不完整落盘的流式传输；
11. 启用 CPU、内存、磁盘、网络和错误率动态调度；
12. 配置异常自动重启、VPS 开机自动启动和日志轮转；
13. 确认 Bot 容器已启动；不运行或等待容器健康检查。

两条路线都有一件事必须由你本人完成：

- 登录 CloudDrive2，挂载你选择的网盘（115 或 Google Drive），并开启 WebDAV。

部署器不会要求 Cursor 账号密码，不会替你登录 115 或 Google，也不会读取这些账号的密码。

---

## 二、使用前必须准备

### VPS

- Ubuntu 22.04 / 24.04，或 Debian 12 64 位；
- 默认推荐最小配置：1 核 CPU、1GB 内存、20GB 硬盘（本地任务预算 2GB、磁盘最少保留 6GB）；
- 宽裕配置（可选）：建议内存至少 2GB、硬盘约 50GB（本地任务预算 20GB、磁盘最少保留 20GB）；
- 大量或长期批量传输：4 核 CPU、8GB 内存、80～100GB SSD；
- 公网 IP 或域名；
- SSH 端口、用户名；
- VPS 登录密码，或者 SSH 私钥；
- 如果登录用户不是 root：准备 sudo 密码，或者确保该用户可以免密 sudo；
- 如果勾选“在 VPS 中安装并管理 CloudDrive2”，VPS 需要支持 `/dev/fuse`；部分容器型 VPS
  需要服务商在控制台开启 FUSE。复用已有 CloudDrive2 时不需要；
- VPS 应能稳定访问 Telegram 和 Docker 镜像仓库，端口带宽建议 100Mbps 或以上，并准备
  足够的月流量。实际速度仍受 Telegram、VPS 线路、CloudDrive2 和网盘状态共同影响。

最小配置的 2GB 只限制 Bot 普通落盘（下载完成、等待上传的完整文件），6GB 是整块磁盘至少
留出的空闲；CloudDrive2 自己的缓存不计入这 2GB，所以仍要关注 `df -h` 的真实剩余空间。
单文件超过 2GB 时会自动使用流式模式，不会先完整写入 VPS，不需要把预算调大；经常批量处理
大文件时建议改用宽裕配置或更大的硬盘。

“传输方式”默认是“自动：小文件先落盘，大文件流式”，和旧版一样。选“全部流式”后，所有文件都
直接从 Telegram 流式写入 CloudDrive2，不占 2GB 本地预算，适合硬盘小的 VPS；缺点是中途断开要
整个文件从头重传。CloudDrive2 自己仍会缓存，所以 6GB 磁盘最少保留照样生效。发送 `/status`
可以看到当前用的是哪种方式。两个配置除了预算和磁盘保留，还决定运行期的内存保护阈值
（最小配置 256MB/128MB，宽裕配置 1024MB/512MB，见下表）；它们都不检测 VPS 内存是否健康。

内存和硬盘数字只是推荐，不是安装门槛：部署脚本不做固定最低内存、总磁盘或空闲空间的安装拦截，
1GB 甚至更小的机器也会继续安装。实际安装步骤出错仍会报错——APT 源、Docker、Compose 构建、
容器启动，以及托管 CloudDrive2 时的 `/dev/fuse` 缺失，都会中断部署并保留完整日志。
安装完成后，运行期磁盘保留线和资源调度继续生效。

### Telegram

- Bot Token：从 `@BotFather` 获取；
- API ID：从 `https://my.telegram.org` 获取；
- API Hash：从 `https://my.telegram.org` 获取；
- 你自己的 Telegram 数字 ID。

### CloudDrive2（两条路线都需要）

- CloudDrive2 会员账号；当前方案按会员环境设计，并要求目标网盘挂载和 WebDAV 功能可用；
- CloudDrive2 WebDAV 用户名；
- CloudDrive2 WebDAV 密码；
- 计划挂载的网盘：115 或 Google Drive，二选一；
- 该网盘的剩余空间；
- 保存文件夹：默认 `115/telegram转存` 或 `GoogleDrive/telegram转存`，一般不用改。

CloudDrive2 会员、115 和 Google Drive 都由你本人在 CloudDrive2 管理页登录挂载。部署器
不需要这些账号的登录密码，只需要你另外设置的 WebDAV 用户名和密码。

### 网盘账号（按选择准备其一）

- 选择 `115`：一个可正常登录、剩余空间足够的 115 账号；115 会员不是程序代码的硬性要求；
- 选择 `Google Drive`：一个空间足够的 Google 账号（个人 My Drive 或你有权限的共享盘），
  在 CloudDrive2 中挂载即可。不需要 OAuth token、client ID/secret 或文件夹 ID。

---

## 三、第一次部署

### 第 1 步：运行

推荐直接双击：

```text
TG115-Deployer.exe
```

也可以双击：

```text
一键部署-Telegram到115.cmd
```

Windows 可能显示“未知发布者”，原因是本程序没有购买商业代码签名证书。你可以先在
`SHA256SUMS.txt` 中核对文件校验值。

### 第 2 步：① 连接服务器

- `VPS 地址（IP）`：服务商提供的公网 IP 或域名；
- `用户名`：通常是 root（也可能是 ubuntu、debian）；
- `SSH 端口`：通常是 22；
- `登录方式`：选择“密码”或“SSH 密钥”；
- 密码登录：填写 VPS 登录密码；
- 密钥登录：点“选择”找到私钥文件，有口令时再填写私钥口令；
- 用户名不是 root：到“高级选项 → 服务器细节”填写 sudo 密码；已经配置免密 sudo 可以留空。

可以先点“测试连接”（可选，一键部署也会自动测试）。

首次连接会出现 VPS 主机密钥指纹。应当与 VPS 服务商控制台显示的指纹核对，一致后才能点击“是”。

### 第 3 步：② Telegram 信息

- `Bot Token`：在 Telegram 找 `@BotFather`，发 `/newbot` 创建机器人，复制它回复的 Token；
- `API ID`、`API Hash`：打开 `https://my.telegram.org` → API development tools；
- `你的数字 ID`：在 Telegram 找 `@userinfobot`，它回复的 Id 就是；只有这个账号能使用 Bot。

API Hash、Bot Token 都是秘密信息，不要发到群聊或公开网页。

### 第 4 步：③ 保存位置

在“保存到”处点选一个目标，界面只有两个选项：

- `115`（默认）；
- `Google Drive`。

然后从上往下填：

1. **文件夹**：默认已经填好，一般不用改：
   - 115：`115/telegram转存`；
   - Google Drive：`GoogleDrive/telegram转存`。

   这里的 `115`、`GoogleDrive` 是网盘在 CloudDrive2 里的挂载名（在 CloudDrive2 文件列表最上一层
   看到的名字）；如果你的挂载名不一样（例如 `115open`），把前半段改成你的挂载名即可。
   文件**不会存到网盘根目录**：留空或只填网盘名（例如只填 `115`）时，部署器会自动改成
   `115/telegram转存`。`telegram转存` 文件夹不存在也没关系，Bot 启动后和第 3 小步
   “检查是否可用”时会自动创建。想存到别的文件夹，直接写完整路径，例如 `115/电影/Telegram`。
   115 与 Google Drive 的文件夹各自独立保存，切换时互不覆盖。
2. **CloudDrive2 装在这台 VPS 上（推荐）**：默认勾选。勾选时 WebDAV 地址自动是
   `http://clouddrive2:19798/dav`（同一台 VPS 内部地址），输入框变灰不用改。
3. **WebDAV 地址**：只有取消上面的勾选、用别处已经装好的 CloudDrive2 时才要填，
   格式是 `http://IP:19798/dav`（IP 换成那台 CloudDrive2 机器的地址）；
   走公网必须用 `https://域名/dav`，否则密码会明文传输。
4. **WebDAV 用户名**、**WebDAV 密码**：CloudDrive2 开启 WebDAV 时设置的那一组。

### 第 5 步：高级选项保持默认

“高级选项”默认折叠，不用打开。里面的默认值是：

```text
VPS 配置：最小配置（推荐）
本地任务预算：2GB
磁盘最少保留：6GB
传输方式：自动：小文件先落盘，大文件流式（默认；想全部流式就选“全部流式”）
安装目录：/opt/tg115
时区：Asia/Shanghai
```

VPS 配置有两个选项：

| 选项 | VPS | 本地任务预算 | 磁盘最少保留 | 内存软/硬阈值 |
| --- | --- | --- | --- | --- |
| 最小配置（推荐，默认） | 1 核 / 1GB / 20GB 硬盘 | 2GB | 6GB | 256MB / 128MB |
| 宽裕配置 | 建议内存至少 2GB、硬盘约 50GB | 20GB | 20GB | 1024MB / 512MB |

切换选项时写入预算和保留值（之后可以手工修改，部署时以输入框为准），内存阈值随选项自动写入。不会按配置检测内存健康，
也不会把推荐配置当成安装门槛。

③ 里的“CloudDrive2 装在这台 VPS 上”与选择哪个网盘无关：勾选时由 VPS 托管 CD2，Bot 固定使用容器内网地址
`http://clouddrive2:19798/dav`，不要填写 VPS 公网 IP，也不要把 19798 端口写成 `https://`；该端口本身是 HTTP，
管理页面通过 SSH 隧道安全访问。取消勾选时程序复用 VPS 上已有的 CloudDrive2，不安装、不启动、不修改它的容器和数据，
这时才需要填写 WebDAV 地址。

### 第 6 步：④ 一键部署

在窗口下方的 ④ 点击：

```text
一键部署（115）   或   一键部署（Google Drive）
```

正常需要约 5～15 分钟，主要时间用于：

- VPS 安装 Docker（托管 CD2 时另外安装 FUSE）；
- 下载 CloudDrive2 镜像（仅在需要由 VPS 托管时）；
- 构建 Bot 镜像；
- 安装 Python 依赖；
- 启动容器并连接 CloudDrive2 网络。

不要在部署过程中关闭部署器。填错时会弹窗说明“下一步”去哪里改，改好后再点一次“一键部署”。

---

## 四、在 CloudDrive2 中挂载网盘（两条路线通用）

部署成功后在 ④ 点击第 2 小步：

```text
登录并挂载网盘
```

部署器会建立 SSH 安全隧道，然后在浏览器打开类似地址：

```text
http://127.0.0.1:随机端口
```

这不是公网地址，只有你的电脑通过当前 SSH 隧道才能访问。

在 CloudDrive2 中完成：

1. 登录 CloudDrive2 会员账号；
2. 添加你选择的网盘：
   - 选择 `115` 时添加 115，按 CloudDrive2 提示登录或扫码；
   - 选择 `Google Drive` 时添加 Google Drive，按 CloudDrive2 提示完成 Google 授权；
3. 确认能浏览该网盘文件；
4. 在设置中开启 WebDAV；
5. 不用手动建 `telegram转存` 文件夹，Bot 会自动创建。

Google Drive 的授权完全在 CloudDrive2 内完成，本程序不接触 Google token、client ID/secret
或文件夹 ID，也不直接请求 Google API。

完成后在 ④ 点击第 3 小步：

```text
检查是否可用
```

该验收不是只看容器是否运行，而是会自动执行：

```text
在 VPS 生成 256 字节随机测试文件
→ rclone 上传到 CloudDrive2 WebDAV
→ CloudDrive2 WebDAV 临时文件大小校验
→ 远端改名
→ 再次校验
→ 删除远端和 VPS 测试文件
```

只有输出 `TG115_DESTINATION=OK`，部署器才会显示“验收通过”。这证明文件已经写入
CloudDrive2 WebDAV，但不能单独证明 115 或 Google Drive 官方端已经保存完成。最终应在
对应网盘的官方客户端确认文件大小正常，并能打开或播放。

Bot 每 30 秒自动重新检查 CloudDrive2，不需要重新部署。

---

## 五、两条路线的区别（只有网盘不同）

| 项目 | 115 | Google Drive |
| --- | --- | --- |
| 传输路径 | Telegram → VPS → CloudDrive2 WebDAV → 115 | Telegram → VPS → CloudDrive2 WebDAV → Google Drive |
| 目标配置 | WebDAV 地址、用户名、密码、115 的相对路径 | 同一组 WebDAV 信息、Google Drive 的相对路径 |
| 挂载方式 | 在 CloudDrive2 管理页登录 115 | 在 CloudDrive2 管理页授权并挂载 Google Drive |
| VPS 额外依赖 | 托管 CloudDrive2 时需要 FUSE 和 `/dev/fuse` | 完全相同，取决于是否托管 CloudDrive2 |
| 人工确认 | 在 115 官方客户端确认后 `/confirm` | 在 Google Drive 网页/客户端确认后 `/confirm` |

两条路线使用同一个 rclone `cd2:` WebDAV remote，不存在单独的 Google remote，也不需要在本机
另外执行任何 rclone 授权命令。

### 5.1 切换目标时要注意

- 已持久化但未完成的任务绑定在原来的目标上，切换后 Bot 会明确拒绝把这些任务写到新目标，
  并提示先完成或取消；
- 切换前先发送 `/queue` 查看是否有未完成任务；
- 切换目标不会迁移已经写入旧网盘的文件，也不会删除旧网盘中的任何内容；
- 同一个 VPS 可以先后使用两条路线，但同一时刻只有一个生效目标。

### 5.2 手工检查当前目标

```bash
sudo /opt/tg115/manage.sh destination
```

输出会显示最终网盘（115 或 Google Drive）、共用的 CloudDrive2 WebDAV 相对路径，以及
`rclone remote: cd2:`。

---

## 六、以后怎么使用

Bot 只会主动发三类消息：**开始传输**、**传输完成**、**任务失败**（只在最终失败时发一次）。
唯一例外是磁盘触及安全线导致下载暂停时会提醒一次，因为需要你清理空间。启动、排队、中途重试
都只写日志，不发 Telegram 消息。

1. 在 Telegram 找到视频或文件；
2. 在与自己的私人 Bot 的一对一聊天中转发；
3. Bot 自动登记任务，不再回复“在排队”；真正开始传输时发一条“开始传输 #编号”
   （含文件名、大小、目标网盘和目录）；
4. 进度不会主动推送：想看时发送 `/task <编号>`（百分比、已传大小、速度、剩余时间）或 `/status`；
5. VPS 根据 CPU、内存、磁盘、网络和服务状态动态放行；
6. 不超过本地预算的文件先下载到 VPS，再由 rclone 上传；
7. 超过本地预算的单个文件直接执行 Telegram → rclone → CloudDrive2 流式传输；
8. CloudDrive2 WebDAV 大小验证通过；
9. VPS 删除本地临时文件并释放额度；
10. Bot 发一条完成消息“Bot 传输已完成 #编号”（含大小、网盘路径和用时），
    并自动处理后续任务；如果重试用完仍失败，只发一条“任务失败 #编号”（含原因和 /retry 提示）；
11. CloudDrive2 继续处理网盘写入；这一步不一定出现在“上传任务”列表中；
12. 在 115 官方客户端或 Google Drive 中看到文件大小正常且可以打开或播放；
13. 向 Bot 发送 `/confirm <任务编号>`，把该任务记为“115 官方端已由你确认”或
    “Google Drive 端已由你确认”。

`/confirm` 只记录你的人工确认，不会重新上传，也不会修改或删除网盘中的文件。Bot
当前没有接入可信的 115 或 Google 官方完成接口，因此不能根据 CloudDrive2 文件列表或历史
任务状态自动断言官方端已完成。

你不需要手动分批，也不需要重新转发已经获得任务编号的文件。当前正式支持范围是
“你本人和 Bot 的一对一私聊”；不要把 Bot 放进私人频道或群组代替私聊，否则频道身份
可能与配置的个人数字 ID 不同而被安全规则拒绝。

文件直接保存在 ③ 填的文件夹中，不再按年份和月份建立子目录。例如默认设置下最终路径为：

```text
115/telegram转存/视频文件名.mp4
GoogleDrive/telegram转存/视频文件名.mp4
```

---

## 七、Bot 命令

```text
/start
/help
/queue
/status
/performance
/task <任务编号>
/confirm <任务编号>
/retry <任务编号>
/cancel <任务编号>
```

---

## 八、重新运行部署器会怎样

重新点击 ④ 的“一键部署”用于：

- 修改 Telegram 或 CloudDrive2 WebDAV 配置；
- 更换目标网盘；
- 更新 Bot 程序；
- 修复损坏的部署；
- 更新容器配置并移除旧的 Bot 健康探针。

程序会保留：

- SQLite 任务数据库；
- 下载目录；
- 日志；
- CloudDrive2 配置和挂载数据（由 VPS 托管时）；
- 已部署的 CloudDrive2 容器和登录状态（复用已有 CD2 时完全不动）。

重新部署后，Bot 会把已有 `rclone.conf` 中的 `cd2` 配置更新为本次填写的 WebDAV 地址、
用户名和密码，不会继续使用旧凭据。切换网盘时只改变写入的 CD2 相对路径，不会删除另一条
路线留下的配置和数据。

注意：只在输入框中改值后直接点击“检查是否可用”不会更新 VPS；必须先重新点击
“一键部署”，看到部署成功后再检查。

如果日志出现 `lookup clouddrive2`，打开“高级选项”，在“排查工具”里点击“修复网络”。两条路线都可以使用该
修复：它会保留现有 CloudDrive2 登录、网盘挂载和文件，只刷新 Docker 网络别名，并自动
执行真实 WebDAV 验收。

更新前，旧程序配置会备份到：

```text
/opt/tg115-backups/
```

---

## 九、安全设计

- VPS 密码、私钥口令和 sudo 密码不会写入本地配置文件；
- 本地临时配置在部署结束后自动删除；
- 上传到 VPS 的临时目录为随机名称，权限为 700，结束后自动删除；
- VPS 正式 `.env` 权限设置为 600；
- rclone 将 WebDAV 密码转换为 obscure 格式；
- 本程序不保存、不传输任何 Google OAuth token、client ID 或 client secret；
- CloudDrive2 和 Python 基础镜像锁定到经过复核的不可变 SHA-256 摘要；
- 普通重新部署不会自动拉取未经复核的新基础镜像；
- CloudDrive2 的 19798 端口只绑定 VPS 的 `127.0.0.1`；
- 管理页通过 SSH 隧道访问，不直接暴露公网；
- 只有配置的 Telegram 数字 ID 能够使用 Bot；
- Bot 容器使用专用非 root 用户运行，并启用只读根文件系统、移除 Linux capabilities；
- VPS 的配置、下载、日志和备份目录仅允许对应服务用户或 root 访问；
- 安装目录经过完整校验和 shell 参数转义，不能把异常路径当成远程命令执行；
- Docker 构建上下文使用白名单，只包含 Dockerfile、依赖清单和 Bot 源码，不包含
  `.env`、下载文件、日志或 CloudDrive2 挂载内容；
- 普通落盘模式在远端验证成功前不会删除本地完整文件；
- 普通落盘模式上传失败会保留本地完整文件；
- 流式模式使用准确文件大小和背压控制，不在 VPS 保留完整副本；中断后会从头重试；
- `/cancel` 必须先确认本任务远端文件已经清理，才会删除 VPS 本地副本并标记取消。

注意：CloudDrive2 为实现 FUSE 挂载仍需特权容器和宿主机 PID 命名空间；因此建议这台 VPS
只运行本项目。服务器 root 用户始终能够读取容器配置，任何 VPS 自动化都无法防范已经取得
root 权限的攻击者。

最小配置的 2GB 是 Bot 普通落盘模式的下载和待上传文件预算（宽裕配置为 20GB）。大于该预算的
单文件使用流式模式，不按完整文件大小占用这笔额度。CloudDrive2 是专有程序，可能为目标网盘
后台上传建立额外缓存；程序会监控整个 VPS 分区的真实剩余空间并在安全线（最小配置 6GB、
宽裕配置 20GB）触发时暂停新传输，但不能保证 CloudDrive2 的内部缓存也严格限制在 2GB。
若其任务长期堆积，应暂停继续转发；需要硬隔离时应给 CloudDrive2 使用单独数据盘或文件系统配额。

日志也不会无限增长：共享日志 `logs/tg115.log` 2MB × 2 份，两个容器的 `json-file` 日志
5MB × 2 份，每次传输的 `logs/transfers/<task_id>.log` 单文件最多 1MB 并在任务完成后删除。

---

## 十、常见问题

### 提示 VPS 没有 `/dev/fuse`

只有勾选“在 VPS 中安装并管理 CloudDrive2”时才需要 FUSE。请在 VPS 控制台开启 FUSE，或
联系服务商；也可以取消该勾选，复用 VPS 上已有的 CloudDrive2。

### CloudDrive2 里看不到我选择的网盘

两条路线都要求先在 CloudDrive2 管理页挂载对应网盘：115 需要登录或扫码，Google Drive 需要
在 CloudDrive2 内完成 Google 授权。挂载完成并能浏览文件后，再执行 WebDAV 验收。

### Google Drive 文件没有出现

本程序不直接访问 Google API，因此先检查 CloudDrive2：网盘是否仍处于登录状态、WebDAV 是否
开启、相对路径是否指向已挂载的目录，以及 Google 账号剩余空间是否足够。

### 部署脚本提示 DESTINATION_TYPE 取值错误

只接受 `clouddrive2`（或界面别名 `115`）和 `gdrive`（或别名 `google_drive`）。请检查部署器
选择的目标，不要手工写入其他值；脚本不会猜测目标，也不会在取值错误时继续部署。

### Bot 一直显示在排队

依次检查：

1. CloudDrive2 是否已经登录；
2. 选择的网盘是否已经挂载；
3. WebDAV 是否开启；
4. WebDAV 用户名、密码是否正确；
5. 目标相对路径是否正确；
6. `/status` 中 CloudDrive2 是否正常；
7. VPS 是否还有足够磁盘空间。

### 单文件超过本地预算会怎样

它不会永久排队，也不要求把本地预算调大。系统会自动标记为流式模式，一边从 Telegram
读取，一边通过 rclone 写入 CloudDrive2 WebDAV。流式模式不会在 VPS 保留完整副本，所以
线路中断后需要从头重试；CloudDrive2 自身仍可能使用缓存，磁盘安全线仍然有效。

最小配置的预算是 2GB，所以超过 2GB 的单文件默认走流式；宽裕配置的预算是 20GB。

### VPS 内存只有 1GB 甚至更少（例如 921MB）能装吗

不会再因 921MB 内存在安装前被直接拒绝。内存和硬盘数字只是推荐，部署脚本不做固定最低内存、总磁盘或空闲空间的安装拦截；
真正会中断部署的是实际安装步骤出错（APT 源、Docker、Compose 构建、容器启动、托管
CloudDrive2 时的 `/dev/fuse`），失败会保留完整日志并停止。

安装成功后，运行期仍按实时可用内存放行，这部分保护不会被关掉：

- 可用内存低于硬阈值（最小配置 128MB，宽裕配置 512MB）时，下载和上传窗口降为 0，任务停在“在排队”；
- 可用内存在硬阈值至不足软阈值（最小配置 256MB，宽裕配置 1024MB）时，下载和上传窗口分别逐步收缩到 1；
- 磁盘剩余小于或等于“磁盘最少保留”（最小配置 6GB）时同样暂停新下载，并清理不完整文件。

窗口因内存或磁盘被压到 0 时，Bot 日志会定期打印一条带实际数值的警告，`/status` 里也会显示
“暂停原因”。实测 921MB 的 VPS 在 Docker、Bot 和 CloudDrive2 一起运行后约有 340MB 可用：
旧版固定的 512MB 硬阈值会让任务一直排队，所以最小配置改为 256MB/128MB。建议先转发一个
5～20MB 的小文件，用 `/status`、`/performance` 观察可用内存和下载/上传动态窗口。

### 传输日志会不会把 20GB 硬盘占满

不会。共享日志 `logs/tg115.log` 按 2MB 轮转、最多 2 份；Bot 和 CloudDrive2 两个容器都用
`json-file` 日志驱动，单个文件 5MB、最多 2 份；每次传输的 rclone 输出单独写到
`logs/transfers/<task_id>.log`（最多 1MB），任务进入 `completed` 后立即删除该文件。失败或
未完成的任务可以保留自己的日志，`transfers/` 总体超过 8MB 或 10 个文件时会删除最旧的文件。
成功后不会清空全部共享日志，任务数据库记录也不会被删除。

### 官方客户端已经能看到文件，Bot 为什么还显示“待确认”

这是正常且刻意保守的状态。`Bot 传输已完成，115 官方端待确认`（或 Google Drive 版本）
表示 VPS 已经把文件写入 CloudDrive2 WebDAV，并完成远端大小校验和本地清理；它不是
“仍在占用 VPS 上传带宽”。

确认官方客户端中的文件大小正常并可以打开或播放后，发送：

```text
/confirm <任务编号>
```

状态会变成“115 官方端已由你确认”或“Google Drive 端已由你确认”。如果 `/status` 显示
“Bot 当前实际传输：下载/流式 0，落盘后上传 0”，说明 Bot 此刻没有实际传输任务；
历史任务统计不代表当前网速。

### SSH 连接失败

检查 IP、SSH 端口、用户名、密码或私钥；同时检查 VPS 服务商的安全组是否放行 SSH 端口。

### 部署器被安全软件提示

单文件 EXE 由 PyInstaller 打包，部分安全软件会对未签名的自解压程序作启发式提示。请核对
`SHA256SUMS.txt`，也可以直接查看随包附带的完整源代码。

### 想查看 VPS 日志

通过 SSH 登录 VPS 后执行：

```bash
sudo /opt/tg115/manage.sh logs
```

只查看状态：

```bash
sudo /opt/tg115/manage.sh status
```

查看当前目标网盘和共用的 CD2 WebDAV 路径：

```bash
sudo /opt/tg115/manage.sh destination
```

手动执行与部署器相同的写入验收（115 或 Google Drive 都适用）：

```bash
sudo /opt/tg115/manage.sh verify
```

重启 Bot：

```bash
sudo /opt/tg115/manage.sh restart
```

---

## 十一、当前验证边界

当前版本为 1.6.0。已经完成代码审查、自动化测试（含部署脚本的
`--check-config` 配置校验、Compose 结构与文档检查）、ShellCheck 静态检查、Python 依赖
漏洞扫描和容器镜像高危漏洞扫描。

以下内容**没有**在真实环境验证，不能视为已通过：

- 真实 VPS 上经 CloudDrive2 写入 Google Drive 的端到端上传；
- 在 CloudDrive2 中挂载 Google Drive 的真实授权与 WebDAV 行为；
- 真实 VPS 上的 CloudDrive2 / 115 真实 WebDAV 与 115 入库；
- 真实 Telegram 账号的收发；
- 最小配置（1 核 / 1GB / 20GB）与传输日志清理的真实 VPS 运行效果，目前只有本地代码、
  Compose 配置和文档验证；
- 低于推荐内存的机器（如 921MB）在真实 VPS 上的安装与运行效果，目前只有本地 Bash 预检
  脚本的模拟结果。

由于没有你的真实 VPS、Telegram、CloudDrive2 和网盘凭据，交付前无法替你完成真实 VPS
的端到端上传测试。第一次使用时，部署器会在你的 VPS 上执行真实安装并确认容器启动；完成
CloudDrive2 网盘挂载后，建议先转发一个 5～20MB 的测试文件，确认文件出现在目标网盘并收到
Bot 完成通知，再开始批量使用。
