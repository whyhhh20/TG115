# 安全政策

## 不要公开秘密

提交 Issue 或日志前必须删除：

- VPS 地址、用户名、密码和 SSH 私钥；
- Telegram Bot Token、API ID、API Hash 和个人数字 ID；
- CloudDrive2 WebDAV 地址、用户名和密码；
- CloudDrive2、115、Google 的登录信息、Cookie、二维码和授权凭据；
- 文件名、聊天内容及其他个人数据。

本项目不会要求用户把这些信息发送给维护者。115 和 Google Drive 的登录/授权都在
CloudDrive2 管理页由用户本人完成，本程序不接触 Google OAuth token、client ID 或 client
secret。

## 报告安全问题

不要在公开 Issue 中发布可利用细节或真实凭据。请使用 GitHub 的私密安全报告功能。
在仓库没有启用私密安全报告前，不要提交真实秘密；只提交已脱敏的复现步骤。

## 信任边界

CloudDrive2 是第三方闭源组件，115 与 Google Drive 两条路线都依赖它。其特权容器、内部缓存、
网盘登录和上传实现不属于本项目的可审计源码范围。本项目只能验证 WebDAV 接收和远端大小，
不能证明第三方组件内部实现，也不能证明 115 或 Google 官方端状态。

WebDAV 用户名和密码等同于该 CloudDrive2 实例的访问权限，只写入 VPS 上权限 600 的 `.env`，
不会打印到日志；泄露后应立即在 CloudDrive2 中修改。rclone 的本次 stderr 会写入该任务的
`logs/transfers/<task_id>.log`，返回给异常信息的文本仍然截断，且不会写入 WebDAV 密码。

## 日志与磁盘边界

日志不保存凭据，也不会无限增长：

- 应用共享日志 `logs/tg115.log` 按 2MB 轮转、最多 2 份，保留最近错误供排查；
- `payload/docker-compose.yml` 中 `clouddrive2` 与 `tg115-bot` 两个服务都使用 `json-file`
  日志驱动，`max-size: 5m`（5MB）、`max-file: "2"`，容器标准输出不会持续占用磁盘；
- 每个任务最多一个 `logs/transfers/<task_id>.log`，单文件上限 1MB；任务进入 `completed`
  后立即删除该文件，删除失败只记警告，不改变已完成状态；
- 失败或未完成的任务可以保留自己的日志；启动时清理已完成任务的残留，`transfers/` 总体
  超过 8MB 或 10 个文件时删除最旧文件；
- 成功后不会清空全部共享日志，也不会删除任务数据库记录（完成记录仍用于去重和 `/confirm`）。

因此 20GB 硬盘上的最小配置不会因为日志累积而耗尽空间，但 CloudDrive2 自身的缓存不计入
Bot 的 2GB 本地任务预算，仍需关注真实剩余空间。
