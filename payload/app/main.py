from __future__ import annotations

import asyncio
import hashlib
import inspect
import logging
import os
import signal
import sys
import time
from collections import deque
from dataclasses import dataclass
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit, urlunsplit

from telethon import TelegramClient, events

from .config import Settings
from .db import TaskDB, destination_matches
from .naming import safe_file_name
from .rclone_client import RcloneClient
from .resources import AdaptiveWindow, ResourceMonitor, ResourceSnapshot
from .transfer_log import remove_task_log, sweep_transfers

DESTINATION_CLOUDDRIVE2 = "clouddrive2"
DESTINATION_GDRIVE = "gdrive"

STATE_LABELS = {
    "queued": "在排队",
    "reserved": "已放行，准备下载",
    "downloading": "正在从 Telegram 下载",
    "streaming": "正在从 Telegram 流式写入 CloudDrive2",
    "downloaded": "下载完成，等待上传",
    "waiting_upload": "下载完成，等待上传",
    "uploading": "正在写入 CloudDrive2",
    "verifying": "正在校验 CloudDrive2 文件",
    "finalizing": "正在生成正式文件名",
    "cleanup_pending": "CloudDrive2 已接收，正在清理 VPS 本地文件",
    "completed": "Bot 传输已完成，115 官方端待确认",
    "confirmed": "115 官方端已由你确认",
    "download_failed": "下载失败",
    "upload_failed_retained": "上传失败，本地文件已保留",
    "verification_failed_retained": "校验失败，本地文件已保留",
    "cancelled": "已取消",
}

GDRIVE_STATE_LABELS = {
    # 两条分支都经 CloudDrive2 传输，只有最终网盘不同；文案必须区分
    # “CloudDrive2 已接收”和“Google Drive 官方端待用户确认”。
    "streaming": "正在从 Telegram 流式写入 CloudDrive2（目标 Google Drive）",
    "uploading": "正在写入 CloudDrive2（目标 Google Drive）",
    "verifying": "正在校验 CloudDrive2 文件（目标 Google Drive）",
    "cleanup_pending": "CloudDrive2 已接收，正在清理 VPS 本地文件",
    "completed": "CloudDrive2 已接收，Google Drive 官方端待确认",
    "confirmed": "Google Drive 官方端已由你确认",
}


@dataclass(frozen=True)
class DestinationProfile:
    """User-facing wording for one destination type."""

    key: str
    label: str
    storage: str
    official: str
    health_name: str
    confirm_command: str
    confirm_usage: str
    confirm_hint: str


DESTINATION_PROFILES = {
    DESTINATION_CLOUDDRIVE2: DestinationProfile(
        key=DESTINATION_CLOUDDRIVE2,
        # 界面选项只有「115」和「Google Drive」；CD2 是共同传输层，不是网盘选项。
        label="115",
        storage="CloudDrive2",
        official="115 官方端",
        health_name="CloudDrive2 WebDAV",
        confirm_command="在 115 官方客户端确认文件",
        confirm_usage="只在 115 官方客户端看到文件大小正常、可以打开或播放后使用。",
        confirm_hint=(
            "在 115 官方客户端看到文件大小正常且可以打开或播放后，"
            "发送 /confirm #{task_id}"
        ),
    ),
    DESTINATION_GDRIVE: DestinationProfile(
        key=DESTINATION_GDRIVE,
        label="Google Drive",
        # 文件先落到 CloudDrive2，最终网盘由 CD2 挂载的 Google Drive 承接。
        storage="CloudDrive2",
        official="Google Drive 官方端",
        health_name="CloudDrive2 WebDAV",
        confirm_command="在 Google Drive 网页或客户端确认文件",
        confirm_usage=(
            "只在 Google Drive 网页或客户端看到文件大小正常、可以打开后使用。"
        ),
        confirm_hint=(
            "在 Google Drive 网页或客户端看到文件大小正常且可以打开后，"
            "发送 /confirm #{task_id}"
        ),
    ),
}


DESTINATION_ALIASES = {
    "115": DESTINATION_CLOUDDRIVE2,
    "google_drive": DESTINATION_GDRIVE,
}


def normalize_destination(value: Any) -> str:
    """Map a configured value to a known destination key."""
    if value is None:
        return DESTINATION_CLOUDDRIVE2
    key = str(value).strip().lower()
    if not key:
        return DESTINATION_CLOUDDRIVE2
    key = DESTINATION_ALIASES.get(key, key)
    if key not in DESTINATION_PROFILES:
        raise ValueError(f"未知的目标网盘类型：{value}")
    return key


def destination_profile(destination: Any = None) -> DestinationProfile:
    """Profile for display; unknown values fall back to the legacy wording."""
    try:
        key = normalize_destination(destination)
    except ValueError:
        key = DESTINATION_CLOUDDRIVE2
    return DESTINATION_PROFILES[key]


def destination_identity(kind: str, *parts: str) -> str:
    """One-way identity for the account/endpoint a task was created for.

    Only a truncated SHA-256 digest is persisted, so a task can prove which
    authorization or endpoint produced it without storing any credential.
    """
    material = "\x00".join((kind, *parts))
    return f"{kind}:{hashlib.sha256(material.encode('utf-8')).hexdigest()[:32]}"


def clouddrive2_identity(settings: Any) -> str:
    """CloudDrive2 identity from the endpoint and user, never the password.

    Changing the WebDAV endpoint or account means the same relative path may
    point at a different disk, so those two values identify the destination.
    Both destination types transfer through CloudDrive2, so both use this
    identity; the final netdisk is separated by the destination type instead.
    """
    endpoint = str(getattr(settings, "cd2_url", "") or "").strip()
    parts = urlsplit(endpoint)
    # Host names ignore case; WebDAV paths can refer to different folders by case.
    url = urlunsplit(
        (
            parts.scheme.lower(),
            parts.netloc.lower(),
            parts.path.rstrip("/"),
            parts.query,
            parts.fragment,
        )
    )
    user = str(getattr(settings, "cd2_user", "") or "").strip()
    if not url and not user:
        return ""
    return destination_identity(DESTINATION_CLOUDDRIVE2, url, user)


def state_label(state: str, destination: Any = None) -> str:
    """Human readable state; the default result stays the historical wording."""
    try:
        key = normalize_destination(destination)
    except ValueError:
        key = DESTINATION_CLOUDDRIVE2
    if key == DESTINATION_GDRIVE:
        return GDRIVE_STATE_LABELS.get(state, STATE_LABELS.get(state, state))
    return STATE_LABELS.get(state, state)


def format_bytes(value: float) -> str:
    size = float(value)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if size < 1024 or unit == "TB":
            return f"{size:.2f}{unit}"
        size /= 1024
    return f"{size:.2f}TB"


def format_duration(seconds: float) -> str:
    """把秒数写成“3 分 54 秒”这类简短中文。"""
    total = max(0, round(seconds))
    hours, rest = divmod(total, 3600)
    minutes, secs = divmod(rest, 60)
    if hours:
        return f"{hours} 小时 {minutes} 分"
    if minutes:
        return f"{minutes} 分 {secs} 秒"
    return f"{secs} 秒"


@dataclass
class TransferRate:
    """最近一次进度采样，只在内存里保存，用于 /status、/task 显示速度。"""

    done_bytes: int
    sampled_at: float
    bytes_per_second: float = 0.0


# 进度采样超过这个时间没有更新时，不再显示速度（可能卡住或已切换阶段）。
RATE_STALE_SECONDS = 15.0
PROGRESS_STATES = {"downloading", "streaming"}
STAGE_ONLY_STATES = {"reserved", "uploading", "verifying", "finalizing", "cleanup_pending"}


def _task_log_kwargs(target: Any, task_id: int) -> dict[str, int]:
    """Pass ``task_id`` only to clients that accept it.

    ``RcloneClient`` gained an optional ``task_id`` for the per-task transfer
    log. Older callers and in-repo stand-ins only implement
    ``(path, size)``/``(path, remote)``; they keep working unchanged.
    """
    try:
        parameters = inspect.signature(target).parameters
    except (TypeError, ValueError):
        return {}
    accepts = "task_id" in parameters or any(
        parameter.kind is inspect.Parameter.VAR_KEYWORD
        for parameter in parameters.values()
    )
    return {"task_id": task_id} if accepts else {}


def setup_logging(settings: Settings) -> None:
    settings.log_dir.mkdir(parents=True, exist_ok=True)
    formatter = logging.Formatter(
        "%(asctime)s %(levelname)s %(name)s: %(message)s"
    )
    root = logging.getLogger()
    root.setLevel(logging.INFO)
    console = logging.StreamHandler(sys.stdout)
    console.setFormatter(formatter)
    # 2 MiB x 2 keeps recent errors on a 20 GB VPS; per-transfer output goes to
    # logs/transfers/<task_id>.log instead of this shared file.
    file_handler = RotatingFileHandler(
        settings.log_dir / "tg115.log",
        maxBytes=2 * 1024**2,
        backupCount=2,
        encoding="utf-8",
    )
    file_handler.setFormatter(formatter)
    root.handlers.clear()
    root.addHandler(console)
    root.addHandler(file_handler)


class TransferService:
    def __init__(self, settings: Settings):
        self.settings = settings
        setup_logging(settings)
        self.log = logging.getLogger("tg115")
        self.db = TaskDB(settings.data_dir / "tg115.db")
        self.client = TelegramClient(
            str(settings.data_dir / "bot"),
            settings.api_id,
            settings.api_hash,
            request_retries=5,
            connection_retries=10,
            retry_delay=3,
            auto_reconnect=True,
        )
        self.rclone = RcloneClient(settings)
        self.monitor = ResourceMonitor(settings.download_dir)
        self.download_window = AdaptiveWindow(settings, "download")
        self.upload_window = AdaptiveWindow(settings, "upload")
        self.snapshot: ResourceSnapshot | None = None
        self.destination_healthy = False
        self.destination_last_checked = 0.0
        self.download_tasks: dict[int, asyncio.Task[None]] = {}
        self.upload_tasks: dict[int, asyncio.Task[None]] = {}
        self.recent_download_errors: deque[float] = deque(maxlen=50)
        self.recent_upload_errors: deque[float] = deque(maxlen=50)
        self._background: list[asyncio.Task[Any]] = []
        self._stop = asyncio.Event()
        self._finalize_lock = asyncio.Lock()
        self._register_handlers()

    def _register_handlers(self) -> None:
        self.client.add_event_handler(self._on_message, events.NewMessage(incoming=True))

    def _authorized(self, sender_id: int | None) -> bool:
        return sender_id == self.settings.allowed_user_id

    def _destination_key(self) -> str:
        return normalize_destination(
            getattr(self.settings, "destination_type", None)
        )

    def _profile(self) -> DestinationProfile:
        return destination_profile(self._destination_key())

    def _binding(self) -> dict[str, str]:
        """Destination binding persisted with every new task.

        两条分支都经 CloudDrive2 传输：账号指纹固定取 CD2 端点与用户，
        目标路径固定取本次选中网盘在 CD2 WebDAV 根目录下的相对路径，
        根目录留空。只有 destination_type 区分最终网盘，所以同一个 CD2
        上的 115 与 Google Drive 任务不会互相认领，也不会被合并去重。
        """
        key = self._destination_key()
        target = str(getattr(self.settings, "destination_target", "") or "")
        if not target:
            target = str(getattr(self.settings, "cd2_target", "") or "")
        return {
            "destination_type": key,
            "destination_target": target,
            "destination_root": "",
            "destination_account": clouddrive2_identity(self.settings),
        }

    @staticmethod
    def _is_legacy_direct_gdrive(task: dict[str, Any]) -> bool:
        """旧版直连 Google Drive 创建的任务。

        那一版把 Google 文件夹 ID 写进 destination_root、把 OAuth 授权指纹
        写进 destination_account。这些绑定无法通过 CloudDrive2 复现，因此只能
        阻止，不能自动改写成新的 CD2 绑定。
        """
        if str(task.get("destination_type") or "") != DESTINATION_GDRIVE:
            return False
        if str(task.get("destination_root") or ""):
            return True
        account = str(task.get("destination_account") or "")
        return bool(account) and not account.startswith(
            f"{DESTINATION_CLOUDDRIVE2}:"
        )

    def _task_profile(self, task: dict[str, Any]) -> DestinationProfile:
        # 没有绑定的任务创建于升级前，当时只有 CloudDrive2 目标。
        return destination_profile(
            task.get("destination_type") or DESTINATION_CLOUDDRIVE2
        )

    def _task_label(self, task: dict[str, Any], state: str) -> str:
        return state_label(
            state, task.get("destination_type") or DESTINATION_CLOUDDRIVE2
        )

    def _task_destination_ok(self, task: dict[str, Any]) -> bool:
        return destination_matches(task, self._binding())

    def _destination_block_reason(self, task: dict[str, Any]) -> str:
        stored = destination_profile(task.get("destination_type"))
        current = self._profile()
        if self._is_legacy_direct_gdrive(task):
            return (
                "该任务是旧版直连 Google Drive 创建的，绑定了 Google 文件夹或"
                "OAuth 授权指纹，无法改走 CloudDrive2；为避免把文件写到错误的"
                "位置已停止自动传输。请回到旧版本处理该任务，或确认远端文件后"
                "用 /cancel 关闭记录再重新转发。"
            )
        return (
            f"该任务绑定的是 {stored.label}，当前目标网盘是 {current.label}；"
            "为避免写入错误的网盘已暂停自动传输。"
            "请把目标切回原网盘后使用 /retry，或在原目标下 /cancel 后重新转发。"
        )

    def _block_task_for_destination(self, task_id: int, task: dict[str, Any]) -> None:
        reason = self._destination_block_reason(task)
        self.log.warning("任务 #%s 目标网盘不匹配，已暂停：%s", task_id, reason)
        self.db.update(task_id, wait_reason=reason)

    def _confirm_task(
        self, task_id: int
    ) -> tuple[str, dict[str, Any] | None]:
        confirm = getattr(self.db, "confirm_delivery", None)
        if confirm is None:
            confirm = self.db.confirm_115
        return confirm(task_id)

    @staticmethod
    def _media_key(message: Any) -> str | None:
        document = getattr(message, "document", None)
        document_id = getattr(document, "id", None)
        if document_id is not None:
            return f"document:{document_id}"
        photo = getattr(message, "photo", None)
        photo_id = getattr(photo, "id", None)
        if photo_id is not None:
            return f"photo:{photo_id}"
        return None

    async def _on_message(self, event: events.NewMessage.Event) -> None:
        if not self._authorized(event.sender_id):
            self.log.warning("忽略未授权用户：%s", event.sender_id)
            return

        text = (event.raw_text or "").strip()
        if text.startswith("/"):
            await self._handle_command(event, text)
            return

        message = event.message
        if not message.media or not message.file:
            await event.reply("这条消息没有可下载的文件。请转发视频或文档。")
            return
        file_size = int(message.file.size or 0)
        if file_size <= 0:
            await event.reply("无法读取文件大小，任务没有进入队列。")
            return
        fallback = f"telegram-{message.id}{message.file.ext or '.bin'}"
        file_name = safe_file_name(message.file.name or "", fallback)
        task, created = self.db.create_task(
            chat_id=int(event.chat_id),
            message_id=int(message.id),
            sender_id=int(event.sender_id),
            media_key=self._media_key(message),
            file_name=file_name,
            file_size=file_size,
            destination=self._binding(),
        )
        if not created:
            await event.reply(
                f"这个文件已经登记过。\n{self._format_task(task)}"
            )
            return
        # 不再回复“已接收/在排队”：真正开始传输时只发一条“开始传输”通知。
        self.log.info(
            "任务 #%s 已登记：%s（%s）", task["id"], file_name, format_bytes(file_size)
        )

    async def _handle_command(
        self, event: events.NewMessage.Event, text: str
    ) -> None:
        parts = text.split()
        command = parts[0].split("@", 1)[0].lower()
        if command in {"/start", "/help"}:
            profile = self._profile()
            await event.reply(
                "把视频或文件直接转发给我即可。\n\n"
                f"当前目标网盘：{profile.label}\n\n"
                "/queue - 查看最近任务\n"
                "/status - 查看系统状态\n"
                "/task <编号> - 查看单个任务\n"
                f"/confirm <编号> - {profile.confirm_command}\n"
                "/retry <编号> - 重试保留的失败任务\n"
                "/cancel <编号> - 取消任务"
            )
        elif command == "/queue":
            tasks = self.db.list_recent(15)
            if not tasks:
                await event.reply("当前没有任务。")
            else:
                await event.reply(
                    "最近任务：\n\n"
                    + "\n\n".join(self._format_task(task) for task in tasks)
                )
        elif command in {"/status", "/performance"}:
            await event.reply(self._format_status())
        elif command == "/task":
            await self._command_task(event, parts)
        elif command == "/confirm":
            await self._command_confirm(event, parts)
        elif command == "/retry":
            await self._command_retry(event, parts)
        elif command == "/cancel":
            await self._command_cancel(event, parts)
        else:
            await event.reply("未知命令。发送 /help 查看可用命令。")

    @staticmethod
    def _parse_task_id(parts: list[str]) -> int | None:
        if len(parts) != 2:
            return None
        try:
            return int(parts[1].lstrip("#"))
        except ValueError:
            return None

    async def _command_task(
        self, event: events.NewMessage.Event, parts: list[str]
    ) -> None:
        task_id = self._parse_task_id(parts)
        task = self.db.get(task_id) if task_id else None
        if task is None:
            await event.reply("用法：/task <任务编号>")
            return
        await event.reply(self._format_task(task, verbose=True))

    async def _command_confirm(
        self, event: events.NewMessage.Event, parts: list[str]
    ) -> None:
        task_id = self._parse_task_id(parts)
        if task_id is None:
            profile = self._profile()
            await event.reply(
                "用法：/confirm <任务编号>\n"
                f"{profile.confirm_usage}"
            )
            return
        result, task = self._confirm_task(task_id)
        if result == "missing" or task is None:
            await event.reply("没有找到这个任务。用法：/confirm <任务编号>")
            return
        profile = self._task_profile(task)
        if result == "already":
            await event.reply(f"任务 #{task_id} 已经由你确认过。")
            return
        if result == "invalid":
            await event.reply(
                f"任务当前状态为“{self._task_label(task, task['state'])}”，"
                "还不能确认。\n"
                f"必须先等 Bot 传输完成，并在 {profile.official}看到完整文件。"
            )
            return
        await event.reply(
            f"✅ 任务 #{task_id} 已标记为"
            f"“{self._task_label(task, 'confirmed')}”。\n"
            "这是你的人工确认记录；"
            f"Bot 没有调用 {profile.official}接口复验文件。"
        )

    async def _command_retry(
        self, event: events.NewMessage.Event, parts: list[str]
    ) -> None:
        task_id = self._parse_task_id(parts)
        task = self.db.get(task_id) if task_id else None
        if task is None:
            await event.reply("用法：/retry <任务编号>")
            return
        state = task["state"]
        if not self._task_destination_ok(task):
            await event.reply(
                f"任务 #{task_id} 不能重试。\n"
                f"{self._destination_block_reason(task)}"
            )
            return
        if state in {"upload_failed_retained", "verification_failed_retained"}:
            local_path = Path(task["local_path"] or "")
            if not local_path.is_file():
                self.db.update(
                    task_id,
                    state="queued",
                    local_path=None,
                    downloaded_bytes=0,
                    download_retries=0,
                    upload_retries=0,
                    error=None,
                    wait_reason="本地文件已不存在，用户要求重新下载",
                    next_retry_at=0,
                    started_at=None,
                )
                await event.reply(f"任务 #{task_id} 已重新进入下载队列。")
                return
            self.db.update(
                task_id,
                state="waiting_upload",
                upload_retries=0,
                error=None,
                wait_reason="用户要求重试",
                next_retry_at=0,
            )
            await event.reply(f"任务 #{task_id} 已重新进入上传队列。")
        elif state == "download_failed":
            self.db.update(
                task_id,
                state="queued",
                download_retries=0,
                error=None,
                wait_reason="用户要求重试",
                next_retry_at=0,
                started_at=None,
            )
            await event.reply(f"任务 #{task_id} 已重新进入下载队列。")
        else:
            await event.reply(
                f"任务当前状态为“{self._task_label(task, state)}”，不需要手动重试。"
            )

    async def _command_cancel(
        self, event: events.NewMessage.Event, parts: list[str]
    ) -> None:
        task_id = self._parse_task_id(parts)
        task = self.db.get(task_id) if task_id else None
        if task is None:
            await event.reply("用法：/cancel <任务编号>")
            return
        if task["state"] in {
            "completed",
            "confirmed",
            "cleanup_pending",
            "cancelled",
        }:
            await event.reply(
                f"任务已经是“{self._task_label(task, task['state'])}”状态。"
            )
            return
        running = self.download_tasks.get(task_id) or self.upload_tasks.get(task_id)
        if running:
            running.cancel()
            await asyncio.gather(running, return_exceptions=True)
        task = self.db.get(task_id)
        if task is None:
            await event.reply("任务记录已经不存在，取消中止。")
            return
        if task["state"] in {
            "completed",
            "confirmed",
            "cleanup_pending",
            "cancelled",
        }:
            await event.reply(
                f"任务已经是“{self._task_label(task, task['state'])}”状态。"
            )
            return
        profile = self._task_profile(task)
        remote_path = str(task.get("remote_path") or "")
        legacy_remote = False
        if remote_path and not self._task_destination_ok(task):
            if not self._is_legacy_direct_gdrive(task):
                await event.reply(
                    f"任务 #{task_id} 绑定的是 {profile.label}，当前目标网盘是 "
                    f"{self._profile().label}；为避免误删另一个网盘上的同名文件，"
                    "取消时不会清理远端文件，本次取消中止。\n"
                    "请把目标切回原网盘后再取消，或确认远端文件后手动处理。"
                )
                return
            # 旧直连版本的远端文件不在 CloudDrive2 上，这里只关闭本地记录，
            # 绝不通过 CD2 删除可能同名的其他文件。
            legacy_remote = True
        if remote_path and not legacy_remote:
            try:
                if await self.rclone.exists(remote_path):
                    await self.rclone.remove(remote_path)
                if await self.rclone.exists(remote_path):
                    raise RuntimeError("远端文件删除后仍然存在")
            except Exception as exc:  # noqa: BLE001 - fail closed on remote cleanup
                await event.reply(
                    f"{profile.storage} 远端文件暂时无法安全清理，取消中止；"
                    f"本地副本已保留：{exc}"
                )
                return
        local_path = Path(task["local_path"]) if task.get("local_path") else None
        if local_path and local_path.exists():
            try:
                local_path.unlink()
            except OSError as exc:
                await event.reply(f"无法安全删除本地文件，取消中止：{exc}")
                return
        part = self.settings.download_dir / f"{task_id}.part"
        if part.exists():
            try:
                part.unlink()
            except OSError as exc:
                await event.reply(f"无法安全删除下载临时文件，取消中止：{exc}")
                return
        self.db.update(
            task_id,
            state="cancelled",
            local_path=None,
            remote_path=None,
            downloaded_bytes=0,
            uploaded_bytes=0,
            error=None,
            wait_reason=None,
        )
        if legacy_remote:
            reply = (
                f"任务 #{task_id} 已取消，本地临时文件已清理。\n"
                "该任务是旧版直连 Google Drive 创建的，远端文件没有通过 "
                "CloudDrive2 删除；请在旧版本或 Google Drive 客户端自行处理。"
            )
        else:
            reply = f"任务 #{task_id} 已取消，本地临时文件和本任务远端文件已清理。"
        await event.reply(reply)

    def _format_task(self, task: dict[str, Any], verbose: bool = False) -> str:
        profile = self._task_profile(task)
        text = (
            f"#{task['id']}｜{task['file_name']}\n"
            f"大小：{format_bytes(task['file_size'])}\n"
            f"状态：{self._task_label(task, task['state'])}"
        )
        progress = self._progress_text(task)
        if progress:
            text += f"\n{progress}"
        reason = task.get("wait_reason")
        error = task.get("error")
        if reason:
            text += f"\n等待原因：{reason}"
        if error:
            text += f"\n错误：{str(error)[:500]}"
        if task.get("transfer_mode") == "stream":
            text += "\n模式：流式传输（不占用本地任务额度）"
        if verbose:
            text += (
                f"\n已下载：{format_bytes(task['downloaded_bytes'])}"
                f"\n下载重试：{task['download_retries']}"
                f"\n上传重试：{task['upload_retries']}"
                f"\n目标网盘：{profile.label}"
            )
            if task.get("remote_path"):
                text += f"\n{profile.storage} 路径：{task['remote_path']}"
            if self._is_legacy_direct_gdrive(task):
                text += (
                    "\n注意：该任务由旧版直连 Google Drive 创建，"
                    "无法通过 CloudDrive2 继续，需要回到旧版本处理。"
                )
            if task["state"] == "completed":
                text += (
                    f"\n{profile.official}核验：Bot 无法自动判断；"
                    + profile.confirm_hint.format(task_id=task["id"])
                )
            elif task["state"] == "confirmed":
                text += f"\n{profile.official}核验：已由你人工确认"
        return text

    def _format_status(self) -> str:
        profile = self._profile()
        binding = self._binding()
        counts = self.db.counts(destination=binding)
        counts_text = "、".join(
            f"{state_label(state, binding['destination_type'])} {count}"
            for state, count in sorted(counts.items())
        ) or "无"
        mismatch = self.db.pending_mismatch_count(binding)
        used = self.db.used_local_bytes()
        snapshot = self.snapshot
        resource_text = "资源采样尚未完成"
        if snapshot:
            resource_text = (
                f"CPU 平均：{snapshot.cpu_percent:.1f}%\n"
                f"可用内存：{format_bytes(snapshot.memory_available)}\n"
                f"磁盘可用：{format_bytes(snapshot.disk_free)}\n"
                f"网络总速率：{format_bytes(snapshot.network_bytes_per_second)}/s"
            )
        target = binding["destination_target"]
        target_text = f"（{target}）" if target else ""
        text = (
            "系统状态\n"
            f"目标网盘：{profile.label}{target_text}\n"
            f"{profile.health_name}："
            f"{'可写' if self.destination_healthy else '不可用或未配置完成'}\n"
            f"{profile.official}：Bot 不自动判断；"
            "看到完整文件后使用 /confirm <编号>\n"
            f"Bot 当前实际传输：下载/流式 {len(self.download_tasks)}，"
            f"落盘后上传 {len(self.upload_tasks)}\n"
            f"下载动态窗口：{self.download_window.value}\n"
            f"上传动态窗口：{self.upload_window.value}\n"
            f"传输方式：{'全部流式（不占本地额度）' if self._always_stream() else '自动（超过本地额度的文件流式）'}\n"
            f"本地额度：{format_bytes(used)} / {format_bytes(self.settings.local_budget_bytes)}\n"
            f"{resource_text}\n"
            f"任务统计（当前目标网盘）：{counts_text}"
        )
        active_text = self._active_tasks_text()
        if active_text:
            text += f"\n{active_text}"
        pause_text = self._window_pause_text()
        if pause_text:
            text += f"\n{pause_text}"
        if mismatch:
            text += (
                f"\n其他目标网盘的未完成任务：{mismatch} 个"
                "（不会自动传输，切回原目标后才会继续）"
            )
        return text

    def _always_stream(self) -> bool:
        return getattr(self.settings, "transfer_mode", "auto") == "stream"

    def _rates(self) -> dict[int, TransferRate]:
        rates = self.__dict__.get("_transfer_rates")
        if rates is None:
            rates = self.__dict__["_transfer_rates"] = {}
        return rates

    def _record_progress(self, task_id: int, done_bytes: int) -> None:
        """进度回调里调用：每秒最多更新一次平滑后的速度。"""
        rates = self._rates()
        now = time.monotonic()
        previous = rates.get(task_id)
        if previous is None or done_bytes < previous.done_bytes:
            rates[task_id] = TransferRate(int(done_bytes), now)
            return
        elapsed = now - previous.sampled_at
        if elapsed < 1:
            return
        instant = (done_bytes - previous.done_bytes) / elapsed
        speed = (
            instant
            if previous.bytes_per_second <= 0
            else previous.bytes_per_second * 0.7 + instant * 0.3
        )
        rates[task_id] = TransferRate(int(done_bytes), now, speed)

    def _progress_text(self, task: dict[str, Any]) -> str:
        """一行进度：百分比、字节、速度、剩余时间；不在传输中的任务返回空串。"""
        state = task.get("state")
        if state in STAGE_ONLY_STATES:
            return f"阶段：{self._task_label(task, state)}"
        if state not in PROGRESS_STATES:
            return ""
        total = int(task.get("file_size") or 0)
        done = min(int(task.get("downloaded_bytes") or 0), total) if total else 0
        percent = done / total * 100 if total else 0.0
        text = f"进度：{percent:.1f}%（{format_bytes(done)} / {format_bytes(total)}）"
        rate = self._rates().get(int(task["id"]))
        if (
            rate is not None
            and rate.bytes_per_second > 0
            and time.monotonic() - rate.sampled_at <= RATE_STALE_SECONDS
        ):
            text += f"，{format_bytes(rate.bytes_per_second)}/s"
            remaining = max(0, total - done) / rate.bytes_per_second
            text += f"，约剩 {format_duration(remaining)}"
        started_at = task.get("started_at")
        if started_at:
            text += f"，已用 {format_duration(time.time() - float(started_at))}"
        return text

    def _active_tasks_text(self) -> str:
        task_ids = sorted(set(self.download_tasks) | set(self.upload_tasks))
        lines = []
        for task_id in task_ids:
            task = self.db.get(task_id)
            if not task:
                continue
            progress = self._progress_text(task) or self._task_label(
                task, task["state"]
            )
            lines.append(f"#{task_id} {task['file_name']}：{progress}")
        return ("进行中：\n" + "\n".join(lines)) if lines else ""

    def _target_text(self, task: dict[str, Any], remote: str = "") -> str:
        target = str(task.get("destination_target") or "").strip("/")
        remote = str(remote or "").strip("/")
        if target and remote:
            return f"{target}/{remote}"
        return target or remote or "根目录"

    async def _announce_started(self, task_id: int, task: dict[str, Any]) -> None:
        """任务第一次真正开始传输时发一条通知；重试、重启恢复不重复发。"""
        if task.get("started_at"):
            return
        started_at = time.time()
        self.db.update(task_id, started_at=started_at)
        task["started_at"] = started_at
        profile = self._task_profile(task)
        await self._notify(
            f"⬇️ 开始传输 #{task_id}\n"
            f"文件：{task['file_name']}\n"
            f"大小：{format_bytes(task['file_size'])}\n"
            f"目标：{profile.label}：{self._target_text(task)}\n"
            f"进度可发送 /task {task_id} 查看"
        )

    async def _notify_failed(
        self, task_id: int, task: dict[str, Any], reason: str, hint: str
    ) -> None:
        """失败也是任务的最终结果，只发一次，不在中途重试时打扰。"""
        await self._notify(
            f"❌ 任务失败 #{task_id}\n"
            f"文件：{task.get('file_name') or '未知'}\n"
            f"原因：{reason}\n"
            f"{hint}"
        )

    def _window_pause_text(self) -> str:
        """窗口被压到 0 时说明原因，避免“一直排队却看不出为什么”。"""
        lines = []
        for label, window in (
            ("下载", self.download_window),
            ("上传", self.upload_window),
        ):
            reason = getattr(window, "hold_reason", None)
            if reason:
                lines.append(f"{label}暂停原因：{reason}")
        return "\n".join(lines)

    def _errors_in_last_minute(self, errors: deque[float]) -> int:
        cutoff = time.time() - 60
        while errors and errors[0] < cutoff:
            errors.popleft()
        return len(errors)

    async def _enforce_disk_emergency(self) -> None:
        if (
            not self.snapshot
            or self.snapshot.disk_free > self.settings.min_free_disk_bytes
            or not self.download_tasks
        ):
            return
        active = list(self.download_tasks.items())
        self.log.error(
            "磁盘已触及安全线，暂停 %s 个下载并清理不完整文件",
            len(active),
        )
        for _, task in active:
            task.cancel()
        await asyncio.gather(
            *(task for _, task in active),
            return_exceptions=True,
        )
        requeued = 0
        for task_id, _ in active:
            current = self.db.get(task_id)
            if current and current["state"] == "queued":
                self.db.update(
                    task_id,
                    downloaded_bytes=0,
                    wait_reason="磁盘触及安全线，已自动暂停并重新排队",
                )
                requeued += 1
        if requeued:
            # 唯一保留的中途提醒：磁盘满需要用户处理，否则任务会一直等。
            await self._notify(
                f"⚠️ 磁盘空间不足，{requeued} 个下载已暂停并重新排队，"
                "空间恢复后自动继续。"
            )

    async def _resource_loop(self) -> None:
        while not self._stop.is_set():
            try:
                self.snapshot = self.monitor.sample()
                await self._enforce_disk_emergency()
                now = time.time()
                if (
                    now - self.destination_last_checked
                    >= self.settings.remote_health_interval
                ):
                    self.destination_healthy = await self.rclone.healthy()
                    self.destination_last_checked = now
                counts = self.db.counts()
                download_demand = bool(
                    self.download_tasks
                    or counts.get("queued")
                    or counts.get("reserved")
                    or counts.get("downloading")
                    or counts.get("streaming")
                )
                upload_demand = bool(
                    self.upload_tasks
                    or counts.get("downloaded")
                    or counts.get("waiting_upload")
                    or counts.get("cleanup_pending")
                    or counts.get("uploading")
                    or counts.get("verifying")
                    or counts.get("finalizing")
                )
                self.download_window.update(
                    self.snapshot,
                    recent_errors=self._errors_in_last_minute(
                        self.recent_download_errors
                    ),
                    destination_healthy=self.destination_healthy,
                    demand_present=download_demand,
                )
                self.upload_window.update(
                    self.snapshot,
                    recent_errors=self._errors_in_last_minute(
                        self.recent_upload_errors
                    ),
                    destination_healthy=self.destination_healthy,
                    demand_present=upload_demand,
                )
                (self.settings.data_dir / "heartbeat").touch()
            except Exception:
                self.log.exception("资源控制循环异常")
            await asyncio.sleep(self.settings.control_interval)

    async def _scheduler_loop(self) -> None:
        while not self._stop.is_set():
            try:
                await self._start_downloads()
                await self._start_uploads()
            except Exception:
                self.log.exception("调度循环异常")
            await asyncio.sleep(1)

    def _queued_batches(self, states: tuple[str, ...]):
        destination = self._binding()
        after_id = 0
        while True:
            batch = self.db.list_states(
                states,
                limit=200,
                ready_only=True,
                after_id=after_id,
                destination=destination,
            )
            if not batch:
                return
            yield batch
            after_id = int(batch[-1]["id"])

    async def _start_downloads(self) -> None:
        if not self.snapshot or not self.destination_healthy:
            return
        available_slots = self.download_window.value - len(self.download_tasks)
        if available_slots <= 0:
            return
        for batch in self._queued_batches(("queued",)):
            for task in batch:
                if available_slots <= 0:
                    return
                task_id = int(task["id"])
                if task_id in self.download_tasks:
                    continue
                reserved, reason = self.db.reserve(
                    task_id,
                    budget_bytes=self.settings.local_budget_bytes,
                    current_free_bytes=self.snapshot.disk_free,
                    minimum_free_bytes=self.settings.min_free_disk_bytes,
                    always_stream=self._always_stream(),
                )
                if not reserved:
                    self.log.debug("任务 #%s 暂不放行：%s", task_id, reason)
                    continue
                async_task = asyncio.create_task(
                    self._download_one(task_id), name=f"download-{task_id}"
                )
                self.download_tasks[task_id] = async_task
                async_task.add_done_callback(
                    lambda _, tid=task_id: self.download_tasks.pop(tid, None)
                )
                available_slots -= 1

    async def _start_uploads(self) -> None:
        if not self.snapshot or not self.destination_healthy:
            return
        available_slots = self.upload_window.value - len(self.upload_tasks)
        if available_slots <= 0:
            return
        for batch in self._queued_batches(
            ("cleanup_pending", "waiting_upload", "downloaded")
        ):
            for task in batch:
                if available_slots <= 0:
                    return
                task_id = int(task["id"])
                if task_id in self.upload_tasks:
                    continue
                async_task = asyncio.create_task(
                    self._upload_one(task_id), name=f"upload-{task_id}"
                )
                self.upload_tasks[task_id] = async_task
                async_task.add_done_callback(
                    lambda _, tid=task_id: self.upload_tasks.pop(tid, None)
                )
                available_slots -= 1

    async def _download_one(self, task_id: int) -> None:
        task = self.db.get(task_id)
        if not task:
            return
        if not self._task_destination_ok(task):
            self._block_task_for_destination(task_id, task)
            return
        await self._announce_started(task_id, task)
        if task.get("transfer_mode") == "stream":
            await self._stream_one(task_id)
            return
        part_path = self.settings.download_dir / f"{task_id}.part"
        final_name = f"{task_id}-{safe_file_name(task['file_name'], str(task_id))}"
        final_path = self.settings.download_dir / final_name
        last_progress_at = 0.0

        def progress(received: int, total: int) -> None:
            nonlocal last_progress_at
            now = time.monotonic()
            if now - last_progress_at >= 1 or received >= total:
                self.db.update(task_id, downloaded_bytes=int(received))
                self._record_progress(task_id, int(received))
                last_progress_at = now

        try:
            for attempt in range(
                int(task["download_retries"]), self.settings.max_retries
            ):
                self.db.update(
                    task_id,
                    state="downloading",
                    downloaded_bytes=0,
                    download_retries=attempt,
                    local_path=str(part_path),
                    error=None,
                    wait_reason=None,
                )
                if part_path.exists():
                    part_path.unlink()
                try:
                    message = await self.client.get_messages(
                        int(task["chat_id"]), ids=int(task["message_id"])
                    )
                    if not message or not message.media:
                        raise RuntimeError("Telegram 消息或文件已经不可访问")
                    result = await self.client.download_media(
                        message, file=str(part_path), progress_callback=progress
                    )
                    if not result or not part_path.is_file():
                        raise RuntimeError("Telethon 没有生成下载文件")
                    actual_size = part_path.stat().st_size
                    if actual_size != int(task["file_size"]):
                        raise RuntimeError(
                            f"本地大小错误：{actual_size} != {task['file_size']}"
                        )
                    # Persist the final path before the atomic rename. A crash
                    # after the rename can then recover the completed file.
                    self.db.update(
                        task_id,
                        state="downloaded",
                        local_path=str(final_path),
                        downloaded_bytes=actual_size,
                    )
                    os.replace(part_path, final_path)
                    self.db.update(
                        task_id,
                        state="waiting_upload",
                        local_path=str(final_path),
                        downloaded_bytes=actual_size,
                        download_retries=attempt,
                        error=None,
                        wait_reason=None,
                        next_retry_at=0,
                    )
                    self.log.info("任务 #%s 下载完成，等待上传", task_id)
                    return
                except asyncio.CancelledError:
                    raise
                except Exception as exc:  # noqa: BLE001 - retry external I/O
                    self.recent_download_errors.append(time.time())
                    self.log.warning(
                        "任务 #%s 下载第 %s 次失败：%s", task_id, attempt + 1, exc
                    )
                    self.db.update(
                        task_id,
                        download_retries=attempt + 1,
                        error=str(exc)[:1000],
                    )
                    if attempt + 1 < self.settings.max_retries:
                        await asyncio.sleep(min(60, 2 ** (attempt + 1)))
            if part_path.exists():
                part_path.unlink()
            self.db.update(
                task_id,
                state="download_failed",
                local_path=None,
                error="下载重试次数已经用完",
                wait_reason=None,
            )
            await self._notify_failed(
                task_id,
                task,
                "从 Telegram 下载失败，重试次数已用完",
                f"不完整文件已清理，发送 /retry {task_id} 重新下载",
            )
        except asyncio.CancelledError:
            if part_path.exists():
                part_path.unlink()
            current = self.db.get(task_id)
            if current and current["state"] != "cancelled":
                self.db.update(task_id, state="queued", local_path=None)
            raise
        except Exception as exc:
            self.log.exception("任务 #%s 下载发生未处理异常", task_id)
            if part_path.exists():
                part_path.unlink()
            self.db.update(
                task_id,
                state="download_failed",
                local_path=None,
                error=str(exc)[:1000],
            )
            await self._notify_failed(
                task_id,
                task,
                f"下载出错：{str(exc)[:200]}",
                f"发送 /retry {task_id} 重新下载",
            )

    async def _finalize_stream_remote(
        self,
        task_id: int,
        task: dict[str, Any],
        remote_path: str,
        safe_name: str,
    ) -> None:
        final_remote = remote_path
        if remote_path.startswith(".uploading-"):
            async with self._finalize_lock:
                final_remote = await self._choose_remote_final(safe_name, task_id)
                self.db.update(
                    task_id,
                    state="finalizing",
                    remote_path=final_remote,
                )
                await self.rclone.move(remote_path, final_remote)
        final_size = await self.rclone.remote_size(final_remote)
        if final_size != int(task["file_size"]):
            await self.rclone.remove(final_remote)
            raise RuntimeError(
                f"流式改名后远端大小错误：{final_size} != {task['file_size']}"
            )
        await self._complete_cloud_receive(
            task_id, task, None, final_remote
        )

    async def _stream_one(self, task_id: int) -> None:
        task = self.db.get(task_id)
        if not task:
            return
        safe_name = safe_file_name(task["file_name"], f"task-{task_id}.bin")
        remote_temp = f".uploading-{task_id}-{safe_name}"
        stream = None
        try:
            for attempt in range(
                int(task["download_retries"]), self.settings.max_retries
            ):
                try:
                    current = self.db.get(task_id) or task
                    recorded_remote = str(current.get("remote_path") or "")
                    if recorded_remote and await self.rclone.exists(recorded_remote):
                        recorded_size = await self.rclone.remote_size(recorded_remote)
                        if recorded_size == int(task["file_size"]):
                            await self._finalize_stream_remote(
                                task_id,
                                task,
                                recorded_remote,
                                safe_name,
                            )
                            return
                        if recorded_remote.startswith(".uploading-"):
                            await self.rclone.remove(recorded_remote)
                    self.db.update(
                        task_id,
                        state="streaming",
                        transfer_mode="stream",
                        local_path=None,
                        remote_path=remote_temp,
                        downloaded_bytes=0,
                        uploaded_bytes=0,
                        download_retries=attempt,
                        error=None,
                        wait_reason=None,
                    )
                    message = await self.client.get_messages(
                        int(task["chat_id"]), ids=int(task["message_id"])
                    )
                    if not message or not message.media:
                        raise RuntimeError("Telegram 消息或文件已经不可访问")
                    stream = await self.rclone.open_upload_stream(
                        remote_temp,
                        int(task["file_size"]),
                        **_task_log_kwargs(
                            self.rclone.open_upload_stream, task_id
                        ),
                    )
                    last_progress_at = 0.0

                    def progress(received: int, total: int) -> None:
                        nonlocal last_progress_at
                        now = time.monotonic()
                        if now - last_progress_at >= 1 or received >= total:
                            self.db.update(
                                task_id,
                                downloaded_bytes=int(received),
                                uploaded_bytes=int(received),
                            )
                            self._record_progress(task_id, int(received))
                            last_progress_at = now

                    await self.client.download_media(
                        message,
                        file=stream,
                        progress_callback=progress,
                    )
                    streamed_size = stream.tell()
                    if streamed_size != int(task["file_size"]):
                        raise RuntimeError(
                            f"流式读取大小错误：{streamed_size} "
                            f"!= {task['file_size']}"
                        )
                    await stream.finish()
                    self.db.update(task_id, state="verifying")
                    remote_size = await self.rclone.remote_size(remote_temp)
                    if remote_size != int(task["file_size"]):
                        raise RuntimeError(
                            f"流式远端大小错误：{remote_size} "
                            f"!= {task['file_size']}"
                        )
                    await self._finalize_stream_remote(
                        task_id, task, remote_temp, safe_name
                    )
                    return
                except asyncio.CancelledError:
                    raise
                except Exception as exc:  # noqa: BLE001 - retry external I/O
                    if stream is not None:
                        await stream.abort()
                        stream = None
                    await self.rclone.remove(remote_temp)
                    self.recent_download_errors.append(time.time())
                    self.log.warning(
                        "任务 #%s 流式传输第 %s 次失败：%s",
                        task_id,
                        attempt + 1,
                        exc,
                    )
                    self.db.update(
                        task_id,
                        download_retries=attempt + 1,
                        error=str(exc)[:1000],
                    )
                    if attempt + 1 < self.settings.max_retries:
                        await asyncio.sleep(min(120, 3 ** (attempt + 1)))
            current = self.db.get(task_id) or task
            retained_remote = str(current.get("remote_path") or "")
            if retained_remote.startswith(".uploading-"):
                retained_remote = ""
            self.db.update(
                task_id,
                state="download_failed",
                local_path=None,
                remote_path=retained_remote or None,
                error="流式传输重试次数已经用完；使用 /retry 可重新开始",
                wait_reason=None,
            )
            await self._notify_failed(
                task_id,
                task,
                "流式传输失败，重试次数已用完",
                f"发送 /retry {task_id} 重新开始",
            )
        except asyncio.CancelledError:
            if stream is not None:
                await stream.abort()
            current = self.db.get(task_id)
            recorded_remote = (
                str(current.get("remote_path") or "") if current else ""
            )
            if recorded_remote.startswith(".uploading-"):
                await self.rclone.remove(recorded_remote)
                recorded_remote = ""
            if current and current["state"] != "cancelled":
                self.db.update(
                    task_id,
                    state="queued",
                    local_path=None,
                    remote_path=recorded_remote or None,
                    downloaded_bytes=0,
                    uploaded_bytes=0,
                    wait_reason="流式任务已暂停并自动重新排队",
                )
            raise
        except Exception as exc:
            self.log.exception("任务 #%s 流式传输发生未处理异常", task_id)
            self.db.update(
                task_id,
                state="download_failed",
                local_path=None,
                error=str(exc)[:1000],
            )
            await self._notify_failed(
                task_id,
                task,
                f"流式传输出错：{str(exc)[:200]}",
                f"发送 /retry {task_id} 重新开始",
            )

    async def _choose_remote_final(self, file_name: str, task_id: int) -> str:
        if not await self.rclone.exists(file_name):
            return file_name
        path = Path(file_name)
        candidate = f"{path.stem} (task-{task_id}){path.suffix}"
        suffix = 2
        while await self.rclone.exists(candidate):
            candidate = f"{path.stem} (task-{task_id}-{suffix}){path.suffix}"
            suffix += 1
        return candidate

    def _transfer_log_dir(self) -> Path | None:
        log_dir = getattr(self.settings, "log_dir", None)
        return Path(log_dir) if log_dir is not None else None

    def _remove_transfer_log(self, task_id: int) -> None:
        """Drop this task's rclone log; a failure is only a warning.

        The remote side already holds the file, so a stuck log file must never
        turn a successful transfer into a failed one.
        """
        log_dir = self._transfer_log_dir()
        if log_dir is None:
            return
        if not remove_task_log(log_dir, task_id):
            self.log.warning("任务 #%s 已完成，但传输日志无法删除", task_id)

    def _task_completed(self, task_id: int) -> bool:
        task = self.db.get(task_id)
        return bool(task and task.get("state") == "completed")

    def _sweep_transfer_logs(self) -> None:
        """Startup cleanup of leftover ``logs/transfers`` files.

        Completed tasks never keep a per-task log, so any file still named after
        a completed task is a crash leftover. Unfinished tasks keep theirs, and
        the directory is trimmed to 8 MiB / 10 files either way.
        """
        log_dir = self._transfer_log_dir()
        if log_dir is None:
            return
        try:
            swept = sweep_transfers(log_dir, self._task_completed)
        except Exception as exc:  # noqa: BLE001 - startup must not fail here
            self.log.warning("清理传输日志失败：%s", exc)
            return
        if swept["completed"] or swept["pruned"]:
            self.log.info(
                "传输日志清理：已完成任务残留 %s 份，超限删除 %s 份",
                swept["completed"],
                swept["pruned"],
            )

    async def _complete_cloud_receive(
        self,
        task_id: int,
        task: dict[str, Any],
        local_path: Path | None,
        final_remote: str,
    ) -> None:
        # Persist the verified remote result before deleting the local copy.
        # A restart between these operations can then resume cleanup without
        # re-uploading or creating a duplicate remote file.
        self.db.update(
            task_id,
            state="cleanup_pending",
            remote_path=final_remote,
            uploaded_bytes=int(task["file_size"]),
            error=None,
            wait_reason="远端文件已校验，正在清理 VPS 本地副本",
            next_retry_at=0,
        )
        if local_path is not None:
            try:
                local_path.unlink(missing_ok=True)
            except OSError as exc:
                self.db.update(
                    task_id,
                    error=f"远端已接收，但 VPS 本地文件暂时无法清理：{exc}"[:1000],
                    wait_reason="远端已接收；等待自动重试清理 VPS 本地副本",
                    next_retry_at=time.time() + 60,
                )
                self.log.warning("任务 #%s 的本地副本清理失败：%s", task_id, exc)
                return
        self.db.update(
            task_id,
            state="completed",
            local_path=None,
            error=None,
            wait_reason=None,
            next_retry_at=0,
        )
        # The transfer is done and the local copy is gone: the per-task rclone
        # log has no further use, so it is deleted right away. The task row stays
        # for de-duplication and /confirm.
        self._remove_transfer_log(task_id)
        profile = self._task_profile(task)
        self._rates().pop(task_id, None)
        started_at = float(task.get("started_at") or task.get("created_at") or 0)
        elapsed = format_duration(time.time() - started_at) if started_at else "未知"
        await self._notify(
            f"✅ Bot 传输已完成 #{task_id}（下载完成，{profile.storage} 已接收）\n"
            f"文件：{task['file_name']}\n"
            f"大小：{format_bytes(task['file_size'])}\n"
            f"路径：{profile.label}：{self._target_text(task, final_remote)}\n"
            f"用时：{elapsed}\n"
            f"到 {profile.official}看到完整文件后可发送 /confirm #{task_id}"
        )

    async def _upload_one(self, task_id: int) -> None:
        task = self.db.get(task_id)
        if not task:
            return
        if not self._task_destination_ok(task):
            self._block_task_for_destination(task_id, task)
            return
        local_path = Path(task["local_path"]) if task.get("local_path") else None
        if task.get("state") == "cleanup_pending":
            recorded_remote = str(task.get("remote_path") or "")
            try:
                if (
                    recorded_remote
                    and await self.rclone.exists(recorded_remote)
                    and await self.rclone.remote_size(recorded_remote)
                    == int(task["file_size"])
                ):
                    await self._complete_cloud_receive(
                        task_id, task, local_path, recorded_remote
                    )
                    return
            except Exception as exc:  # noqa: BLE001 - retry external I/O
                self.db.update(
                    task_id,
                    error=f"检查已接收的远端文件失败：{exc}"[:1000],
                    wait_reason=f"等待 {self._task_profile(task).storage} 恢复后继续本地清理",
                    next_retry_at=time.time() + 60,
                )
                return
            if local_path is None or not local_path.is_file():
                if task.get("transfer_mode") == "stream":
                    self.db.update(
                        task_id,
                        state="queued",
                        local_path=None,
                        downloaded_bytes=0,
                        uploaded_bytes=0,
                        wait_reason=(
                            "流式任务的远端文件无法确认，已自动重新排队"
                        ),
                        next_retry_at=0,
                    )
                    return
                self.db.update(
                    task_id,
                    state="verification_failed_retained",
                    error="远端正式文件和 VPS 本地副本都无法确认，已停止自动操作",
                    wait_reason=None,
                )
                await self._notify_failed(
                    task_id,
                    task,
                    "远端文件和 VPS 本地副本都无法确认，已停止自动处理",
                    f"发送 /task {task_id} 查看详情",
                )
                return
            self.db.update(
                task_id,
                state="waiting_upload",
                remote_path=None,
                error="远端正式文件不存在，保留本地副本并重新上传",
                wait_reason=None,
                next_retry_at=0,
            )
            task = self.db.get(task_id) or task
        if local_path is None or not local_path.is_file():
            self.db.update(
                task_id,
                state="queued",
                local_path=None,
                downloaded_bytes=0,
                download_retries=0,
                upload_retries=0,
                error="数据库记录的本地完整文件不存在",
                wait_reason="本地文件不存在，已自动重新排队下载",
                next_retry_at=0,
            )
            return
        actual_local_size = local_path.stat().st_size
        if actual_local_size != int(task["file_size"]):
            error = (
                f"本地文件大小错误：{actual_local_size} != {task['file_size']}"
            )
            try:
                local_path.unlink()
            except OSError as exc:
                self.db.update(
                    task_id,
                    state="verification_failed_retained",
                    error=f"{error}；且无法清理：{exc}"[:1000],
                )
                await self._notify_failed(
                    task_id,
                    task,
                    "本地文件不完整且无法清理",
                    f"发送 /task {task_id} 查看详情",
                )
                return
            self.db.update(
                task_id,
                state="queued",
                local_path=None,
                downloaded_bytes=0,
                download_retries=0,
                upload_retries=0,
                error=error,
                wait_reason="本地文件不完整，已自动重新排队下载",
                next_retry_at=0,
            )
            self.log.warning("任务 #%s 本地文件校验失败，已自动重新下载", task_id)
            return
        safe_name = safe_file_name(task["file_name"], f"task-{task_id}.bin")
        remote_temp = f".uploading-{task_id}-{safe_name}"
        try:
            for attempt in range(
                int(task["upload_retries"]), self.settings.max_retries
            ):
                self.db.update(
                    task_id,
                    upload_retries=attempt,
                    error=None,
                    wait_reason=None,
                )
                try:
                    current = self.db.get(task_id) or task
                    recorded_remote = str(current.get("remote_path") or "")
                    if (
                        recorded_remote
                        and not recorded_remote.startswith(".uploading-")
                        and await self.rclone.exists(recorded_remote)
                    ):
                        recorded_size = await self.rclone.remote_size(recorded_remote)
                        if recorded_size == int(task["file_size"]):
                            await self._complete_cloud_receive(
                                task_id, task, local_path, recorded_remote
                            )
                            return
                    self.db.update(
                        task_id,
                        state="uploading",
                        remote_path=remote_temp,
                    )
                    await self.rclone.upload(
                        local_path,
                        remote_temp,
                        **_task_log_kwargs(self.rclone.upload, task_id),
                    )
                    self.db.update(task_id, state="verifying")
                    remote_size = await self.rclone.remote_size(remote_temp)
                    if remote_size != int(task["file_size"]):
                        raise RuntimeError(
                            f"远端大小错误：{remote_size} != {task['file_size']}"
                        )
                    async with self._finalize_lock:
                        final_remote = await self._choose_remote_final(
                            safe_name, task_id
                        )
                        self.db.update(
                            task_id,
                            state="finalizing",
                            remote_path=final_remote,
                        )
                        await self.rclone.move(remote_temp, final_remote)
                    final_size = await self.rclone.remote_size(final_remote)
                    if final_size != int(task["file_size"]):
                        await self.rclone.remove(final_remote)
                        raise RuntimeError(
                            f"改名后远端大小错误：{final_size} != {task['file_size']}"
                        )
                    await self._complete_cloud_receive(
                        task_id, task, local_path, final_remote
                    )
                    return
                except asyncio.CancelledError:
                    raise
                except Exception as exc:  # noqa: BLE001 - retry external I/O
                    self.recent_upload_errors.append(time.time())
                    self.log.warning(
                        "任务 #%s 上传第 %s 次失败：%s", task_id, attempt + 1, exc
                    )
                    self.db.update(
                        task_id,
                        upload_retries=attempt + 1,
                        error=str(exc)[:1000],
                    )
                    if attempt + 1 < self.settings.max_retries:
                        await asyncio.sleep(min(120, 3 ** (attempt + 1)))
            await self.rclone.remove(remote_temp)
            self.db.update(
                task_id,
                state="upload_failed_retained",
                error="上传重试次数已经用完；本地完整文件已保留",
            )
            await self._notify_failed(
                task_id,
                task,
                "上传失败（重试次数已用完），本地完整文件已保留",
                f"修复 {self._task_profile(task).storage} 后发送 /retry {task_id}",
            )
        except asyncio.CancelledError:
            await self.rclone.remove(remote_temp)
            current = self.db.get(task_id)
            if current and current["state"] != "cancelled":
                self.db.update(task_id, state="waiting_upload")
            raise
        except Exception as exc:
            self.log.exception("任务 #%s 上传发生未处理异常", task_id)
            self.db.update(
                task_id,
                state="upload_failed_retained",
                error=str(exc)[:1000],
            )
            await self._notify_failed(
                task_id,
                task,
                f"上传出错：{str(exc)[:200]}，本地文件已保留",
                f"修复后发送 /retry {task_id}",
            )

    async def _notify(self, message: str) -> None:
        try:
            await self.client.send_message(self.settings.allowed_user_id, message)
        except Exception:
            self.log.exception("发送 Bot 通知失败")

    async def start(self) -> None:
        recovered = self.db.recover(self.settings.download_dir)
        self.log.info("重启恢复结果：%s", recovered)
        self._sweep_transfer_logs()
        binding = self._binding()
        profile = self._profile()
        adopted = 0
        if binding["destination_type"] == DESTINATION_CLOUDDRIVE2:
            # 升级前的部署只有 CloudDrive2 目标，历史任务在这里补上绑定，
            # 保证合法重启后仍能继续原来的队列。
            adopted = self.db.bind_unbound_tasks(binding)
            if adopted:
                self.log.info("已为 %s 个历史任务补写 CloudDrive2 目标绑定", adopted)
            # 正式旧版只有 CloudDrive2；未知账号的 Google 任务不可自动认领，
            # 因为目录相同不足以证明它们属于当前 Google 授权。
            stamped = self.db.adopt_legacy_account(binding)
            if stamped:
                self.log.info("已为 %s 个历史任务补写目标账号指纹", stamped)
        mismatch = self.db.pending_mismatch_count(binding)
        await self.client.start(bot_token=self.settings.bot_token)
        me = await self.client.get_me()
        self.log.info("Telegram Bot 已登录：@%s", me.username)
        self._background = [
            asyncio.create_task(self._resource_loop(), name="resource-loop"),
            asyncio.create_task(self._scheduler_loop(), name="scheduler-loop"),
        ]
        (self.settings.data_dir / "heartbeat").touch()
        # 启动不再发 Telegram 通知；/status 会显示其他目标网盘的未完成任务。
        self.log.info("Telegram → %s 服务已启动", profile.label)
        if mismatch:
            self.log.warning(
                "有 %s 个未完成任务属于其他目标网盘，不会自动传输；"
                "切回原目标后会自动继续",
                mismatch,
            )

    async def stop(self) -> None:
        self._stop.set()
        all_tasks = (
            self._background
            + list(self.download_tasks.values())
            + list(self.upload_tasks.values())
        )
        for task in all_tasks:
            task.cancel()
        if all_tasks:
            await asyncio.gather(*all_tasks, return_exceptions=True)
        await self.client.disconnect()
        self.db.close()

    async def run(self) -> None:
        await self.start()
        await self._stop.wait()


async def async_main() -> None:
    settings = Settings.from_env()
    service = TransferService(settings)
    loop = asyncio.get_running_loop()
    for signame in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(signame, service._stop.set)
        except NotImplementedError:
            pass
    try:
        await service.run()
    finally:
        await service.stop()


if __name__ == "__main__":
    asyncio.run(async_main())
