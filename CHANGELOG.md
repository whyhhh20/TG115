# 更新记录

## v1.6.0

在 v1.5.0 基础上的功能更新版，`APP_VERSION` 为 `1.6.0`。自动化测试覆盖部署器、部署脚本和 Bot；
CloudDrive2 内部缓存与网盘官方端最终入库状态仍需用户在自己的环境中确认。

用户能看到的变化：

- 部署器界面整理为 ① 连接服务器 → ② Telegram 信息 → ③ 保存位置 → ④ 部署并检查，只有一个主按钮，
  不常用设置收进默认折叠的“高级选项”，出错时提示“下一步”并定位到出错的输入框；
- CloudDrive2 WebDAV 地址和“CloudDrive2 装在这台 VPS 上”勾选框移到 ③ 保存位置；
- 保存文件夹默认 `115/telegram转存` / `GoogleDrive/telegram转存`，不存在时由 Bot 用 `rclone mkdir`
  自动创建；部署器不会把留空或只填网盘名的文件夹写成网盘根目录，明确填写的路径保持不变；
- Bot 只主动发送开始传输、传输完成和最终失败三类消息，进度通过 `/task <编号>` 和 `/status` 查看；
- 新增 `TRANSFER_MODE=auto|stream`：默认 `auto`，`stream` 让所有文件都流式传输；
- 部署结束前确认 Bot 容器稳定运行；新增只读“查看 VPS 状态”；部署日志去掉终端控制字符；
  上传到 VPS 的部署包和 Docker 构建上下文排除 `__pycache__` / `*.pyc`；
- 运行期内存下限随 VPS 配置缩放，`/status` 和日志会说明因内存或磁盘暂停的原因。

开发过程中的详细技术变更：

- 新增 `DESTINATION_TYPE=clouddrive2|gdrive`，每次部署选择一个目标网盘，缺失时默认
  `clouddrive2`，未知值由部署脚本和 Bot 双方拒绝；界面别名 `115` 归一为 `clouddrive2`，
  `google_drive` 归一为 `gdrive`；
- 两条路线都经过 CloudDrive2：`Telegram → VPS → CloudDrive2 → 115` 或
  `Telegram → VPS → CloudDrive2 → Google Drive`，rclone 只作为内部 WebDAV 客户端使用同一个
  `cd2:` remote（`type=webdav`），不再创建 Google `drive` 类型 remote；
- 两条路线都要求 `CD2_WEBDAV_URL_B64`、`CD2_WEBDAV_USERNAME_B64`、
  `CD2_WEBDAV_PASSWORD_B64` 和可选的 `CD2_TARGET_PATH_B64`（选中网盘在 WebDAV 根目录下的
  相对路径，空值表示 WebDAV 根目录已指向该网盘）；部署脚本不再读取或校验 Google token、
  client ID/secret、根目录 ID；
- 删除“Google Drive 模式强制 `DEPLOY_CLOUDDRIVE2=false`”以及跳过 FUSE、跳过 CD2 容器、
  跳过网络修复的全部分支：是否托管 CloudDrive2 只由用户勾选 `DEPLOY_CLOUDDRIVE2`
  （默认 `true`）决定，与目标网盘无关；两条路线都可以打开 CD2 管理页、修复 CD2 网络并
  执行 WebDAV 验收；
- `clouddrive2` 服务继续放在 Compose profile 中，只有显式 `--profile clouddrive2` 才会
  启动，复用 VPS 上已有的 CloudDrive2 时不会误启动或修改它；
- 部署脚本新增 `--check-config`：无需 root 即可校验目标类型（含别名归一）、CD2 WebDAV
  必填项和 Base64 格式，并输出 `TG115_DESTINATION_LABEL`；
- 修复脚本不再拒绝 Google Drive 目标，两条路线共用同一套 CD2 网络修复与 WebDAV 验收；
- `manage.sh destination` 显示最终网盘（115 或 Google Drive）、共用的 CloudDrive2 WebDAV
  相对路径和 `cd2:` remote，不再显示 `gdrive:`；
- README、小白使用说明、填写信息清单、验收报告、第三方组件说明和 SECURITY 改为“在
  CloudDrive2 中挂载对应网盘，再选择对应 WebDAV 相对路径”，并明确本程序不处理 Google
  token/client；
- 部署器新增 VPS 配置预设，默认并推荐 `minimum`（1 核 / 1GB / 20GB 硬盘，本地任务预算
  2GB、磁盘最少保留 6GB），保留 `standard` 宽裕配置（20GB 预算、20GB 保留）。两项都只是
  推荐，不按所选配置检测内存健康，内存阈值固定为 1024MB/512MB；切换预设只写入输入框，
  部署时以用户实际填写的预算和保留值为准；未设置 `LOCAL_TEMP_BUDGET_GB` 时
  `Settings.from_env()` 仍得到 20GiB；
- 安装阶段不再按固定最低总内存、总磁盘或空闲空间拦截安装：`payload/remote_install.sh` 原先在
  总内存低于 1800MB 或根分区可用空间低于 8GB 时直接失败（日志形如
  `[TG115][ERROR] 内存不足 2GB，当前约 921MB`），现在这些数字只作为推荐值与提示；
  APT 源、Docker、Compose 构建、容器启动和托管 CloudDrive2 时的 `/dev/fuse` 等实际安装步骤
  出错仍会报错并停止。运行期磁盘保留与资源调度保护不变，`MEMORY_SOFT_MIN_MB=1024` /
  `MEMORY_HARD_MIN_MB=512` 仍是运行期放行阈值：可用内存低于 512MB 时窗口降为 0，512MB～1024MB
  之间下载与上传窗口分别逐步收缩到 1；
- README、小白使用说明、填写信息清单同步说明“1C1G20GB 与宽裕配置只是推荐，不是安装门槛”，
  并新增“VPS 内存只有 1GB 甚至更少（例如 921MB）能装吗”的说明；
- 传输日志按任务限额：应用日志改为 2MB × 2 份轮转；`payload/docker-compose.yml` 的
  `clouddrive2` 与 `tg115-bot` 两个服务都使用 `json-file` 日志驱动（`max-size: 5m`、
  `max-file: "2"`）；每次传输的 rclone stderr 追加到 `logs/transfers/<task_id>.log`
  （单文件最多 1MB），任务 `completed` 后立即删除该文件，失败或未完成任务保留自己的日志；
  启动时清理已完成任务的残留，`transfers/` 超过 8MB 或 10 个文件时删除最旧文件；成功后
  不清空共享日志，也不删除任务数据库记录；
- `tests/test_gdrive_deployment.py` 增加 Compose 日志上限、最小 VPS 预设文案和传输日志
  清理说明的断言；
- README、小白使用说明、填写信息清单、验收报告、CHANGELOG 和 SECURITY 改为以最小 VPS
  为推荐配置，并说明 2GB 预算不含 CloudDrive2 自身缓存、超过预算的单文件仍自动流式传输。

修正前那套“Google Drive 直连、跳过 CloudDrive2”的实现已备份在 `.local/previous-direct-version`，
其 286 项测试结果不代表本轮修正后的验证。

## v1.5.0

- 单文件超过本地任务预算时自动切换为带背压的流式传输，不再永久排队；
- 使用 `rclone rcat --size` 直接从 Telegram 写入 CloudDrive2，并继续执行大小校验和安全改名；
- 修复远端改名与 `/cancel` 竞态导致的远端孤儿文件；
- 修复本地下载完成改名与数据库提交之间崩溃导致的本地孤儿文件；
- 修复不存在的保留文件仍占用本地任务额度；
- CloudDrive2 修复脚本不再把任意占用 19798 端口的容器当作 CloudDrive2；
- SQLite 旧数据库自动新增传输模式字段；
- CI 新增 Linux ShellCheck、Compose 校验、Bot 镜像构建、依赖审计和 Windows EXE 自检；
- 自动化测试增加到 75 项。

## v1.0.0

首个正式版本。

- 提供 Windows 图形化部署器；
- 支持 Telegram 私聊文件自动排队与任务恢复；
- 支持 CloudDrive2 WebDAV 扁平目录写入和远端大小校验；
- 支持 CPU、内存、磁盘和错误率动态并发控制；
- 支持 20GB 本地任务预算和磁盘安全线；
- 区分 Bot 传输完成与 115 官方客户端人工确认；
- 提供 `/confirm <任务编号>`、`/status`、`/task`、`/retry` 和 `/cancel`；
- 通过自动化模拟、静态检查、依赖审计和打包自检。

此前本地构建均为内部测试版本，不作为公开发行版本。
