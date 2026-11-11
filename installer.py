from __future__ import annotations

import base64
import codecs
import hashlib
import ipaddress
import math
import os
import queue
import re
import select
import shlex
import socketserver
import sys
import tarfile
import tempfile
import threading
import time
import tkinter as tk
import uuid
import webbrowser
from collections.abc import Callable, Mapping
from pathlib import Path
from tkinter import filedialog, messagebox, scrolledtext, ttk
from urllib.parse import urlsplit

import paramiko

APP_TITLE = "Telegram → 115 / Google Drive 一键部署器（CloudDrive2 共同传输层）"
APP_VERSION = "1.5.0"
LOCAL_BUILD_LABEL = "本地开发版"
MANAGED_CD2_WEBDAV_URL = "http://clouddrive2:19798/dav"

# VPS 预设：默认最小配置，宽裕配置保留给内存/硬盘更大的机器。
# 预算与磁盘保留只是界面默认值，部署时以输入框里的数字为准；
# 内存阈值跟随预设，不单独提供输入框。
VPS_PROFILE_MINIMUM = "minimum"
VPS_PROFILE_STANDARD = "standard"
VPS_PROFILE_ORDER = (VPS_PROFILE_MINIMUM, VPS_PROFILE_STANDARD)
VPS_PROFILE_LABELS = {
    VPS_PROFILE_MINIMUM: "最小配置（推荐）",
    VPS_PROFILE_STANDARD: "宽裕配置",
}
VPS_PROFILE_SPECS = {
    VPS_PROFILE_MINIMUM: "1 核 / 1GB 内存 / 20GB 硬盘",
    VPS_PROFILE_STANDARD: "建议内存至少 2GB、硬盘约 50GB",
}
VPS_PROFILE_NOTES = {
    VPS_PROFILE_MINIMUM: (
        "只写入推荐的 2GB 本地预算和 6GB 磁盘保留，不会按机器内存做配置健康检测。"
        "超过预算的单文件继续自动流式传输，CloudDrive2 自己的缓存不计入 2GB。"
    ),
    VPS_PROFILE_STANDARD: (
        "只写入推荐的 20GB 本地预算和 20GB 磁盘保留，适合硬盘更大的机器。"
    ),
}
VPS_PROFILE_PRESETS = {
    VPS_PROFILE_MINIMUM: {
        "local_budget_gb": "2",
        "min_free_disk_gb": "6",
    },
    VPS_PROFILE_STANDARD: {
        "local_budget_gb": "20",
        "min_free_disk_gb": "20",
    },
}
# 运行期内存保护阈值（软/硬，MB）跟随 VPS 预设：
# 1GB 级小机器 Docker + CloudDrive2 常驻后可用内存只有约 300MB，沿用 1024/512
# 会让下载/上传窗口永远为 0，任务只会一直排队。最小配置因此用 256/128。
VPS_PROFILE_MEMORY_LIMITS = {
    VPS_PROFILE_MINIMUM: ("256", "128"),
    VPS_PROFILE_STANDARD: ("1024", "512"),
}
# 没有 vps_profile 字段的旧调用（旧配置、旧测试、旧界面快照）沿用这组阈值。
LEGACY_MEMORY_LIMITS = ("1024", "512")

# 传输方式（写入 .env 的 TRANSFER_MODE）：auto 保持原行为，stream 让所有文件流式传输。
TRANSFER_MODE_AUTO = "auto"
TRANSFER_MODE_STREAM = "stream"
TRANSFER_MODE_LABELS = {
    TRANSFER_MODE_AUTO: "自动：小文件先落盘，大文件流式",
    TRANSFER_MODE_STREAM: "全部流式：不占 VPS 本地磁盘，失败需整文件重传",
}


def transfer_mode_value(values: Mapping[str, str]) -> str:
    """解析传输方式；缺失或留空沿用 auto。"""
    raw = (values.get("transfer_mode") or "").strip().lower() or TRANSFER_MODE_AUTO
    if raw not in TRANSFER_MODE_LABELS:
        raise ValueError("传输方式只能是 auto 或 stream，当前值：" + raw)
    return raw

DESTINATION_CLOUDDRIVE2 = "clouddrive2"
DESTINATION_GDRIVE = "gdrive"
# 界面上只出现「115」和「Google Drive」；CloudDrive2 是两者共同的传输层。
DESTINATION_LABELS = {
    DESTINATION_CLOUDDRIVE2: "115",
    DESTINATION_GDRIVE: "Google Drive",
}
DESTINATION_ALIASES = {
    "115": DESTINATION_CLOUDDRIVE2,
    "google_drive": DESTINATION_GDRIVE,
}
# 两个分支都通过 CloudDrive2 的 WebDAV 写入，因此 remote 都是 cd2:。
DESTINATION_REMOTES = {
    DESTINATION_CLOUDDRIVE2: "cd2:",
    DESTINATION_GDRIVE: "cd2:",
}
DESTINATION_DESCRIPTIONS = {
    DESTINATION_CLOUDDRIVE2: (
        "Telegram → VPS → CloudDrive2 WebDAV → 115。"
        "请先在 CloudDrive2 管理页登录并挂载 115，再开启 WebDAV；"
        "115 的登录与挂载由你本人在 CloudDrive2 中完成。"
    ),
    DESTINATION_GDRIVE: (
        "Telegram → VPS → CloudDrive2 WebDAV → Google Drive。"
        "请先在 CloudDrive2 管理页登录并挂载 Google Drive，再开启 WebDAV；"
        "部署器不导入 Google 授权、不直接调用 Google API。"
    ),
}
# “查看 VPS 状态”是只读检查：只读容器 State 字段与末尾日志，
# 不写入、不启动/重启服务，也不代表 WebDAV 或最终网盘已入库。
STATUS_RUNNING = "running"
STATUS_STOPPED = "stopped"
STATUS_MISSING = "missing"
STATUS_NOT_INSTALLED = "not_installed"
STATUS_UNKNOWN = "unknown"

# 只在日志与弹窗里做兜底遮蔽；正常流程不会打印这些字段。
SECRET_FIELDS = (
    "vps_password",
    "sudo_password",
    "ssh_key_passphrase",
    "bot_token",
    "telegram_api_hash",
    "cd2_password",
)

def resource_path(name: str) -> Path:
    base = Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parent))
    return base / name


# 本地运行测试或 PyInstaller 打包时会在 payload 里留下字节码缓存；它们与 VPS 上的
# Python 版本无关，上传只会增大部署包，还可能进入镜像构建上下文。
PAYLOAD_EXCLUDED_DIRS = frozenset({"__pycache__"})
PAYLOAD_EXCLUDED_SUFFIXES = (".pyc", ".pyo")


def payload_tar_filter(info: tarfile.TarInfo) -> tarfile.TarInfo | None:
    """tarfile filter: drop bytecode caches from the uploaded payload."""
    parts = info.name.replace("\\", "/").split("/")
    if PAYLOAD_EXCLUDED_DIRS.intersection(parts):
        return None
    if info.name.endswith(PAYLOAD_EXCLUDED_SUFFIXES):
        return None
    return info


def build_payload_archive(payload: Path, archive: Path) -> None:
    """Pack ``payload`` as ``payload/...`` into a gzip tarball for upload."""
    with tarfile.open(archive, "w:gz") as tar:
        tar.add(payload, arcname="payload", filter=payload_tar_filter)


def b64(value: str) -> str:
    return base64.b64encode(value.encode("utf-8")).decode("ascii")


def destination_type(values: Mapping[str, str]) -> str:
    """解析目标网盘；缺失时默认 clouddrive2，接受 115/google_drive 别名。"""
    raw = (values.get("destination_type") or "").strip().lower()
    if not raw:
        return DESTINATION_CLOUDDRIVE2
    raw = DESTINATION_ALIASES.get(raw, raw)
    if raw not in DESTINATION_LABELS:
        raise ValueError("目标网盘只能是 clouddrive2 或 gdrive，当前值：" + raw)
    return raw


def effective_webdav_url(values: Mapping[str, str]) -> str:
    if (values.get("deploy_clouddrive2") or "true") == "true":
        return MANAGED_CD2_WEBDAV_URL
    return (values.get("cd2_url") or "").strip()


def destination_target(values: Mapping[str, str]) -> str:
    """本次选中网盘在 CD2 WebDAV 根目录下的相对路径（已去掉首尾斜杠）。"""
    name = (
        "gdrive_target"
        if destination_type(values) == DESTINATION_GDRIVE
        else "cd2_target"
    )
    return (values.get(name) or "").strip().strip("/")


# 默认保存到网盘里的「telegram转存」文件夹，不直接存到网盘根目录。
# CloudDrive2 WebDAV 根目录下每个网盘是一个挂载文件夹（例如 115、GoogleDrive）。
DEFAULT_SAVE_FOLDER = "telegram转存"
DEFAULT_DRIVE_MOUNTS = {
    DESTINATION_CLOUDDRIVE2: "115",
    DESTINATION_GDRIVE: "GoogleDrive",
}
DEFAULT_TARGETS = {
    destination: f"{mount}/{DEFAULT_SAVE_FOLDER}"
    for destination, mount in DEFAULT_DRIVE_MOUNTS.items()
}
# 只填了这些网盘挂载名之一（= 网盘根目录）时，自动在后面接上 telegram转存。
DRIVE_ROOT_NAMES = frozenset(
    {"115", "115open", "115网盘", "googledrive", "google云端硬盘", "谷歌网盘", "gdrive"}
)


def normalize_drive_folder(destination: str, raw: str) -> str:
    """部署器里的网盘文件夹：留空或只填网盘根目录时改用 <网盘>/telegram转存。

    明确填写的子路径（例如 115/Telegram）原样保留；只做去掉首尾空白和斜杠。
    """
    value = (raw or "").strip().strip("/")
    if not value:
        return DEFAULT_TARGETS[destination]
    parts = [part for part in value.split("/") if part]
    if len(parts) == 1 and parts[0].casefold().replace(" ", "") in DRIVE_ROOT_NAMES:
        return f"{parts[0]}/{DEFAULT_SAVE_FOLDER}"
    return value


def deploy_success_message(destination: str) -> str:
    label = DESTINATION_LABELS.get(destination, DESTINATION_LABELS[DESTINATION_CLOUDDRIVE2])
    return (
        f"程序已经安装，Bot 容器已在 VPS 上启动，目标为 {label}。\n\n"
        "安装已完成；账号登录、网盘挂载和实际写入是否正常，尚未检查。\n\n"
        "接下来按顺序做两件事：\n"
        f"1. 点击“登录并挂载网盘”，确认 CloudDrive2 已登录并挂载 {label}；"
        "如果之前已经挂载，无需重复添加。\n"
        "2. 回到部署器点击“检查是否可用”，自动写入小文件、检查大小并清理。\n\n"
        "检查通过后，再给 Bot 发一个小文件试用。"
    )


def verify_success_message(destination: str) -> str:
    label = DESTINATION_LABELS.get(destination, DESTINATION_LABELS[DESTINATION_CLOUDDRIVE2])
    return (
        f"CloudDrive2 WebDAV 已为 {label} 通过真实测试文件的"
        "写入、大小校验、改名和清理。\n\n"
        "注意：这只证明 CloudDrive2 已接收并通过 WebDAV 校验；"
        f"不代表 {label} 官方端已经确认，请在官方客户端中确认文件可见、"
        "大小正常且可以打开。\n\n"
        "接下来：在 Telegram 里给 Bot 发一个文件，再到官方客户端确认它出现。"
    )


# ---------------------------------------------------------------------------
# 失败诊断（纯函数：不碰 Tk、不联网，便于无 GUI 单测）
#
# 网络修复和网盘写入验收是两件事：网络修复只证明 Bot 能连到 CloudDrive2，
# 认证失败、路径不存在、网盘未挂载都必须由验收阶段单独报出来，不能笼统说成
# “网络修复失败”。这里把远程输出归类成用户能看懂的一条结论 + 一个具体下一步，
# 并保留有界的原始错误尾部，方便用户或技术支持定位。
# ---------------------------------------------------------------------------

FAILURE_TAIL_LINES = 12
FAILURE_TAIL_WIDTH = 180

FAILURE_AUTH = "auth"
FAILURE_PATH = "path"
FAILURE_NETWORK = "network"
FAILURE_PROTOCOL = "protocol"
FAILURE_CONTAINER = "container"
FAILURE_TIMEOUT = "timeout"
FAILURE_UNKNOWN = "unknown"

# 顺序即优先级：先排除容器/认证这类更具体的原因，再落到网络与未知。
FAILURE_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    (
        FAILURE_CONTAINER,
        re.compile(
            r"cannot connect to the docker daemon"
            r"|docker: (command )?not found"
            r"|docker 不可用"
            r"|no such container"
            r"|is not running"
            r"|容器没有运行"
            r"|找不到现有 tg115"
            r"|多个正在运行的 clouddrive2"
            r"|无法检查 clouddrive2 容器"
            r"|/dev/fuse",
            re.IGNORECASE,
        ),
    ),
    (
        FAILURE_AUTH,
        re.compile(
            r"\b40[13]\b"
            r"|unauthorized"
            r"|forbidden"
            r"|invalid credentials"
            r"|authentication failed"
            r"|认证失败"
            r"|凭据无效",
            re.IGNORECASE,
        ),
    ),
    (
        FAILURE_PROTOCOL,
        re.compile(
            r"server gave http response to https client"
            r"|wrong version number"
            r"|ssl: "
            r"|tls handshake"
            r"|协议不匹配",
            re.IGNORECASE,
        ),
    ),
    (
        FAILURE_TIMEOUT,
        re.compile(r"timed out|timeout|超时", re.IGNORECASE),
    ),
    (
        FAILURE_PATH,
        re.compile(
            r"\b404\b"
            r"|no such file"
            r"|does not exist"
            r"|directory not found"
            r"|path not found"
            r"|目标路径不存在",
            re.IGNORECASE,
        ),
    ),
    (
        FAILURE_NETWORK,
        re.compile(
            r"lookup clouddrive2"
            r"|name or service not known"
            r"|temporary failure in name resolution"
            r"|connection refused"
            r"|connection reset"
            r"|no route to host"
            r"|network is unreachable"
            r"|无法解析"
            r"|无法通过"
            r"|无法访问 clouddrive2"
            r"|19798 端口没有响应",
            re.IGNORECASE,
        ),
    ),
)

FAILURE_HEADLINES = {
    FAILURE_AUTH: "认证失败：CloudDrive2 拒绝了 WebDAV 用户名或密码（或网盘尚未登录）",
    FAILURE_PATH: "目标路径不存在：CloudDrive2 里找不到这个目录",
    FAILURE_NETWORK: "网络不通：Bot 连不到 CloudDrive2 的 19798 端口",
    FAILURE_PROTOCOL: "协议不匹配：19798 是 HTTP，当前配置却按 HTTPS 连接",
    FAILURE_CONTAINER: "CloudDrive2 容器没有正常运行",
    FAILURE_TIMEOUT: "操作超时：远程命令没有在限定时间内返回",
    FAILURE_UNKNOWN: "没有通过，但输出里没有可识别的具体原因",
}


def classify_failure(output: str) -> str:
    """把远程输出归类成 FAILURE_* 之一；无法识别时返回 FAILURE_UNKNOWN。"""
    text = str(output or "")
    for category, pattern in FAILURE_PATTERNS:
        if pattern.search(text):
            return category
    return FAILURE_UNKNOWN


# 终端控制序列：docker compose / BuildKit 的颜色、光标显隐、清行等。日志窗口是纯文本，
# 原样显示会出现 “[?25h”“[2K”“[32m” 这类乱码。
TERMINAL_CONTROL_RE = re.compile(
    r"\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)"  # OSC（例如设置窗口标题）
    r"|(?:\x1b\[|\x9b)[0-?]*[ -/]*[@-~]"  # CSI：颜色、光标移动、清行
    r"|\x1b[ -/]*[0-~]"  # 其他两字符 ESC 序列
    r"|[\x00-\x08\x0b-\x1f\x7f]"  # 其余 C0 控制字符（保留 \t 和 \n）
)


def strip_terminal_controls(text: str) -> str:
    """Remove ANSI/terminal control sequences so remote output reads as plain text."""
    return TERMINAL_CONTROL_RE.sub("", text)


def bounded_output_tail(
    output: str,
    max_lines: int = FAILURE_TAIL_LINES,
    width: int = FAILURE_TAIL_WIDTH,
) -> tuple[str, ...]:
    """取输出末尾若干非空行，逐行截断，保证弹窗内容有界。"""
    lines: list[str] = []
    normalized = str(output or "").replace(chr(13) + chr(10), chr(10)).replace(chr(13), chr(10))
    normalized = strip_terminal_controls(normalized)
    for raw in normalized.split(chr(10)):
        text = raw.strip()
        if not text:
            continue
        if width > 0 and len(text) > width:
            text = text[: max(width - 1, 1)] + "…"
        lines.append(text)
    if max_lines <= 0:
        return ()
    return tuple(lines[-max_lines:])


def format_diagnostic_tail(output: str) -> str:
    """有界的“原始诊断要点”段落；没有内容时返回空串。"""
    tail = bounded_output_tail(output)
    if not tail:
        return ""
    body = "\n".join(f"  {line}" for line in tail)
    return f"原始诊断要点（末尾片段）：\n{body}"


def failure_next_step(category: str, stage: str, label: str) -> str:
    """针对分类和阶段给出一个用户能立刻执行的动作。"""
    if category == FAILURE_AUTH:
        if stage == "verify":
            return (
                f"打开 CloudDrive2 管理页，确认已登录并挂载 {label}，"
                "且 WebDAV 用户名/密码与部署器里填的一致；"
                f"改好后重新点“一键部署（{label}）”，再点“检查是否可用”。"
            )
        return (
            "网络修复不会改动账号或凭据。请打开 CloudDrive2 管理页，"
            f"确认已登录并挂载 {label}，再用当前凭据重新点“一键部署（{label}）”，"
            "然后点“检查是否可用”。"
        )
    if category == FAILURE_PATH:
        return (
            "在 CloudDrive2 里确认这个目录存在；也可以把“目标路径”留空，"
            "表示直接使用 WebDAV 根目录。改好后重新点“一键部署”，再点“检查是否可用”。"
        )
    if category == FAILURE_PROTOCOL:
        return (
            "用当前版本部署器重新点“一键部署”（它会写入 HTTP 地址），"
            "然后再点“检查是否可用”。"
        )
    if category == FAILURE_CONTAINER:
        return (
            "点“登录并挂载网盘”确认容器在运行；"
            "如果打不开，先重新点“一键部署”，再点“修复网络”。"
        )
    if category == FAILURE_TIMEOUT:
        return (
            "容器可能还在启动：等 1–2 分钟后重试；连续超时请先点"
            "“修复网络”，再点“检查是否可用”。"
        )
    if category == FAILURE_NETWORK:
        if stage == "verify":
            return (
                "先点“修复网络”（只修网络，不做网盘写入校验），"
                "成功后再点“检查是否可用”。"
            )
        return (
            "确认 CloudDrive2 容器在运行且 19798 端口可访问；"
            "仍失败请把下面的诊断要点发给技术支持。"
        )
    return (
        "把下面的诊断要点发给技术支持；也可以先点“修复网络”，"
        "成功后再点“检查是否可用”。"
    )


def describe_failure(
    stage: str,
    output: str,
    destination: str = DESTINATION_CLOUDDRIVE2,
) -> str:
    """按阶段（repair/verify）生成具体失败说明：结论 + 下一步 + 有界原始要点。

    stage="repair" 只代表网络修复失败；stage="verify" 代表网盘写入验收失败。
    两者不共用同一句总错误，避免把认证/路径问题说成网络问题。
    """
    label = DESTINATION_LABELS.get(destination, DESTINATION_LABELS[DESTINATION_CLOUDDRIVE2])
    category = classify_failure(output)
    if stage == "repair":
        lead = "CloudDrive2 网络连接没有修复成功"
    else:
        lead = f"{label} 的 WebDAV 写入验收没有通过"
    lines = [
        f"{lead}：{FAILURE_HEADLINES[category]}",
        f"下一步：{failure_next_step(category, stage, label)}",
    ]
    tail = format_diagnostic_tail(output)
    if tail:
        lines.append(tail)
    return "\n".join(lines)


def vps_profile_name(values: Mapping[str, str]) -> str | None:
    """解析 VPS 预设；缺失或留空返回 None，表示沿用旧的内存阈值。"""
    raw = (values.get("vps_profile") or "").strip().lower()
    if not raw:
        return None
    if raw not in VPS_PROFILE_PRESETS:
        raise ValueError("VPS 配置只能是 minimum 或 standard，当前值：" + raw)
    return raw


def vps_memory_limits(values: Mapping[str, str]) -> tuple[str, str]:
    """按 VPS 预设返回 (软阈值, 硬阈值) MB；未指定预设时沿用旧的 1024/512。"""
    profile = vps_profile_name(values)
    if profile is None:
        return LEGACY_MEMORY_LIMITS
    return VPS_PROFILE_MEMORY_LIMITS[profile]


def build_deploy_config(values: Mapping[str, str]) -> str:
    """按目标生成 VPS 上的 .env 内容（键=值，UTF-8，无 CRLF）。

    两个分支都走 CloudDrive2 WebDAV：凭据、地址与相对路径共用同一组 CD2_* 键，
    只是 CD2_TARGET_PATH_B64 取本次选中网盘的相对路径。不输出任何 GDRIVE_* 变量。
    预算与磁盘保留直接取输入框的值（预设只在切换时写入，不在这里覆盖）。
    内存阈值跟随预设：最小配置 256/128，宽裕配置 1024/512，未指定预设沿用 1024/512。
    """
    destination = destination_type(values)
    memory_soft_mb, memory_hard_mb = vps_memory_limits(values)
    pairs: dict[str, str] = {
        "DESTINATION_TYPE": destination,
        "TELEGRAM_API_ID": (values.get("telegram_api_id") or "").strip(),
        "TELEGRAM_API_HASH_B64": b64((values.get("telegram_api_hash") or "").strip()),
        "BOT_TOKEN_B64": b64((values.get("bot_token") or "").strip()),
        "ALLOWED_USER_ID": (values.get("allowed_user_id") or "").strip(),
        "CD2_WEBDAV_URL_B64": b64(effective_webdav_url(values)),
        "CD2_WEBDAV_USERNAME_B64": b64(
            (values.get("cd2_username") or "").strip()
        ),
        "CD2_WEBDAV_PASSWORD_B64": b64(values.get("cd2_password") or ""),
        "CD2_TARGET_PATH_B64": b64(destination_target(values)),
    }
    pairs.update(
        {
            "LOCAL_TEMP_BUDGET_GB": (values.get("local_budget_gb") or "").strip(),
            "MIN_FREE_DISK_GB": (values.get("min_free_disk_gb") or "").strip(),
            "CONTROL_INTERVAL_SECONDS": "3",
            "CPU_TARGET_LOW_PERCENT": "60",
            "CPU_TARGET_HIGH_PERCENT": "80",
            "CPU_PRESSURE_PERCENT": "90",
            "MEMORY_SOFT_MIN_MB": memory_soft_mb,
            "MEMORY_HARD_MIN_MB": memory_hard_mb,
            "RAMP_UP_STEP": "1",
            "RAMP_DOWN_FACTOR": "0.5",
            "MAX_RETRIES": "3",
            "REMOTE_HEALTH_INTERVAL_SECONDS": "30",
            "TZ": (values.get("timezone") or "").strip() or "Asia/Shanghai",
            "TRANSFER_MODE": transfer_mode_value(values),
            # CloudDrive2 是共同传输层：是否托管容器只取用户勾选，与网盘选择无关。
            "DEPLOY_CLOUDDRIVE2": (
                "true"
                if (values.get("deploy_clouddrive2") or "true") == "true"
                else "false"
            ),
        }
    )
    return "\n".join(f"{key}={value}" for key, value in pairs.items()) + "\n"


def fingerprint_sha256(key: paramiko.PKey) -> str:
    digest = hashlib.sha256(key.asbytes()).digest()
    return "SHA256:" + base64.b64encode(digest).decode("ascii").rstrip("=")


class UserRejectedHostKey(paramiko.SSHException):
    pass


class ConfirmHostKeyPolicy(paramiko.MissingHostKeyPolicy):
    def __init__(self, confirm: Callable[[str, str, str], bool], known_hosts: Path):
        self.confirm = confirm
        self.known_hosts = known_hosts

    def missing_host_key(
        self, client: paramiko.SSHClient, hostname: str, key: paramiko.PKey
    ) -> None:
        fingerprint = fingerprint_sha256(key)
        if not self.confirm(hostname, key.get_name(), fingerprint):
            raise UserRejectedHostKey("用户拒绝了首次出现的 VPS 主机密钥")
        client._host_keys.add(hostname, key.get_name(), key)
        self.known_hosts.parent.mkdir(parents=True, exist_ok=True)
        client.save_host_keys(str(self.known_hosts))


# SSH 输出流保护：默认有限超时、有界缓冲、空闲提示与通道操作超时。
SSH_CHANNEL_OP_TIMEOUT = 15.0
SSH_RUN_DEFAULT_TIMEOUT = 900.0
SSH_EXIT_STATUS_GRACE = 2.0
SSH_IDLE_HINT_INTERVAL = 15.0
SSH_IDLE_HINT_TEXT = "SSH 仍连接，等待远端输出……"
SSH_OUTPUT_TAIL_LIMIT = 256 * 1024
SSH_LINE_BUFFER_LIMIT = 8192
SSH_READ_CHUNK = 65536


class RemoteSession:
    def __init__(
        self,
        values: dict[str, str],
        confirm_host_key: Callable[[str, str, str], bool],
    ):
        self.values = values
        self.confirm_host_key = confirm_host_key
        app_data = Path(os.getenv("APPDATA", Path.home()))
        self.known_hosts = app_data / "TG115-Deployer" / "known_hosts"
        self.client = paramiko.SSHClient()
        if self.known_hosts.exists():
            self.client.load_host_keys(str(self.known_hosts))
        self.client.set_missing_host_key_policy(
            ConfirmHostKeyPolicy(confirm_host_key, self.known_hosts)
        )

    def connect(self) -> None:
        kwargs: dict[str, object] = {
            "hostname": self.values["vps_host"].strip(),
            "port": int(self.values["vps_port"]),
            "username": self.values["vps_user"].strip(),
            "timeout": 15,
            "banner_timeout": 20,
            "auth_timeout": 20,
            "look_for_keys": False,
            "allow_agent": False,
        }
        if self.values["auth_method"] == "密码":
            kwargs["password"] = self.values["vps_password"]
        else:
            kwargs["key_filename"] = self.values["ssh_key_path"]
            passphrase = self.values["ssh_key_passphrase"]
            if passphrase:
                kwargs["passphrase"] = passphrase
        self.client.connect(**kwargs)
        transport = self.client.get_transport()
        if transport:
            transport.set_keepalive(30)

    def close(self) -> None:
        self.client.close()

    def run(
        self,
        command: str,
        *,
        sudo: bool = False,
        stream: Callable[[str], None] | None = None,
        timeout: float | None = None,
    ) -> tuple[int, str]:
        transport = self.client.get_transport()
        if transport is None or not transport.is_active():
            raise RuntimeError("SSH 连接已经断开")
        limit = SSH_RUN_DEFAULT_TIMEOUT if timeout is None else float(timeout)
        channel = transport.open_session(timeout=SSH_CHANNEL_OP_TIMEOUT)
        try:
            # paramiko 5 的 get_pty/exec_command 不接受 timeout 参数：
            # 读写用 settimeout 限时，请求应答用有界 join 兜底。
            channel.settimeout(SSH_CHANNEL_OP_TIMEOUT)
            actual = (
                f"sudo -S -p '' -- sh -c {shlex.quote(command)}"
                if sudo else command
            )
            password = self.values.get("sudo_password", "") if sudo else ""
            setup_error: list[BaseException] = []

            def setup_channel() -> None:
                channel.get_pty(width=180, height=40)
                # All callers use fixed commands, validated paths, or shlex.quote.
                channel.exec_command(actual)  # nosec B601
                if sudo:
                    channel.sendall((password + "\n").encode("utf-8"))

            def setup_target() -> None:
                try:
                    setup_channel()
                except BaseException as exc:  # noqa: BLE001 - forward thread failures to the caller
                    setup_error.append(exc)

            setup_thread = threading.Thread(
                target=setup_target, name="ssh-channel-setup", daemon=True
            )
            setup_thread.start()
            setup_thread.join(SSH_CHANNEL_OP_TIMEOUT)
            if setup_thread.is_alive():
                raise TimeoutError(f"SSH 通道初始化超时：{command}")
            if setup_error:
                raise setup_error[0]

            # 增量解码：多字节 UTF-8 可能被 recv 分片切断，逐片 decode 会出乱码。
            decoder = codecs.getincrementaldecoder("utf-8")(errors="replace")
            stderr_decoder = codecs.getincrementaldecoder("utf-8")(errors="replace")
            output_parts: list[str] = []
            output_len = 0
            pending = ""
            started = time.monotonic()
            last_activity = started
            last_hint = started

            def emit(line: str) -> None:
                if line and stream:
                    stream(line)

            def feed(text: str) -> None:
                """保留有界尾部输出，并把完整行或 CR 进度立刻交给 stream。"""
                nonlocal output_len, pending
                if not text:
                    return
                output_parts.append(text)
                output_len += len(text)
                while output_len > SSH_OUTPUT_TAIL_LIMIT and len(output_parts) > 1:
                    output_len -= len(output_parts.pop(0))
                if output_len > SSH_OUTPUT_TAIL_LIMIT:
                    excess = output_len - SSH_OUTPUT_TAIL_LIMIT
                    output_parts[0] = output_parts[0][excess:]
                    output_len -= excess
                pending += text
                while True:
                    index_n = pending.find("\n")
                    index_r = pending.find("\r")
                    if index_n < 0 and index_r < 0:
                        break
                    if index_r < 0 or (0 <= index_n < index_r):
                        index = index_n
                    else:
                        index = index_r
                    line = pending[:index]
                    pending = pending[index + 1 :]
                    emit(line)
                if len(pending) > SSH_LINE_BUFFER_LIMIT:
                    # 单行没有换行（进度条/长日志）也要及时交出，不能无限攒。
                    emit(pending)
                    pending = ""

            def exit_status_received() -> bool:
                # paramiko 关闭通道时也会置位 status_event，只有 exit_status
                # 从哨兵 -1 变成真实退出码才说明远端确实报了退出状态。
                return getattr(channel, "exit_status", -1) != -1

            def wait_exit_status() -> None:
                grace_end = time.monotonic() + SSH_EXIT_STATUS_GRACE
                while not exit_status_received() and time.monotonic() < grace_end:
                    time.sleep(0.05)
                if not exit_status_received():
                    raise RuntimeError(
                        "SSH 通道已关闭，连接可能已断开，未收到命令退出状态"
                    )

            while True:
                now = time.monotonic()
                if limit > 0 and now - started > limit:
                    raise TimeoutError(f"远程命令超时：{command}")
                progressed = False
                if channel.recv_ready():
                    try:
                        chunk: bytes | None = channel.recv(SSH_READ_CHUNK)
                    except (TimeoutError, OSError):
                        chunk = None
                    if chunk is not None:
                        if not chunk:
                            # 远端关闭通道：先给退出状态一点到达时间。
                            wait_exit_status()
                            break
                        last_activity = now
                        feed(decoder.decode(chunk))
                        progressed = True
                if channel.recv_stderr_ready():
                    try:
                        err = channel.recv_stderr(SSH_READ_CHUNK)
                    except (TimeoutError, OSError):
                        err = b""
                    if err:
                        last_activity = now
                        feed(stderr_decoder.decode(err))
                        progressed = True
                if not channel.recv_ready() and (
                    getattr(channel, "closed", False)
                    or getattr(channel, "eof_received", False)
                ):
                    wait_exit_status()
                    break
                if channel.exit_status_ready() and not channel.recv_ready():
                    if not exit_status_received():
                        wait_exit_status()
                    break
                if not progressed:
                    if (
                        stream
                        and now - last_activity >= SSH_IDLE_HINT_INTERVAL
                        and now - last_hint >= SSH_IDLE_HINT_INTERVAL
                    ):
                        # 只说明连接还在，不伪造任何进度。
                        stream(SSH_IDLE_HINT_TEXT)
                        last_hint = now
                    time.sleep(0.05)
            feed(decoder.decode(b"", final=True))
            feed(stderr_decoder.decode(b"", final=True))
            if pending:
                emit(pending)
            code = channel.recv_exit_status()
            return code, "".join(output_parts)
        finally:
            channel.close()

    def sftp(self) -> paramiko.SFTPClient:
        return self.client.open_sftp()


class ForwardHandler(socketserver.BaseRequestHandler):
    ssh_transport: paramiko.Transport
    remote_host = "127.0.0.1"
    remote_port = 19798

    def handle(self) -> None:
        try:
            channel = self.ssh_transport.open_channel(
                "direct-tcpip",
                (self.remote_host, self.remote_port),
                self.request.getpeername(),
            )
        except (OSError, paramiko.SSHException):
            return
        if channel is None:
            return
        try:
            while True:
                readable, _, _ = select.select([self.request, channel], [], [], 1)
                if self.request in readable:
                    data = self.request.recv(65536)
                    if not data:
                        break
                    channel.sendall(data)
                if channel in readable:
                    data = channel.recv(65536)
                    if not data:
                        break
                    self.request.sendall(data)
        finally:
            channel.close()
            self.request.close()


class Tunnel:
    def __init__(self, session: RemoteSession):
        transport = session.client.get_transport()
        if transport is None:
            raise RuntimeError("SSH 连接不可用")
        handler_type = type(
            "CloudDriveForwardHandler",
            (ForwardHandler,),
            {"ssh_transport": transport},
        )
        self.session = session
        self.server = socketserver.ThreadingTCPServer(("127.0.0.1", 0), handler_type)
        self.server.daemon_threads = True
        self.port = int(self.server.server_address[1])
        self.thread = threading.Thread(
            target=self.server.serve_forever, name="cd2-tunnel", daemon=True
        )

    def start(self) -> None:
        self.thread.start()

    def close(self) -> None:
        self.server.shutdown()
        self.server.server_close()
        self.session.close()


COLOR_BG = "#F5F6F8"
COLOR_CARD = "#FFFFFF"
COLOR_HEADING = "#1E3A5F"
COLOR_TEXT = "#0F172A"
COLOR_MUTED = "#475569"
COLOR_BORDER = "#E4E7EB"
COLOR_FIELD = "#F8FAFC"
COLOR_FOCUS = "#2563EB"
COLOR_PRIMARY = "#1E3A5F"
COLOR_PRIMARY_ACTIVE = "#16304F"
COLOR_SECONDARY_BG = "#F1F3F5"
COLOR_LOG_BG = "#F8FAFC"
COLOR_LOG_FG = "#0F172A"
COLOR_LOG_MUTED = "#475569"
COLOR_OK = "#047857"
COLOR_WARN = "#B45309"
COLOR_ERROR = "#DC2626"
COLOR_BADGE_CD2 = "#DBEAFE"
COLOR_BADGE_GDRIVE = "#D1FAE5"
COLOR_SELECTED = "#EFF4FF"
FONT = "Segoe UI"
FONT_MONO = "Cascadia Mono"
SPACE = 8
__TG115_UI_LAYOUT__ = "fluent-light-v1"

# 日志与 UI 队列预算：Docker build / PTY 会一次性吐出上万行，
# 逐行 insert+see 会把 Tk 主线程锁在单个 after 回调里（用户看到的“卡死”）。
# 这里把渲染改成“有界批量 + 时间预算”，保证心跳与完成回调总能及时执行。
LOG_HISTORY_MAX_LINES = 4000  # 日志控件里保留的可见历史行数
LOG_MAX_LINE_CHARS = 4000  # 单行最长字符数，超出截断
LOG_PENDING_MAX_LINES = 20000  # 主线程待渲染缓冲上限，超出丢弃最旧行
LOG_FLUSH_MAX_LINES = 400  # 单批最多渲染行数
LOG_FLUSH_BUDGET_SECONDS = 0.012  # 单批渲染时间预算
LOG_MAIN_SYNC_BURST = 8  # 主线程连续 _log 时允许的即时渲染条数
LOG_TRUNCATED_SUFFIX = " ……（已截断"
UI_DRAIN_MAX_CALLBACKS = 64  # 单次 after 回调最多处理的 UI 回调数
UI_DRAIN_BUDGET_SECONDS = 0.02  # 单次 after 回调的时间预算
UI_DRAIN_INTERVAL_MS = 100  # 队列空闲时的轮询间隔
UI_DRAIN_BUSY_INTERVAL_MS = 5  # 队列还有积压时的轮询间隔


class ScrollableFrame(tk.Frame):
    """带滚动条的容器，保证表单在小屏或高 DPI 缩放下也能完整填写。"""

    def __init__(self, parent: tk.Misc, *, height: int = 430) -> None:
        super().__init__(parent, bg=COLOR_BG)
        self.canvas = tk.Canvas(
            self, height=height, highlightthickness=0, bd=0, bg=COLOR_BG
        )
        self.scrollbar = ttk.Scrollbar(
            self, orient=tk.VERTICAL, command=self.canvas.yview
        )
        self.canvas.configure(yscrollcommand=self.scrollbar.set)
        self.canvas.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        self.scrollbar.pack(side=tk.RIGHT, fill=tk.Y)
        self.body = tk.Frame(self.canvas, bg=COLOR_BG)
        self.body.columnconfigure(0, weight=1)
        self._window = self.canvas.create_window(
            (0, 0), window=self.body, anchor=tk.NW
        )
        self.body.bind("<Configure>", self._on_body_configure)
        self.canvas.bind("<Configure>", self._on_canvas_configure)
        self.canvas.bind("<Enter>", self._bind_wheel)
        self.canvas.bind("<Leave>", self._unbind_wheel)

    def _on_body_configure(self, _event: tk.Event) -> None:
        region = self.canvas.bbox("all")
        if region:
            self.canvas.configure(scrollregion=region)

    def _on_canvas_configure(self, event: tk.Event) -> None:
        self.canvas.itemconfigure(self._window, width=event.width)

    def _bind_wheel(self, _event: tk.Event) -> None:
        self.canvas.bind_all("<MouseWheel>", self._on_wheel)

    def _unbind_wheel(self, _event: tk.Event) -> None:
        self.canvas.unbind_all("<MouseWheel>")

    def _on_wheel(self, event: tk.Event) -> None:
        if not event.delta:
            return
        self.canvas.yview_scroll(int(-1 * (event.delta / 120)), "units")


CD2_SHARED_HELP = (
    "两个网盘都通过 CloudDrive2 的 WebDAV 写入，因此这里填写的是同一套 "
    "CloudDrive2 连接信息。请先在 CloudDrive2 管理页登录并挂载你本次要写入的网盘"
    "（115 或 Google Drive），再开启 WebDAV。部署器不导入 Google 授权、"
    "不直接调用 Google API。WebDAV 验收只证明 CloudDrive2 已接收并校验文件，"
    "不代表 115 或 Google Drive 官方端已经确认。"
)


# 新手主流程：从上往下按 ① → ④ 填写，④ 里只有一个主按钮；不常用的都收进「高级选项」。
STEP_NUMBERS = ("①", "②", "③", "④")
BEGINNER_STEPS = (
    ("连接服务器", "填 VPS 服务商给你的 IP、用户名和密码（或 SSH 私钥）。"),
    ("Telegram 信息", "填 Bot 的 Token 和你自己的 Telegram 信息，Bot 只听你一个人的。"),
    ("保存位置", "选 115 还是 Google Drive，填保存文件夹和 CloudDrive2 的 WebDAV 地址、账号。"),
    ("部署并检查", "先点「一键部署」；成功后按顺序做第 2、3 小步。"),
)
BEGINNER_STEPS_NOTE = (
    "按 ① → ④ 从上往下填，最后点「一键部署」。"
    "已经部署过？不用重新安装，直接做 ④ 的第 2、3 小步。"
)
DEPLOY_SUBSTEPS = (
    ("第1小步", "把程序装到 VPS，约 5–15 分钟，期间不要关窗口"),
    ("第2小步", "部署成功后点：在 CloudDrive2 里登录并挂载你选的网盘"),
    ("第3小步", "挂载好后点：自动写入小文件检查，通过才算真正可用"),
)
DEPLOY_BUTTON_TEXTS = ("一键部署", "登录并挂载网盘", "检查是否可用")
ADVANCED_TOGGLE_CLOSED = "▸ 高级选项（一般不用改，出问题时再看）"
ADVANCED_TOGGLE_OPEN = "▾ 收起高级选项"
CD2_URL_HINT_MANAGED = (
    "CloudDrive2 和 Bot 装在同一台 VPS：自动使用内部地址 "
    f"{MANAGED_CD2_WEBDAV_URL}，不用改"
)
CD2_URL_HINT_CUSTOM = (
    "填你的 CloudDrive2 WebDAV 地址，格式 http://IP:19798/dav；"
    "同一内网可以用 http，走公网必须用 https://域名/dav"
)
MORE_HELP = (
    "只有出问题时才用：一键部署已包含 SSH 连接测试；「修复网络」用于 CloudDrive2 连不上网盘；"
    "「查看 VPS 状态」只读查看容器和最近日志；「清空日志」只清空下面的部署记录。"
)
LABEL_WIDTH = 18
# 输入框下方的一句话提示：告诉新手去哪里找这个值。
FIELD_HINTS = {
    "vps_host": "VPS 服务商发给你的公网 IP，例如 203.0.113.10（也可以填域名）",
    "vps_user": "通常是 root",
    "vps_port": "通常是 22",
    "vps_password": "VPS 的登录密码（服务商邮件或控制台里能找到）",  # nosec B105 - helper text, not a credential
    "ssh_key_path": "点「选择」找到你电脑上的私钥文件",
    "bot_token": "在 Telegram 找 @BotFather，发 /newbot 创建机器人，复制它回复的 Token",  # nosec B105 - helper text, not a credential
    "telegram_api_id": "打开 my.telegram.org → API development tools",
    "telegram_api_hash": "和 API ID 在同一个页面",
    "allowed_user_id": "在 Telegram 找 @userinfobot，它回复的 Id 就是；只有这个账号能用 Bot",
    "cd2_target": (
        "默认 115/telegram转存，不存在会自动创建；留空或只填 115 也存到这里，"
        "不会存到网盘根目录"
    ),
    "gdrive_target": (
        "默认 GoogleDrive/telegram转存，不存在会自动创建；留空或只填 GoogleDrive"
        " 也存到这里，不会存到网盘根目录"
    ),
    "cd2_username": "CloudDrive2 开启 WebDAV 时设置的用户名；首次部署先自己定一组，部署后在 CloudDrive2 里设成同样的",
    "cd2_password": "和上面的用户名配套的 WebDAV 密码",  # nosec B105 - helper text, not a credential
}
# 出错时告诉用户去哪一步改哪个输入框：字段名 -> (步骤, 界面上的名字)。
FIELD_LOCATIONS = {
    "vps_host": ("① 连接服务器", "VPS 地址"),
    "vps_port": ("① 连接服务器", "SSH 端口"),
    "vps_user": ("① 连接服务器", "用户名"),
    "vps_password": ("① 连接服务器", "登录密码"),
    "ssh_key_path": ("① 连接服务器", "SSH 私钥文件"),
    "bot_token": ("② Telegram 信息", "Bot Token"),
    "telegram_api_id": ("② Telegram 信息", "API ID"),
    "telegram_api_hash": ("② Telegram 信息", "API Hash"),
    "allowed_user_id": ("② Telegram 信息", "你的数字 ID"),
    "cd2_target": ("③ 保存位置", "文件夹"),
    "gdrive_target": ("③ 保存位置", "文件夹"),
    "cd2_username": ("③ 保存位置", "WebDAV 用户名"),
    "cd2_password": ("③ 保存位置", "WebDAV 密码"),
    "cd2_url": ("③ 保存位置", "WebDAV 地址"),
    "sudo_password": ("高级选项 → 服务器细节", "sudo 密码"),
    "install_dir": ("高级选项 → 服务器细节", "安装目录"),
    "timezone": ("高级选项 → 服务器细节", "时区"),
    "local_budget_gb": ("高级选项 → 容量与传输方式", "本地任务预算"),
    "min_free_disk_gb": ("高级选项 → 容量与传输方式", "磁盘最少保留"),
    "vps_profile": ("高级选项 → 容量与传输方式", "VPS 配置"),
}
ADVANCED_FIELDS = frozenset(
    name for name, (step, _label) in FIELD_LOCATIONS.items() if step.startswith("高级选项")
)
DESTINATION_TARGET_FIELDS = {
    DESTINATION_CLOUDDRIVE2: "cd2_target",
    DESTINATION_GDRIVE: "gdrive_target",
}
STATUS_BUSY = "正在处理，请稍候"
STATUS_IDLE = "等待操作，网盘状态尚未检查"


class InstallerApp:
    def __init__(self, root: tk.Tk):
        self.root = root
        self.root.title(f"{APP_TITLE} v{APP_VERSION} · {LOCAL_BUILD_LABEL}")
        self.root.geometry("1180x860")
        self.root.minsize(980, 680)
        self.values: dict[str, tk.StringVar] = {}
        self.busy = False
        self.tunnel: Tunnel | None = None
        self._secrets: list[str] = []
        self._token_secrets: list[str] = []
        self._destination_cards: dict[str, tk.Frame] = {}
        self._target_frames: dict[str, tk.Frame] = {}
        self._applied_destination: str | None = None
        self._field_rows: dict[str, tuple[tk.Frame, tk.Label]] = {}
        self._field_entries: dict[str, ttk.Entry] = {}
        self._status_text = tk.StringVar(value=STATUS_IDLE)
        self._advanced_open = tk.BooleanVar(value=False)
        # 后台线程只往队列里放回调，绝不在工作线程里调用 Tk。
        self._ui_queue: queue.Queue[Callable[[], None]] = queue.Queue()
        self._drain_job: str | None = None
        # 日志先进入有界缓冲，主线程按批插入，避免逐行滚动阻塞窗口。
        self._log_lock = threading.Lock()
        self._log_pending: list[tuple[str, tuple[str, ...]]] = []
        self._log_pending_dropped = 0
        self._log_flush_queued = False
        self._log_flush_job: str | None = None
        # 主线程连续 _log 的即时渲染额度：事件循环每转一圈补满，
        # 单条日志仍立即可读，主线程里的成批输出不会退化成逐行渲染。
        self._main_log_tokens = LOG_MAIN_SYNC_BURST
        self._configure_style()
        self._build_ui()
        self._start_ui_drain()
        self.root.protocol("WM_DELETE_WINDOW", self._on_close)

    def _configure_style(self) -> None:
        try:
            self.root.configure(background=COLOR_BG)
        except tk.TclError:
            return
        style = ttk.Style(self.root)
        try:
            if "clam" in style.theme_names():
                style.theme_use("clam")
        except tk.TclError:
            return
        style.configure(".", font=(FONT, 10), background=COLOR_BG)
        style.configure("TFrame", background=COLOR_BG)
        style.configure("Card.TFrame", background=COLOR_CARD)
        style.configure(
            "TEntry",
            fieldbackground=COLOR_FIELD,
            bordercolor=COLOR_BORDER,
            lightcolor=COLOR_BORDER,
            darkcolor=COLOR_BORDER,
            padding=(10, 7),
            relief="flat",
        )
        style.map(
            "TEntry",
            bordercolor=[("focus", COLOR_FOCUS), ("!focus", COLOR_BORDER)],
            lightcolor=[("focus", COLOR_FOCUS)],
            darkcolor=[("focus", COLOR_FOCUS)],
        )
        style.configure(
            "TCombobox",
            fieldbackground=COLOR_FIELD,
            bordercolor=COLOR_BORDER,
            padding=(8, 6),
            arrowsize=14,
        )
        style.map(
            "TCombobox",
            bordercolor=[("focus", COLOR_FOCUS), ("readonly focus", COLOR_FOCUS)],
            fieldbackground=[("readonly", COLOR_FIELD)],
        )
        style.configure(
            "Primary.TButton",
            background=COLOR_PRIMARY,
            foreground="#FFFFFF",
            borderwidth=0,
            padding=(18, 9),
            font=(FONT, 10, "bold"),
            focuscolor=COLOR_FOCUS,
        )
        style.map(
            "Primary.TButton",
            background=[
                ("disabled", "#94A3B8"),
                ("pressed", COLOR_PRIMARY_ACTIVE),
                ("active", "#27486F"),
            ],
            foreground=[("disabled", "#F8FAFC")],
        )
        style.configure(
            "Secondary.TButton",
            background=COLOR_SECONDARY_BG,
            foreground=COLOR_TEXT,
            borderwidth=0,
            padding=(14, 8),
            font=(FONT, 10),
        )
        style.map(
            "Secondary.TButton",
            background=[
                ("disabled", "#F1F3F5"),
                ("pressed", "#E2E8F0"),
                ("active", "#E7EBEF"),
            ],
        )
        style.configure(
            "Quiet.TButton",
            background=COLOR_BG,
            foreground=COLOR_MUTED,
            borderwidth=0,
            padding=(10, 7),
            font=(FONT, 9),
        )
        style.map(
            "Quiet.TButton",
            background=[("active", COLOR_SECONDARY_BG), ("pressed", "#E2E8F0")],
            foreground=[("disabled", "#94A3B8")],
        )
        style.configure(
            "TCheckbutton",
            background=COLOR_CARD,
            foreground=COLOR_TEXT,
            font=(FONT, 10),
            focuscolor=COLOR_FOCUS,
        )
        style.configure(
            "TRadiobutton",
            background=COLOR_CARD,
            foreground=COLOR_TEXT,
            font=(FONT, 10),
            focuscolor=COLOR_FOCUS,
        )
        style.configure(
            "Status.Horizontal.TProgressbar",
            troughcolor="#E7EDF4",
            background=COLOR_FOCUS,
            bordercolor="#E7EDF4",
            lightcolor=COLOR_FOCUS,
            darkcolor=COLOR_FOCUS,
            thickness=4,
        )

    def _var(self, name: str, default: str = "") -> tk.StringVar:
        variable = tk.StringVar(value=default)
        self.values[name] = variable
        return variable

    def _start_ui_drain(self) -> None:
        self._drain_job = self.root.after(100, self._drain_ui_queue)

    def _post(self, callback: Callable[[], None]) -> None:
        """从任意线程提交 UI 回调：主线程直接执行，后台线程排队等主线程 after 取出。"""
        if threading.current_thread() is threading.main_thread():
            callback()
        else:
            self._ui_queue.put(callback)

    def _drain_ui_queue(self) -> None:
        """有界消化 UI 回调：单次 after 只做有限条数/有限时间，剩下的下一轮再来。

        旧实现用 while True 一次跑空队列，Docker/PTY 的上万行日志回调会把主线程
        锁死在这一个回调里，心跳、完成状态更新和窗口重绘都得不到执行。
        """
        self._drain_job = None
        # 事件循环回到这里说明主线程空出来了：补满即时渲染额度。
        self._main_log_tokens = LOG_MAIN_SYNC_BURST
        deadline = time.monotonic() + UI_DRAIN_BUDGET_SECONDS
        processed = 0
        while processed < UI_DRAIN_MAX_CALLBACKS:
            try:
                callback = self._ui_queue.get_nowait()
            except queue.Empty:
                break
            processed += 1
            try:
                callback()
            except tk.TclError:
                # 窗口已经销毁，剩余回调没有意义。
                return
            if time.monotonic() > deadline:
                break
        delay = (
            UI_DRAIN_INTERVAL_MS
            if self._ui_queue.empty()
            else UI_DRAIN_BUSY_INTERVAL_MS
        )
        try:
            self._drain_job = self.root.after(delay, self._drain_ui_queue)
        except tk.TclError:
            self._drain_job = None

    def _stop_ui_drain(self) -> None:
        for job in (self._drain_job, self._log_flush_job):
            if job is None:
                continue
            try:
                self.root.after_cancel(job)
            except tk.TclError:
                pass
        self._drain_job = None
        self._log_flush_job = None
        with self._log_lock:
            self._log_flush_queued = False

    def _build_ui(self) -> None:
        """新手主流程：从上往下 ①②③ 填写，在 ④ 点一个主按钮；其余收进「高级选项」。"""
        outer = tk.Frame(self.root, bg=COLOR_BG, padx=SPACE * 2, pady=SPACE * 2)
        outer.pack(fill=tk.BOTH, expand=True)
        outer.columnconfigure(0, weight=1)
        # 表单（①②③ 和高级选项）拿走多余高度；部署记录保持紧凑。
        outer.rowconfigure(2, weight=1)

        self._build_header(outer)
        self._steps_note = tk.Label(
            outer,
            text=BEGINNER_STEPS_NOTE,
            bg=COLOR_BG,
            fg=COLOR_MUTED,
            justify=tk.LEFT,
            anchor=tk.W,
            wraplength=1100,
            font=(FONT, 9),
        )
        self._steps_note.grid(row=1, column=0, sticky=tk.EW, pady=(4, 0))
        outer.bind("<Configure>", self._on_outer_configure, add="+")

        form = ScrollableFrame(outer, height=430)
        form.grid(row=2, column=0, sticky=tk.NSEW, pady=(SPACE, 0))
        self.form = form
        body = form.body
        self._step_cards: list[tk.Frame] = []
        self._build_connection_step(body, 0)
        self._build_telegram_step(body, 1)
        self._build_destination_step(body, 2)
        self._build_advanced_section(body, 3)

        self._build_deploy_step(outer, 3)

        status = tk.Frame(outer, bg=COLOR_BG)
        status.grid(row=4, column=0, sticky=tk.EW, pady=(SPACE, 0))
        status.columnconfigure(0, weight=1)
        tk.Label(
            status,
            textvariable=self._status_text,
            bg=COLOR_BG,
            fg=COLOR_MUTED,
            anchor=tk.W,
            font=(FONT, 9),
        ).grid(row=0, column=0, sticky=tk.EW, pady=(0, 4))
        self.progress = ttk.Progressbar(
            status, mode="indeterminate", style="Status.Horizontal.TProgressbar"
        )
        self.progress.grid(row=1, column=0, sticky=tk.EW)

        console = tk.Frame(
            outer,
            bg=COLOR_CARD,
            highlightbackground=COLOR_BORDER,
            highlightthickness=1,
            padx=SPACE,
            pady=SPACE,
        )
        console.grid(row=5, column=0, sticky=tk.NSEW, pady=(SPACE, 0))
        console.rowconfigure(1, weight=1)
        console.columnconfigure(0, weight=1)
        tk.Label(
            console,
            text="部署记录（出错时看最后几行）",
            bg=COLOR_CARD,
            fg=COLOR_HEADING,
            font=(FONT, 10, "bold"),
            anchor=tk.W,
        ).grid(row=0, column=0, sticky=tk.W, pady=(0, 4))
        self.log = scrolledtext.ScrolledText(
            console,
            height=3,
            font=(FONT_MONO, 10),
            wrap=tk.WORD,
            state=tk.NORMAL,
            bg=COLOR_LOG_BG,
            fg=COLOR_LOG_FG,
            insertbackground=COLOR_LOG_FG,
            relief=tk.FLAT,
            padx=8,
            pady=6,
            spacing1=1,
            spacing3=2,
        )
        self.log.grid(row=1, column=0, sticky=tk.NSEW)
        self.log.tag_configure("ok", foreground=COLOR_OK)
        self.log.tag_configure("warn", foreground=COLOR_WARN)
        self.log.tag_configure("error", foreground=COLOR_ERROR)
        self.log.tag_configure("info", foreground=COLOR_LOG_MUTED)

        self._apply_destination_state()
        self._adopt_status_button()
        self._log(
            "准备就绪。按 ① → ④ 从上往下填写，最后点「一键部署」。"
            "密码只保存在当前程序内存和 VPS 的受限配置文件中。"
        )

    def _build_header(self, parent: tk.Frame) -> None:
        header = tk.Frame(parent, bg=COLOR_BG)
        header.grid(row=0, column=0, sticky=tk.EW)
        header.columnconfigure(0, weight=1)
        title = tk.Frame(header, bg=COLOR_BG)
        title.grid(row=0, column=0, sticky=tk.W)
        tk.Label(
            title,
            text="TG115",
            bg=COLOR_BG,
            fg=COLOR_HEADING,
            font=(FONT, 20, "bold"),
        ).pack(side=tk.LEFT)
        tk.Label(
            title,
            text=f"部署器   v{APP_VERSION} {LOCAL_BUILD_LABEL}",
            bg=COLOR_BG,
            fg=COLOR_MUTED,
            font=(FONT, 11),
        ).pack(side=tk.LEFT, padx=(SPACE, 0))
        # 隐藏的徽标只给状态文字使用，不占布局。
        self.badge = tk.Label(header, text="", bg=COLOR_BG, fg=COLOR_BG, font=(FONT, 1))

    def _on_outer_configure(self, event: tk.Event) -> None:
        """按窗口宽度重排说明文字，窄窗口不出现横向溢出。"""
        width = max(int(event.width), 320)
        self._steps_note.configure(wraplength=max(width - 4 * SPACE, 240))

    def _step_card(
        self, parent: tk.Misc, row: int, index: int, *, grid: bool = True
    ) -> tuple[tk.Frame, tk.Frame]:
        """一个编号步骤卡片：圆圈编号 + 标题 + 一句话说明，返回 (card, body)。"""
        title, purpose = BEGINNER_STEPS[index]
        card = tk.Frame(
            parent,
            bg=COLOR_CARD,
            highlightbackground=COLOR_BORDER,
            highlightthickness=1,
            padx=SPACE * 2,
            pady=SPACE + 4,
        )
        if grid:
            card.grid(row=row, column=0, sticky=tk.EW, pady=(0, SPACE))
        head = tk.Frame(card, bg=COLOR_CARD)
        head.pack(fill=tk.X)
        tk.Label(
            head,
            text=STEP_NUMBERS[index],
            bg=COLOR_CARD,
            fg=COLOR_PRIMARY,
            font=(FONT, 16, "bold"),
        ).pack(side=tk.LEFT)
        tk.Label(
            head,
            text=title,
            bg=COLOR_CARD,
            fg=COLOR_HEADING,
            font=(FONT, 12, "bold"),
        ).pack(side=tk.LEFT, padx=(6, 0))
        purpose_label = tk.Label(
            head,
            text=purpose,
            bg=COLOR_CARD,
            fg=COLOR_MUTED,
            anchor=tk.W,
            justify=tk.LEFT,
            font=(FONT, 9),
        )
        purpose_label.pack(side=tk.LEFT, padx=(SPACE, 0), fill=tk.X, expand=True)
        body = tk.Frame(card, bg=COLOR_CARD)
        body.pack(fill=tk.X, pady=(SPACE, 0))
        body.columnconfigure(1, weight=1)
        card.step_title = title  # type: ignore[attr-defined]
        card.purpose_label = purpose_label  # type: ignore[attr-defined]
        self._step_cards.append(card)
        return card, body

    def _two_columns(self, parent: tk.Frame, row: int) -> tuple[tk.Frame, tk.Frame]:
        columns = tk.Frame(parent, bg=COLOR_CARD)
        columns.grid(row=row, column=0, columnspan=3, sticky=tk.EW)
        columns.columnconfigure(0, weight=1, uniform="pair")
        columns.columnconfigure(1, weight=1, uniform="pair")
        left = tk.Frame(columns, bg=COLOR_CARD)
        left.grid(row=0, column=0, sticky=tk.NSEW, padx=(0, SPACE))
        left.columnconfigure(1, weight=1)
        right = tk.Frame(columns, bg=COLOR_CARD)
        right.grid(row=0, column=1, sticky=tk.NSEW, padx=(SPACE, 0))
        right.columnconfigure(1, weight=1)
        return left, right

    def _build_connection_step(self, parent: tk.Frame, row: int) -> None:
        _card, body = self._step_card(parent, row, 0)
        self._entry(body, 0, "VPS 地址（IP）", "vps_host", hint=FIELD_HINTS["vps_host"])
        left, right = self._two_columns(body, 1)
        self._entry(left, 0, "用户名", "vps_user", "root", width=16, hint=FIELD_HINTS["vps_user"])
        self._entry(right, 0, "SSH 端口", "vps_port", "22", width=10, hint=FIELD_HINTS["vps_port"])
        tk.Label(
            body, text="登录方式", bg=COLOR_CARD, fg=COLOR_MUTED, width=LABEL_WIDTH, anchor=tk.W, font=(FONT, 10)
        ).grid(row=2, column=0, sticky=tk.W, pady=3)
        method = ttk.Combobox(
            body,
            textvariable=self._var("auth_method", "密码"),
            values=("密码", "SSH 密钥"),
            state="readonly",
            width=14,
        )
        method.grid(row=2, column=1, sticky=tk.W, padx=(SPACE, 0), pady=3)
        self.values["auth_method"].trace_add(
            "write", lambda *_args: self._apply_auth_visibility()
        )
        self._entry(
            body, 3, "登录密码", "vps_password", secret=True, hint=FIELD_HINTS["vps_password"]
        )
        self._key_panel = tk.Frame(body, bg=COLOR_CARD)
        self._key_panel.grid(row=4, column=0, columnspan=3, sticky=tk.EW)
        self._key_panel.columnconfigure(1, weight=1)
        key_entry = self._entry(
            self._key_panel, 0, "SSH 私钥文件", "ssh_key_path", hint=FIELD_HINTS["ssh_key_path"]
        )
        ttk.Button(
            self._field_rows["ssh_key_path"][0],
            text="选择",
            style="Secondary.TButton",
            command=lambda: self._choose_key(key_entry),
        ).grid(row=0, column=2, padx=(SPACE, 0))
        self._entry(self._key_panel, 1, "私钥口令（没有就留空）", "ssh_key_passphrase", secret=True)
        self._key_panel.grid_remove()
        self._apply_auth_visibility()
        test_row = tk.Frame(body, bg=COLOR_CARD)
        test_row.grid(row=5, column=0, columnspan=3, sticky=tk.W, pady=(SPACE, 0))
        self.test_button = ttk.Button(
            test_row, text="测试连接", style="Secondary.TButton", command=self.test_connection
        )
        self.test_button.pack(side=tk.LEFT)
        tk.Label(
            test_row,
            text="可选：先确认能连上 VPS。「一键部署」也会自动测试。不是 root 用户？到「高级选项」填 sudo 密码。",
            bg=COLOR_CARD,
            fg=COLOR_MUTED,
            font=(FONT, 9),
        ).pack(side=tk.LEFT, padx=(SPACE, 0))

    def _build_telegram_step(self, parent: tk.Frame, row: int) -> None:
        _card, body = self._step_card(parent, row, 1)
        self._entry(body, 0, "Bot Token", "bot_token", secret=True, hint=FIELD_HINTS["bot_token"])
        left, right = self._two_columns(body, 1)
        self._entry(left, 0, "API ID", "telegram_api_id", width=16, hint=FIELD_HINTS["telegram_api_id"])
        self._entry(
            right, 0, "API Hash", "telegram_api_hash", secret=True, width=16,
            hint=FIELD_HINTS["telegram_api_hash"],
        )
        self._entry(body, 2, "你的数字 ID", "allowed_user_id", hint=FIELD_HINTS["allowed_user_id"])

    def _build_destination_step(self, parent: tk.Frame, row: int) -> None:
        _card, body = self._step_card(parent, row, 2)
        tk.Label(
            body, text="保存到", bg=COLOR_CARD, fg=COLOR_MUTED, width=LABEL_WIDTH, anchor=tk.W, font=(FONT, 10)
        ).grid(row=0, column=0, sticky=tk.W, pady=3)
        switch = tk.Frame(
            body,
            bg=COLOR_CARD,
            highlightbackground=COLOR_BORDER,
            highlightthickness=1,
            padx=2,
            pady=2,
        )
        switch.grid(row=0, column=1, sticky=tk.W, padx=(SPACE, 0), pady=3)
        self.destination_var = self._var("destination_type", DESTINATION_CLOUDDRIVE2)
        self.destination_buttons: dict[str, tk.Label] = {}
        for index, (value, label) in enumerate(DESTINATION_LABELS.items()):
            chip = tk.Label(
                switch,
                text=label,
                bg=COLOR_CARD,
                fg=COLOR_MUTED,
                font=(FONT, 10, "bold"),
                padx=16,
                pady=6,
                cursor="hand2",
            )
            chip.grid(row=0, column=index)
            chip.bind("<Button-1>", lambda _event, chosen=value: self.destination_var.set(chosen))
            chip.bind("<Return>", lambda _event, chosen=value: self.destination_var.set(chosen))
            chip.bind("<FocusIn>", lambda _event, widget=chip: widget.configure(highlightthickness=2, highlightbackground=COLOR_FOCUS))
            chip.bind("<FocusOut>", lambda _event, widget=chip: widget.configure(highlightthickness=0))
            chip.configure(takefocus=1)
            self.destination_buttons[value] = chip
        self.destination_var.trace_add("write", self._on_destination_change)
        self.destination_help = tk.Label(
            body,
            text="",
            bg=COLOR_CARD,
            fg=COLOR_MUTED,
            justify=tk.LEFT,
            wraplength=900,
            anchor=tk.W,
            font=(FONT, 9),
        )
        self.destination_help.grid(row=1, column=0, columnspan=3, sticky=tk.EW, pady=(0, 4))
        target_holder = tk.Frame(body, bg=COLOR_CARD)
        target_holder.grid(row=2, column=0, columnspan=3, sticky=tk.EW)
        target_holder.columnconfigure(0, weight=1)
        for destination, label, name in (
            (DESTINATION_CLOUDDRIVE2, "115 文件夹", "cd2_target"),
            (DESTINATION_GDRIVE, "Google Drive 文件夹", "gdrive_target"),
        ):
            frame = tk.Frame(target_holder, bg=COLOR_CARD)
            frame.columnconfigure(1, weight=1)
            entry = self._entry(
                frame, 0, label, name, DEFAULT_TARGETS[destination], hint=FIELD_HINTS[name]
            )
            entry.bind(
                "<FocusOut>",
                lambda _event, field=name: self._normalize_target_fields(field),
                add="+",
            )
            self._target_frames[destination] = frame
            self._destination_cards[destination] = frame
        # 是否把 CloudDrive2 装在这台 VPS 上，直接决定 WebDAV 地址怎么填，所以放在地址旁边。
        self.cd2_checkbox_var = self._var("deploy_clouddrive2", "true")
        self.cd2_checkbox = ttk.Checkbutton(
            body,
            text="CloudDrive2 装在这台 VPS 上，由部署器安装和管理（推荐）",
            variable=self.cd2_checkbox_var,
            onvalue="true",
            offvalue="false",
        )
        self.cd2_checkbox.grid(row=3, column=0, columnspan=3, sticky=tk.W, pady=(SPACE, 2))
        self.cd2_url_entry = self._entry(
            body, 4, "WebDAV 地址", "cd2_url", MANAGED_CD2_WEBDAV_URL, hint=CD2_URL_HINT_MANAGED
        )
        self._cd2_url_hint = self._field_rows["cd2_url"][0].grid_slaves(row=2, column=1)[0]
        self._custom_cd2_url = ""
        self.cd2_checkbox_var.trace_add("write", lambda *_args: self._apply_cd2_managed_state())
        self._entry(body, 5, "WebDAV 用户名", "cd2_username", hint=FIELD_HINTS["cd2_username"])
        self._entry(
            body, 6, "WebDAV 密码", "cd2_password", secret=True, hint=FIELD_HINTS["cd2_password"]
        )
        self._apply_cd2_managed_state()

    def _apply_cd2_managed_state(self) -> None:
        """勾选“装在这台 VPS 上”时地址固定为内部地址且不可改；取消勾选后由用户填写。"""
        variable = self.values["cd2_url"]
        managed = self.values["deploy_clouddrive2"].get() == "true"
        current = variable.get().strip()
        if managed:
            if current and current != MANAGED_CD2_WEBDAV_URL:
                self._custom_cd2_url = current
            if current != MANAGED_CD2_WEBDAV_URL:
                variable.set(MANAGED_CD2_WEBDAV_URL)
            self.cd2_url_entry.configure(state="readonly")
            self._cd2_url_hint.configure(text=CD2_URL_HINT_MANAGED)
        else:
            if current == MANAGED_CD2_WEBDAV_URL:
                variable.set(self._custom_cd2_url)
            self.cd2_url_entry.configure(state=tk.NORMAL)
            self._cd2_url_hint.configure(text=CD2_URL_HINT_CUSTOM)

    def _normalize_target_fields(self, *names: str) -> None:
        """把留空或只填网盘根目录的文件夹改成 <网盘>/telegram转存，并显示在输入框里。"""
        for name, destination in (
            ("cd2_target", DESTINATION_CLOUDDRIVE2),
            ("gdrive_target", DESTINATION_GDRIVE),
        ):
            if names and name not in names:
                continue
            variable = self.values.get(name)
            if variable is None:
                continue
            normalized = normalize_drive_folder(destination, variable.get())
            if variable.get() != normalized:
                variable.set(normalized)

    def _build_deploy_step(self, parent: tk.Frame, row: int) -> None:
        """④ 固定在表单下方：一个主按钮，后面两小步部署成功后再点。"""
        card, body = self._step_card(parent, row, 3, grid=False)
        card.configure(pady=SPACE)
        card.grid(row=row, column=0, sticky=tk.EW, pady=(SPACE, 0))
        self._deploy_card = card
        self._deploy_substep_cells: list[tk.Frame] = []
        commands = (self.deploy, self.open_clouddrive, self.verify)
        styles = ("Primary.TButton", "Secondary.TButton", "Secondary.TButton")
        buttons: list[ttk.Button] = []
        for column, ((title, hint), command, style, text) in enumerate(
            zip(DEPLOY_SUBSTEPS, commands, styles, DEPLOY_BUTTON_TEXTS)
        ):
            body.columnconfigure(column, weight=1, uniform="deploy-substep")
            cell = tk.Frame(body, bg=COLOR_CARD)
            cell.grid(row=0, column=column, sticky=tk.NSEW, padx=(0 if column == 0 else SPACE * 2, 0))
            button = ttk.Button(cell, text=text, style=style, command=command)
            button.pack(anchor=tk.W, pady=(0, 2))
            tk.Label(
                cell,
                text=f"{title}：{hint}",
                bg=COLOR_CARD,
                fg=COLOR_MUTED,
                font=(FONT, 8),
                anchor=tk.W,
                justify=tk.LEFT,
                wraplength=280,
            ).pack(anchor=tk.W)
            self._deploy_substep_cells.append(cell)
            buttons.append(button)
        self.deploy_button, self.cloud_button, self.verify_button = buttons

    def _build_advanced_section(self, parent: tk.Frame, row: int) -> None:
        """默认折叠：容量/传输方式、CloudDrive2 托管与地址、服务器细节、排查工具。"""
        card = tk.Frame(
            parent,
            bg=COLOR_CARD,
            highlightbackground=COLOR_BORDER,
            highlightthickness=1,
            padx=SPACE * 2,
            pady=SPACE,
        )
        card.grid(row=row, column=0, sticky=tk.EW)
        self._advanced_card = card
        self._advanced_toggle = ttk.Button(
            card,
            text=ADVANCED_TOGGLE_CLOSED,
            style="Quiet.TButton",
            command=self._toggle_advanced,
        )
        self._advanced_toggle.pack(anchor=tk.W)
        self._advanced_body = tk.Frame(card, bg=COLOR_CARD)
        self._advanced_body.columnconfigure(0, weight=1)
        sections = (
            ("容量与传输方式", "默认已按 1 核 / 1GB / 20GB 的小 VPS 设好。"),
            ("服务器细节", "大多数人不用改。"),
            ("排查工具", MORE_HELP),
        )
        frames = []
        for index, (title, hint) in enumerate(sections):
            section = tk.Frame(self._advanced_body, bg=COLOR_CARD)
            section.grid(row=index, column=0, sticky=tk.EW, pady=(SPACE, 0))
            section.columnconfigure(1, weight=1)
            tk.Label(
                section, text=title, bg=COLOR_CARD, fg=COLOR_HEADING, font=(FONT, 10, "bold"), anchor=tk.W
            ).grid(row=0, column=0, columnspan=3, sticky=tk.W)
            tk.Label(
                section, text=hint, bg=COLOR_CARD, fg=COLOR_MUTED, font=(FONT, 9),
                anchor=tk.W, justify=tk.LEFT, wraplength=900,
            ).grid(row=1, column=0, columnspan=3, sticky=tk.W, pady=(0, 4))
            frames.append(section)
        capacity, server, tools = frames

        tk.Label(
            capacity, text="VPS 配置", bg=COLOR_CARD, fg=COLOR_MUTED, width=LABEL_WIDTH, anchor=tk.W, font=(FONT, 10)
        ).grid(row=2, column=0, sticky=tk.W, pady=3)
        self.vps_profile_var = self._var("vps_profile", VPS_PROFILE_MINIMUM)
        self.vps_profile_label_var = tk.StringVar(
            value=VPS_PROFILE_LABELS[VPS_PROFILE_MINIMUM]
        )
        self.vps_profile_combo = ttk.Combobox(
            capacity,
            textvariable=self.vps_profile_label_var,
            values=tuple(VPS_PROFILE_LABELS[name] for name in VPS_PROFILE_ORDER),
            state="readonly",
        )
        self.vps_profile_combo.grid(row=2, column=1, sticky=tk.EW, padx=(SPACE, 0), pady=3)
        self.vps_profile_hint = tk.Label(
            capacity,
            text="",
            bg=COLOR_CARD,
            fg=COLOR_MUTED,
            justify=tk.LEFT,
            wraplength=420,
            font=(FONT, 9),
        )
        self.vps_profile_hint.grid(row=3, column=1, columnspan=2, sticky=tk.W, padx=(SPACE, 0), pady=(0, 4))
        self.vps_profile_label_var.trace_add("write", self._on_vps_profile_change)
        self._update_vps_profile_hint(VPS_PROFILE_MINIMUM)
        self._entry(capacity, 4, "本地任务预算 GB", "local_budget_gb", "2", width=10)
        self._entry(capacity, 5, "磁盘最少保留 GB", "min_free_disk_gb", "6", width=10)
        mode_row = tk.Frame(capacity, bg=COLOR_CARD)
        mode_row.grid(row=6, column=0, columnspan=3, sticky=tk.EW, pady=3)
        tk.Label(
            mode_row,
            text="传输方式",
            bg=COLOR_CARD,
            fg=COLOR_MUTED,
            width=LABEL_WIDTH,
            anchor=tk.W,
            font=(FONT, 10),
        ).grid(row=0, column=0, sticky=tk.NW)
        self.transfer_mode_var = self._var("transfer_mode", TRANSFER_MODE_AUTO)
        choices = tk.Frame(mode_row, bg=COLOR_CARD)
        choices.grid(row=0, column=1, sticky=tk.W, padx=(SPACE, 0))
        self.transfer_mode_buttons = {}
        for index, (value, text) in enumerate(TRANSFER_MODE_LABELS.items()):
            button = ttk.Radiobutton(
                choices, text=text, value=value, variable=self.transfer_mode_var
            )
            button.grid(row=index, column=0, sticky=tk.W)
            self.transfer_mode_buttons[value] = button

        self._entry(server, 2, "sudo 密码", "sudo_password", secret=True, hint="用户名不是 root 时填写；root 留空。")
        self._entry(server, 3, "安装目录", "install_dir", "/opt/tg115")
        self._entry(server, 4, "时区", "timezone", "Asia/Shanghai")

        self._more_actions_row = tk.Frame(tools, bg=COLOR_CARD)
        self._more_actions_row.grid(row=2, column=0, columnspan=3, sticky=tk.W, pady=(0, SPACE))
        self.repair_button = ttk.Button(
            self._more_actions_row, text="修复网络", style="Quiet.TButton", command=self.repair_clouddrive
        )
        self.repair_button.pack(side=tk.LEFT)
        self.status_button = ttk.Button(
            self._more_actions_row,
            text="查看 VPS 状态",
            style="Quiet.TButton",
            command=self.check_status,
        )
        self.clear_button = ttk.Button(
            self._more_actions_row, text="清空日志", style="Quiet.TButton", command=self._clear_log
        )
        self.clear_button.pack(side=tk.LEFT, padx=(SPACE, 0))

    def _toggle_advanced(self, open_now: bool | None = None) -> None:
        if open_now is None:
            open_now = not bool(self._advanced_open.get())
        self._advanced_open.set(open_now)
        if open_now:
            self._advanced_body.pack(fill=tk.X, pady=(0, SPACE))
            self._advanced_toggle.configure(text=ADVANCED_TOGGLE_OPEN)
        else:
            self._advanced_body.pack_forget()
            self._advanced_toggle.configure(text=ADVANCED_TOGGLE_CLOSED)

    def _reveal_field(self, name: str) -> None:
        """出错时把对应输入框展示出来：必要时展开高级选项，滚动过去并聚焦。"""
        if name == "cd2_target":
            name = DESTINATION_TARGET_FIELDS[self._current_destination()]
        row = self._field_rows.get(name)
        if row is None:
            return
        if name in ADVANCED_FIELDS and not self._advanced_open.get():
            self._toggle_advanced(True)
        try:
            self.root.update_idletasks()
            body = self.form.body
            total = max(body.winfo_height(), 1)
            offset = row[0].winfo_rooty() - body.winfo_rooty()
            # 尽量让整张步骤卡片的标题也露出来，用户知道自己在第几步。
            card: tk.Misc | None = row[0]
            anchors = {str(widget) for widget in [*self._step_cards, self._advanced_card]}
            while card is not None and str(card) not in anchors:
                card = card.master
            top = offset
            if card is not None:
                card_top = card.winfo_rooty() - body.winfo_rooty()
                visible = max(self.form.canvas.winfo_height(), 1)
                top = card_top if offset - card_top < visible * 0.6 else offset - 60
            self.form.canvas.yview_moveto(max(0.0, (top - 4) / total))
        except tk.TclError:
            return

    @staticmethod
    def _next_step_for_error(message: str) -> tuple[str, str]:
        """把错误翻译成“去哪一步改什么”：返回 (字段名, 下一步提示)。"""
        field = InstallerApp._field_for_error(message)
        if field:
            step, label = FIELD_LOCATIONS[field]
            return field, f"下一步：到「{step}」改好“{label}”，再点刚才的按钮。"
        lowered = message.lower()
        if any(marker in lowered for marker in ("authentication", "认证失败", "permission denied")):
            return "", (
                "下一步：到「① 连接服务器」核对用户名和密码（或私钥），"
                "再点「测试连接」。"
            )
        if any(
            marker in lowered
            for marker in ("timed out", "timeout", "超时", "unable to connect", "connection refused", "no route", "getaddrinfo")
        ):
            return "", (
                "下一步：到「① 连接服务器」核对 VPS 地址和 SSH 端口，确认 VPS 已开机，"
                "再点「测试连接」。"
            )
        return "", "下一步：看下方「部署记录」最后几行的提示，改好后再点刚才的按钮。"

    def _adopt_status_button(self) -> None:
        """状态 Agent 的“检查 VPS 状态”按钮并入折叠排障区，不占第 4 个主按钮。"""
        button = getattr(self, "status_button", None)
        row = getattr(self, "_more_actions_row", None)
        if button is None or row is None:
            return
        try:
            parent = str(button.winfo_parent())
        except tk.TclError:
            return
        if parent != str(row) or button.winfo_manager():
            return
        button.pack(side=tk.LEFT, padx=(SPACE, 0))

    def _clear_log(self) -> None:
        # 连待渲染缓冲一起清掉，否则清空后旧积压会继续刷回来。
        with self._log_lock:
            self._log_pending.clear()
            self._log_pending_dropped = 0
        self.log.delete("1.0", tk.END)
        self._status_text.set("日志已清空")

    def _card(
        self, parent: tk.Frame, row: int, title: str, hint: str = ""
    ) -> tuple[tk.Frame, tk.Frame]:
        card = tk.Frame(
            parent,
            bg=COLOR_CARD,
            highlightbackground=COLOR_BORDER,
            highlightthickness=1,
            padx=SPACE * 2,
            pady=SPACE * 2,
        )
        card.grid(row=row, column=0, sticky=tk.EW, pady=(0, SPACE))
        tk.Label(
            card,
            text=title,
            bg=COLOR_CARD,
            fg=COLOR_HEADING,
            font=(FONT, 12, "bold"),
        ).pack(anchor=tk.W)
        body = tk.Frame(card, bg=COLOR_CARD)
        body.pack(fill=tk.X, pady=(SPACE, 0))
        body.columnconfigure(1, weight=1)
        if hint:
            tk.Label(
                card,
                text=hint,
                bg=COLOR_CARD,
                fg=COLOR_MUTED,
                justify=tk.LEFT,
                wraplength=980,
                anchor=tk.W,
                font=(FONT, 9),
            ).pack(fill=tk.X, pady=(SPACE, 0))
        return card, body

    def _entry(
        self,
        parent: tk.Frame,
        row: int,
        label: str,
        name: str,
        default: str = "",
        *,
        secret: bool = False,
        width: int = 42,
        hint: str = "",
    ) -> ttk.Entry:
        row_frame = tk.Frame(parent, bg=COLOR_CARD)
        row_frame.grid(row=row, column=0, columnspan=3, sticky=tk.EW, pady=3)
        row_frame.columnconfigure(1, weight=1)
        caption = tk.Label(
            row_frame,
            text=label,
            bg=COLOR_CARD,
            fg=COLOR_MUTED,
            width=LABEL_WIDTH,
            anchor=tk.W,
            font=(FONT, 10),
        )
        caption.grid(row=0, column=0, sticky=tk.W)
        entry = ttk.Entry(
            row_frame,
            textvariable=self._var(name, default),
            width=width,
            show="●" if secret else "",
        )
        entry.grid(row=0, column=1, sticky=tk.EW, padx=(SPACE, 0))
        message = tk.Label(
            row_frame,
            text="",
            bg=COLOR_CARD,
            fg=COLOR_ERROR,
            anchor=tk.W,
            font=(FONT, 8),
        )
        message.grid(row=1, column=1, sticky=tk.W, padx=(SPACE, 0))
        # 没有错误时不占高度；出错时再显示在输入框正下方。
        message.grid_remove()
        if hint:
            tk.Label(
                row_frame,
                text=hint,
                bg=COLOR_CARD,
                fg=COLOR_MUTED,
                anchor=tk.W,
                justify=tk.LEFT,
                font=(FONT, 8),
                wraplength=640,
            ).grid(row=2, column=1, columnspan=2, sticky=tk.W, padx=(SPACE, 0))
        self._field_rows[name] = (row_frame, message)
        self._field_entries[name] = entry
        entry.bind("<FocusOut>", lambda _event, field=name: self._validate_field(field))
        entry.bind("<FocusIn>", lambda _event, field=name: self._clear_field_error(field))
        return entry

    def _clear_field_error(self, name: str) -> None:
        row = self._field_rows.get(name)
        if row is not None:
            row[1].configure(text="")
            row[1].grid_remove()

    def _mark_field_error(self, message: str) -> None:
        field = self._field_for_error(message)
        row = self._field_rows.get(field) if field else None
        if row is not None:
            row[1].configure(text=message)
            row[1].grid()

    @staticmethod
    def _field_for_error(message: str) -> str:
        pairs = (
            ("VPS IP", "vps_host"),
            ("VPS 地址", "vps_host"),
            ("SSH 端口", "vps_port"),
            ("SSH 用户名", "vps_user"),
            ("VPS 登录密码", "vps_password"),
            ("SSH 私钥", "ssh_key_path"),
            ("sudo", "sudo_password"),
            ("Bot Token", "bot_token"),
            ("API ID", "telegram_api_id"),
            ("API Hash", "telegram_api_hash"),
            ("数字 ID", "allowed_user_id"),
            ("WebDAV 地址", "cd2_url"),
            ("WebDAV 用户名", "cd2_username"),
            ("WebDAV 密码", "cd2_password"),
            ("目标路径", "cd2_target"),
            ("安装目录", "install_dir"),
            ("磁盘预算", "local_budget_gb"),
            ("安全线", "min_free_disk_gb"),
            ("时区", "timezone"),
            ("VPS 配置", "vps_profile"),
        )
        return next((name for marker, name in pairs if marker in message), "")

    def _validate_field(self, name: str) -> None:
        values = {key: variable.get() for key, variable in self.values.items()}
        try:
            if name in {"vps_host", "vps_port", "vps_user", "vps_password", "ssh_key_path", "sudo_password"}:
                if values.get(name, "").strip():
                    self._validate_connection(values)
            elif name in {"bot_token", "telegram_api_id", "telegram_api_hash", "allowed_user_id"}:
                if values.get(name, "").strip():
                    self._validate_telegram(values)
            elif name in {"cd2_url", "cd2_username", "cd2_password", "cd2_target", "gdrive_target"}:
                if values.get(name, "").strip() or name in {"cd2_target", "gdrive_target"}:
                    self._validate_clouddrive2(values)
            elif name in {"install_dir", "local_budget_gb", "min_free_disk_gb", "timezone"}:
                self._validate_common(values)
        except ValueError as exc:
            message = str(exc)
            if self._field_for_error(message) in {"", name}:
                self._mark_field_error(message)
            return
        self._clear_field_error(name)

    def _hint(self, parent: tk.Frame, row: int, text: str) -> tk.Label:
        label = tk.Label(
            parent,
            text=text,
            bg=COLOR_CARD,
            fg=COLOR_MUTED,
            justify=tk.LEFT,
            wraplength=920,
            font=(FONT, 9),
        )
        label.grid(row=row, column=0, columnspan=3, sticky=tk.W, pady=(SPACE, 0))
        return label

    def _apply_auth_visibility(self) -> None:
        method = self.values.get("auth_method")
        uses_key = method is not None and method.get() == "SSH 密钥"
        password_row = self._field_rows.get("vps_password")
        key_panel = getattr(self, "_key_panel", None)
        if uses_key:
            if password_row is not None:
                password_row[0].grid_remove()
            if key_panel is not None:
                key_panel.grid()
        else:
            if key_panel is not None:
                key_panel.grid_remove()
            if password_row is not None:
                password_row[0].grid()

    def _on_vps_profile_change(self, *_args: object) -> None:
        profile = self._profile_from_label(self.vps_profile_label_var.get())
        self._apply_vps_profile(profile)

    @staticmethod
    def _profile_from_label(label: str) -> str:
        for name in VPS_PROFILE_ORDER:
            if VPS_PROFILE_LABELS[name] == label:
                return name
        return VPS_PROFILE_MINIMUM

    def _apply_vps_profile(self, profile: str) -> None:
        """切换预设时写入预算、保留值和内存阈值；之后用户仍可手工改数字。"""
        preset = VPS_PROFILE_PRESETS[profile]
        variable = self.values.get("vps_profile")
        if variable is not None:
            variable.set(profile)
        for name in ("local_budget_gb", "min_free_disk_gb"):
            target = self.values.get(name)
            if target is not None:
                target.set(preset[name])
        self._update_vps_profile_hint(profile)

    def _update_vps_profile_hint(self, profile: str) -> None:
        hint = getattr(self, "vps_profile_hint", None)
        if hint is None:
            return
        hint.configure(text=VPS_PROFILE_SPECS[profile], wraplength=520)

    def _current_destination(self) -> str:
        variable = self.values.get("destination_type")
        raw = variable.get().strip().lower() if variable is not None else ""
        if raw in DESTINATION_LABELS:
            return raw
        return DESTINATION_CLOUDDRIVE2

    def _on_destination_change(self, *_args: object) -> None:
        self._apply_destination_state()

    def _apply_destination_state(self) -> None:
        destination = self._current_destination()
        self._applied_destination = destination
        for name, frame in self._target_frames.items():
            if name == destination:
                frame.grid(row=0, column=0, columnspan=3, sticky=tk.EW)
            else:
                frame.grid_remove()
        self.cd2_checkbox.configure(state=tk.NORMAL)
        self.destination_help.configure(text=DESTINATION_DESCRIPTIONS[destination])
        for value, chip in getattr(self, "destination_buttons", {}).items():
            selected = value == destination
            chip.configure(
                bg=COLOR_HEADING if selected else COLOR_CARD,
                fg="#FFFFFF" if selected else COLOR_MUTED,
            )
        self.badge.configure(
            text=f"当前目标：{DESTINATION_LABELS[destination]}",
            bg=COLOR_BADGE_GDRIVE if destination == DESTINATION_GDRIVE else COLOR_BADGE_CD2,
        )
        self._update_buttons()

    def _update_buttons(self) -> None:
        destination = self._current_destination()
        idle = tk.DISABLED if self.busy else tk.NORMAL
        buttons = [
            self.test_button,
            self.deploy_button,
            self.verify_button,
            self.clear_button,
            self.cloud_button,
            self.repair_button,
        ]
        # 状态 Agent 的按钮属于折叠排障区；存在时一起跟随 busy 状态。
        status_button = getattr(self, "status_button", None)
        if status_button is not None:
            buttons.append(status_button)
        for button in buttons:
            button.configure(state=idle)
        # 主按钮文案保持原样，步骤含义放在上方的步骤标签里。
        self.deploy_button.configure(
            text=f"一键部署（{DESTINATION_LABELS[destination]}）"
        )
        self.verify_button.configure(text="检查是否可用")
        self.cloud_button.configure(text="登录并挂载网盘")
        if self.busy:
            if not getattr(self, "_status_busy_placeholder", False):
                self._status_busy_placeholder = True
                self._status_text.set(STATUS_BUSY)
            self.progress.start(12)
        else:
            self.progress.stop()
            # 只收回本方法写入的占位状态；_show_info/_show_error 等设置的精确状态保持不动。
            if (
                getattr(self, "_status_busy_placeholder", False)
                and self._status_text.get() == STATUS_BUSY
            ):
                self._status_text.set(STATUS_IDLE)
            self._status_busy_placeholder = False

    def _choose_key(self, entry: ttk.Entry) -> None:
        path = filedialog.askopenfilename(
            title="选择 SSH 私钥",
            filetypes=[("SSH private key", "*"), ("All files", "*.*")],
        )
        if path:
            self.values["ssh_key_path"].set(path)
            entry.xview_moveto(1)

    def _snapshot(self) -> dict[str, str]:
        # 部署器界面不允许保存到网盘根目录：留空或只填网盘名时改用 <网盘>/telegram转存。
        self._normalize_target_fields()
        values = {name: variable.get() for name, variable in self.values.items()}
        # 在 Tk 主线程读取一次密钥值，后台线程只使用这份纯字符串副本做日志遮蔽。
        secrets = [
            values[name] for name in SECRET_FIELDS if len(values.get(name, "")) >= 4
        ]
        secrets.extend(b64(secret) for secret in list(secrets))
        self._secrets = [secret for secret in secrets if secret]
        # sudo 密码经 PTY 发送，可能被远端回显成单独一行，所以不论长短都要遮蔽。
        # 少于 4 个字符时只遮蔽独立出现的整词，避免把日志里的普通字符全部替换掉。
        sudo_password = values.get("sudo_password", "")
        self._token_secrets = (
            [sudo_password] if 0 < len(sudo_password) < 4 else []
        )
        return values

    def _redact(self, text: str) -> str:
        result = str(text)
        for secret in self._secrets:
            if len(secret) >= 4 and secret in result:
                result = result.replace(secret, "***")
        for secret in getattr(self, "_token_secrets", ()):
            if secret and secret in result:
                result = re.sub(
                    rf"(?<!\S){re.escape(secret)}(?!\S)", "***", result
                )
        return result

    def _validate(self, values: dict[str, str]) -> None:
        destination_type(values)
        transfer_mode_value(values)
        self._validate_connection(values)
        self._validate_telegram(values)
        # 两个分支都经 CloudDrive2 WebDAV：凭据与地址约束完全一致。
        self._validate_clouddrive2(values)
        self._validate_common(values)

    def _validate_telegram(self, values: dict[str, str]) -> None:
        required = {
            # These are user-facing labels, not embedded credentials.
            "bot_token": "Bot Token",  # nosec B105
            "telegram_api_id": "Telegram API ID",
            "telegram_api_hash": "Telegram API Hash",
            "allowed_user_id": "Telegram 数字 ID",
        }
        for name, label in required.items():
            if not values.get(name, "").strip():
                raise ValueError(f"请填写：{label}")
        try:
            api_id = int(values["telegram_api_id"])
            user_id = int(values["allowed_user_id"])
        except ValueError as exc:
            raise ValueError("端口、API ID、数字 ID 和磁盘数值必须是数字") from exc
        if api_id <= 0 or user_id <= 0:
            raise ValueError("API ID 和 Telegram 数字 ID 必须大于 0")
        token = values["bot_token"].strip()
        if ":" not in token or len(token) < 25:
            raise ValueError("Bot Token 格式看起来不正确")
        api_hash = values["telegram_api_hash"].strip()
        if len(api_hash) != 32 or any(
            c not in "0123456789abcdefABCDEF" for c in api_hash
        ):
            raise ValueError("Telegram API Hash 应为 32 位十六进制字符")

    def _validate_clouddrive2(self, values: dict[str, str]) -> None:
        required = {
            "cd2_url": "CloudDrive2 WebDAV 地址",
            "cd2_username": "WebDAV 用户名",
            "cd2_password": "WebDAV 密码",  # nosec B105  # pragma: allowlist secret
        }
        for name, label in required.items():
            if not values.get(name, "").strip():
                raise ValueError(f"请填写：{label}")
        url = self._effective_webdav_url(values)
        if not url.startswith(("http://", "https://")):
            raise ValueError("WebDAV 地址必须以 http:// 或 https:// 开头")
        parsed_url = urlsplit(url)
        if not parsed_url.hostname:
            raise ValueError("WebDAV 地址缺少有效主机名")
        if parsed_url.username or parsed_url.password:
            raise ValueError("WebDAV 用户名和密码应填写在专用输入框，不能放在地址中")
        try:
            _ = parsed_url.port
        except ValueError as exc:
            raise ValueError("WebDAV 地址端口格式不正确") from exc
        if parsed_url.query or parsed_url.fragment:
            raise ValueError("WebDAV 地址不能包含查询参数或片段")
        if any(ord(char) < 32 or char.isspace() for char in url):
            raise ValueError("WebDAV 地址不能包含空格或控制字符")
        if parsed_url.scheme == "http":
            host = parsed_url.hostname.lower()
            try:
                address = ipaddress.ip_address(host)
                private_http = (
                    address.is_private or address.is_loopback or address.is_link_local
                )
            except ValueError:
                private_http = (
                    "." not in host
                    or host == "localhost"
                    or host.endswith((".local", ".internal"))
                )
            if not private_http:
                raise ValueError("公网 WebDAV 地址必须使用 HTTPS，避免密码明文传输")
        target = destination_target(values)
        label = DESTINATION_LABELS[destination_type(values)]
        if any(ord(char) < 32 for char in target) or "\\" in target:
            raise ValueError(f"{label} 目标路径不能包含控制字符或反斜杠")
        if ".." in target.split("/"):
            raise ValueError(f"{label} 目标路径不能包含 ..")

    def _validate_common(self, values: dict[str, str]) -> None:
        # 预设本身只决定界面默认值；这里只拒绝未知的预设名。
        vps_profile_name(values)
        try:
            budget = float(values["local_budget_gb"])
            reserve = float(values["min_free_disk_gb"])
        except ValueError as exc:
            raise ValueError("端口、API ID、数字 ID 和磁盘数值必须是数字") from exc
        if not math.isfinite(budget) or not math.isfinite(reserve):
            raise ValueError("磁盘预算与安全线必须是有限数字")
        if budget <= 0 or reserve <= 0:
            raise ValueError("磁盘预算与安全线必须大于 0")
        if not re.fullmatch(
            r"/opt/[A-Za-z0-9._-]+(?:/[A-Za-z0-9._-]+)*",
            values["install_dir"],
        ):
            raise ValueError("安装目录只能是 /opt/ 下不含空格的安全路径")
        if any(
            part in {".", ".."} for part in values["install_dir"].split("/")
        ):
            raise ValueError("安装目录不能包含 . 或 ..")
        if not re.fullmatch(r"[A-Za-z0-9_+./-]+", values["timezone"]):
            raise ValueError("时区格式不正确")
        if (
            values["timezone"].startswith("/")
            or any(part in {".", ".."} for part in values["timezone"].split("/"))
        ):
            raise ValueError("时区格式不正确")

    @staticmethod
    def _effective_webdav_url(values: dict[str, str]) -> str:
        return effective_webdav_url(values)

    def _validate_connection(self, values: dict[str, str]) -> None:
        for name, label in (
            ("vps_host", "VPS IP 或域名"),
            ("vps_port", "SSH 端口"),
            ("vps_user", "SSH 用户名"),
        ):
            if not values.get(name, "").strip():
                raise ValueError(f"请填写：{label}")
        if any(
            ord(char) < 32 or char.isspace()
            for char in values["vps_host"].strip()
        ):
            raise ValueError("VPS IP 或域名不能包含空格或控制字符")
        try:
            port = int(values["vps_port"])
        except ValueError as exc:
            raise ValueError("SSH 端口必须是数字") from exc
        if not (1 <= port <= 65535):
            raise ValueError("SSH 端口必须在 1–65535 之间")
        if any(char in values["vps_user"] for char in "\r\n"):
            raise ValueError("SSH 用户名格式不正确")
        if any(char in values.get("sudo_password", "") for char in "\r\n"):
            raise ValueError("sudo 密码不能包含换行符")
        if values["auth_method"] == "密码":
            if not values["vps_password"]:
                raise ValueError("密码登录方式必须填写 VPS 登录密码")
        else:
            key_path = Path(values["ssh_key_path"])
            if not key_path.is_file():
                raise ValueError("SSH 私钥文件不存在")

    def _log(self, text: str, level: str = "") -> None:
        message = self._redact(strip_terminal_controls(str(text))).rstrip()
        if len(message) > LOG_MAX_LINE_CHARS:
            dropped = len(message) - LOG_MAX_LINE_CHARS
            message = f"{message[:LOG_MAX_LINE_CHARS]}{LOG_TRUNCATED_SUFFIX}，+{dropped} 字符）"
        tags = (level,) if level in {"ok", "warn", "error", "info"} else ()
        with self._log_lock:
            if len(self._log_pending) >= LOG_PENDING_MAX_LINES:
                # 缓冲也满：丢弃最旧的行，保证内存和主线程工作量都有界。
                overflow = len(self._log_pending) - LOG_PENDING_MAX_LINES + 1
                del self._log_pending[:overflow]
                self._log_pending_dropped += overflow
            self._log_pending.append((message, tags))
        if threading.current_thread() is threading.main_thread():
            # 主线程：单条日志立刻渲染（保持“可立即读取”的契约）；
            # 成批输出用掉额度后只排队，交给事件循环按批渲染。
            # 事件循环已经停止（例如窗口正在关闭）时没有别人会来渲染，直接刷。
            if self._main_log_tokens > 0 or self._drain_job is None:
                self._main_log_tokens = max(0, self._main_log_tokens - 1)
                self._flush_log_pending()
            else:
                self._schedule_log_flush()
        else:
            # 后台线程不直接触碰 Tk，只把“该刷日志了”这一个回调交给队列。
            self._request_log_flush()

    def _request_log_flush(self) -> None:
        """后台线程调用：只排一个刷新回调，避免上万行各占一个队列项。"""
        with self._log_lock:
            if self._log_flush_queued:
                return
            self._log_flush_queued = True
        self._ui_queue.put(self._flush_log_pending)

    def _schedule_log_flush(self) -> None:
        """主线程额度用完时，用一次 after 让事件循环先处理其它回调。"""
        if self._log_flush_job is not None:
            return
        try:
            self._log_flush_job = self.root.after(1, self._flush_log_pending)
        except tk.TclError:
            self._log_flush_job = None

    def _pending_log_count(self) -> int:
        with self._log_lock:
            return len(self._log_pending)

    def _flush_log_pending(self) -> None:
        """主线程按批渲染日志：一批一次 insert/see，超出预算就留给下一批。"""
        with self._log_lock:
            self._log_flush_queued = False
            job, self._log_flush_job = self._log_flush_job, None
            if job is not None:
                try:
                    self.root.after_cancel(job)
                except tk.TclError:
                    pass
            batch = self._log_pending[:LOG_FLUSH_MAX_LINES]
            del self._log_pending[:LOG_FLUSH_MAX_LINES]
            dropped, self._log_pending_dropped = self._log_pending_dropped, 0
        if dropped:
            batch.insert(0, (f"…… 日志过快，已省略 {dropped} 行 ……", ("warn",)))
        if not batch:
            return
        deadline = time.monotonic() + LOG_FLUSH_BUDGET_SECONDS
        buffer_text = ""
        buffer_tags: tuple[str, ...] = ()
        consumed = 0
        try:
            for message, tags in batch:
                if consumed and time.monotonic() > deadline:
                    break
                if buffer_text and tags != buffer_tags:
                    self.log.insert(tk.END, buffer_text, buffer_tags)
                    buffer_text = ""
                buffer_tags = tags
                buffer_text += message + "\n"
                consumed += 1
            if buffer_text:
                self.log.insert(tk.END, buffer_text, buffer_tags)
            # 不用 see(tk.END)：它会在回调里同步布局整段文本（长行可达数秒，
            # 正是 py-spy 抓到的卡死帧）。yview_moveto(1.0) 只滚动到末尾。
            self.log.yview_moveto(1.0)
        except tk.TclError:
            # 窗口已经销毁，丢弃这批日志即可。
            return
        self._trim_log_history()
        rest = batch[consumed:]
        with self._log_lock:
            if rest:
                # 时间预算用完：没渲染的行放回缓冲最前面，下一批继续。
                self._log_pending[:0] = rest
            remaining = bool(self._log_pending)
        if remaining:
            try:
                self._log_flush_job = self.root.after(1, self._flush_log_pending)
            except tk.TclError:
                self._log_flush_job = None

    def _trim_log_history(self) -> None:
        """可见历史有界：超出上限从最旧的行开始删。"""
        try:
            total = int(self.log.index("end-1c").split(".")[0])
            if total > LOG_HISTORY_MAX_LINES:
                self.log.delete("1.0", f"{total - LOG_HISTORY_MAX_LINES + 1}.0")
        except (tk.TclError, ValueError):
            return

    def _set_busy(self, busy: bool) -> None:
        self.busy = busy
        self._post(self._update_buttons)

    def _ask_host_key(self, host: str, key_type: str, fingerprint: str) -> bool:
        result: dict[str, bool] = {}
        done = threading.Event()

        def ask() -> None:
            result["approved"] = messagebox.askyesno(
                "确认 VPS 主机密钥",
                (
                    f"这是首次连接 {host}。\n\n"
                    f"类型：{key_type}\n"
                    f"指纹：{fingerprint}\n\n"
                    "请与 VPS 服务商控制台显示的指纹核对。确认一致后点击“是”。"
                ),
            )
            done.set()

        self._post(ask)
        done.wait()
        return result.get("approved", False)

    def _show_error(self, title: str, exc: Exception) -> None:
        message = self._redact(str(exc))
        field, next_step = self._next_step_for_error(message)
        shown = message if "下一步" in message else f"{message}\n\n{next_step}"

        def show() -> None:
            # 先保存失败结果；恢复按钮时只撤销忙碌占位文字，保留这个结果。
            self._status_text.set("操作失败")
            self._log(f"[失败] {message}", "error")
            if "下一步" not in message:
                self._log(next_step, "warn")
            if field:
                # 先滚动并聚焦（聚焦会清掉旧提示），再显示这次的错误。
                self._reveal_field(field)
                self._mark_field_error(message)
            messagebox.showerror(title, shown)

        self._post(show)

    def _show_info(self, title: str, message: str) -> None:
        def show() -> None:
            # 成功时状态栏显示这次操作的结果（如“验收通过”），
            # 而不是笼统的“已完成”，避免把“只修好网络”当成“网盘可用”。
            self._status_text.set(title)
            messagebox.showinfo(title, message)

        self._post(show)

    def _run_worker(self, function: Callable[[], None]) -> None:
        if self.busy:
            return
        self._set_busy(True)

        def worker() -> None:
            try:
                function()
            except Exception as exc:  # noqa: BLE001 - GUI worker boundary
                message = self._redact(str(exc))
                try:
                    self._show_error("操作失败", exc)
                except Exception:  # noqa: BLE001 - 窗口可能已销毁
                    # 弹窗失败也必须留下失败状态，不能被 finally 改写成“已完成”。
                    self._post(lambda: self._status_text.set("操作失败"))
                # 字段提示只能在主线程改控件，这里交回事件循环。
                self._post(lambda: self._mark_field_error(message))
            finally:
                # 无论成功失败都恢复按钮，用户可以直接重试或换下一步。
                # 交回主线程执行：排在上面已经排队的失败弹窗/状态栏回调之后，
                # 避免结果还没显示出来，用户就能再次点击启动新的操作。
                self._post(lambda: self._set_busy(False))

        threading.Thread(target=worker, daemon=True).start()

    def _new_session(self, values: dict[str, str]) -> RemoteSession:
        session = RemoteSession(values, self._ask_host_key)
        session.connect()
        return session

    def test_connection(self) -> None:
        values = self._snapshot()

        def action() -> None:
            self._validate_connection(values)
            self._log("正在连接 VPS……")
            session = self._new_session(values)
            try:
                code, output = session.run(
                    "printf 'SSH_OK\\n'; uname -a; id; "
                    "awk -F= '/^(ID|VERSION_ID)=/ {print}' /etc/os-release; "
                    "free -h; df -h /"
                )
                if code != 0 or "SSH_OK" not in output:
                    raise RuntimeError("SSH 已连接，但预检命令失败")
                for line in output.splitlines():
                    self._log(line)
                self._log("SSH 测试成功。")
                self._show_info("测试成功", "SSH 连接和基础预检正常。")
            finally:
                session.close()

        self._run_worker(action)

    @staticmethod
    def _status_command(install_dir: str) -> str:
        # 只读：容器列表、State 的四个字段和固定条数日志。
        # 绝不 inspect 整个 Config（会带出 Env 里的凭据）。
        inspect = (
            "docker inspect --format "
            "'{{.State.Status}}|{{.State.StartedAt}}|"
            "{{.RestartCount}}|{{.State.ExitCode}}' "
            "tg115-bot 2>&1"
        )
        return (
            f"cd {shlex.quote(install_dir)} || "
            "{ printf 'TG115_CD_FAILED\\n'; exit 0; }; "
            "printf 'TG115_PS_BEGIN\\n'; docker compose ps 2>&1; "
            "printf 'TG115_PS_END\\n'; "
            f"printf 'TG115_INSPECT_BEGIN\\n'; {inspect}; "
            "printf 'TG115_INSPECT_END\\n'; "
            "printf 'TG115_LOGS_BEGIN\\n'; "
            "docker compose logs --tail=30 tg115-bot 2>&1; "
            "printf 'TG115_LOGS_END\\n'; exit 0"
        )

    @staticmethod
    def _status_section(output: str, name: str) -> str:
        begin = f"TG115_{name}_BEGIN"
        end = f"TG115_{name}_END"
        text = str(output or "")
        if begin not in text or end not in text:
            return ""
        return text.split(begin, 1)[1].split(end, 1)[0].strip()

    @classmethod
    def _classify_status(cls, output: str) -> dict[str, str]:
        text = str(output or "")
        info = {
            "state": STATUS_UNKNOWN,
            "status": "",
            "started_at": "",
            "restarts": "",
            "exit_code": "",
            "logs": "",
        }
        if "TG115_CD_FAILED" in text:
            info["state"] = STATUS_NOT_INSTALLED
            return info
        ps = cls._status_section(text, "PS")
        inspect = cls._status_section(text, "INSPECT")
        info["logs"] = cls._status_section(text, "LOGS")
        combined = f"{ps}\n{inspect}".lower()
        if any(
            marker in combined
            for marker in (
                "cannot connect to the docker daemon",
                "is the docker daemon running",
                "permission denied",
            )
        ):
            info["reason"] = "Docker 服务不可用或无读取权限，暂不能判断 Bot 是否运行"
            return info
        if any(
            marker in combined
            for marker in (
                "no configuration file",
                "docker: not found",
                "command not found",
            )
        ):
            # 缺少程序或配置；不同于已安装但 Docker 暂时不可用。
            info["state"] = STATUS_NOT_INSTALLED
            return info
        match = re.search(
            r"(running|exited|created|paused|dead|restarting)"
            r"\|([^|\n]*)\|(\d+)\|(-?\d+)",
            inspect,
        )
        if match:
            status, started, restarts, exit_code = match.groups()
            info.update(
                status=status,
                started_at=started.strip(),
                restarts=restarts,
                exit_code=exit_code,
            )
            # 只有 State.Status=running 才算运行；docker ps 的 Up 不足以证明。
            info["state"] = STATUS_RUNNING if status == "running" else STATUS_STOPPED
            return info
        if "no such" in combined:
            info["state"] = STATUS_MISSING
            return info
        return info

    @staticmethod
    def _status_message(destination: str, info: Mapping[str, str]) -> str:
        label = DESTINATION_LABELS.get(destination, destination)
        state = info.get("state", STATUS_UNKNOWN)
        if state == STATUS_RUNNING:
            detail = (
                "Bot 正在运行（容器状态 running，"
                f"启动于 {info.get('started_at') or '未知'}，"
                f"重启 {info.get('restarts') or '0'} 次）"
            )
        elif state == STATUS_STOPPED:
            detail = (
                f"Bot 已停止（容器状态 {info.get('status') or '未知'}，"
                f"退出码 {info.get('exit_code') or '未知'}，"
                f"重启 {info.get('restarts') or '0'} 次）"
            )
        elif state == STATUS_MISSING:
            detail = "Bot 容器不存在：VPS 上可能还没有完成部署"
        elif state == STATUS_NOT_INSTALLED:
            detail = "安装仍未完成：找不到安装目录、Compose 配置或 Docker 服务"
        else:
            detail = info.get("reason") or "状态未知：无法读取容器 State.Status，不能据此判断服务是否运行"
        return (
            f"{detail}。目标网盘：{label}。\n"
            "这是只读运行状态检查，不代表 WebDAV 或最终网盘已入库；"
            "第三步可检查网盘读写；最终入库请在网盘官方端确认。"
        )

    def check_status(self) -> None:
        values = self._snapshot()

        def action() -> None:
            destination = destination_type(values)
            label = DESTINATION_LABELS[destination]
            self._validate_connection(values)
            install_dir = values.get("install_dir", "").strip()
            if not re.fullmatch(
                r"/opt/[A-Za-z0-9._-]+(?:/[A-Za-z0-9._-]+)*", install_dir
            ) or any(part in {".", ".."} for part in install_dir.split("/")):
                raise ValueError("安装目录只能是 /opt/ 下不含空格的安全路径")
            self._log(
                f"[状态] 只读检查目标网盘 {label} 的 VPS 运行状态；"
                "不会写入、启动、重启或修改任何服务，"
                "也不代表 WebDAV 或最终网盘已入库。",
                "info",
            )
            try:
                session = self._new_session(values)
            except Exception as exc:
                self._log(f"[状态] SSH 连接失败：{self._redact(str(exc))}", "error")
                raise RuntimeError("SSH 连接失败，无法读取 VPS 状态") from exc
            try:
                _code, output = session.run(
                    self._status_command(install_dir), timeout=60
                )
                info = self._classify_status(output)
                for line in info["logs"].splitlines():
                    if line.strip():
                        self._log(line, "info")
                message = self._status_message(destination, info)
                level = "ok" if info["state"] == STATUS_RUNNING else "warn"
                self._log(f"[状态] {message}", level)
                self._show_info("VPS 状态", message)
            finally:
                session.close()

        self._run_worker(action)

    def _build_config(self, values: dict[str, str]) -> str:
        return build_deploy_config(values)

    def deploy(self) -> None:
        values = self._snapshot()

        def action() -> None:
            self._validate(values)
            destination = destination_type(values)
            payload = resource_path("payload")
            if not (payload / "remote_install.sh").is_file():
                raise RuntimeError("部署器内部 payload 缺失，请重新下载完整安装包")
            if destination == DESTINATION_GDRIVE:
                self._log(
                    "目标：Google Drive（经 CloudDrive2 WebDAV 写入）。请确认已在 "
                    "CloudDrive2 管理页登录并挂载 Google Drive，并开启 WebDAV。",
                    "info",
                )
            if (
                values.get("deploy_clouddrive2", "true") == "true"
                and values["cd2_url"].strip() != MANAGED_CD2_WEBDAV_URL
            ):
                self._log(
                    "CloudDrive2 与 Bot 位于同一台 VPS：已自动使用安全的容器内网 "
                    f"WebDAV 地址 {MANAGED_CD2_WEBDAV_URL}。"
                )
            self._log(
                f"开始一键部署到 {DESTINATION_LABELS[destination]}。"
                "整个过程可能需要 5–15 分钟。"
            )
            self._log(
                f"保存文件夹：{destination_target(values) or 'WebDAV 根目录'}"
                "（不存在时，Bot 启动后和“检查是否可用”时会自动创建）。",
                "info",
            )
            with tempfile.TemporaryDirectory(prefix="tg115-deployer-") as temp_name:
                temp = Path(temp_name)
                archive = temp / "payload.tar.gz"
                config_file = temp / "config.env"
                # The file is sourced by Bash on the VPS. Writing bytes avoids
                # Windows CRLF endings turning "true" into "true\r".
                config_file.write_bytes(
                    self._build_config(values).encode("utf-8")
                )
                build_payload_archive(payload, archive)
                session = self._new_session(values)
                # UUID plus mkdir mode 700 prevents cross-user stage reuse.
                remote_stage = f"/tmp/tg115-deploy-{uuid.uuid4().hex}"  # nosec B108
                try:
                    self._log("SSH 连接成功，上传部署包……")
                    code, _ = session.run(f"mkdir -m 700 {remote_stage}")
                    if code != 0:
                        raise RuntimeError("无法在 VPS 创建临时部署目录")
                    sftp = session.sftp()
                    try:
                        sftp.put(str(archive), f"{remote_stage}/payload.tar.gz")
                        sftp.put(str(config_file), f"{remote_stage}/config.env")
                        sftp.chmod(f"{remote_stage}/config.env", 0o600)
                    finally:
                        sftp.close()
                    self._log("上传完成，开始安装 VPS 运行环境和服务……")
                    extract = (
                        f"tar -xzf {remote_stage}/payload.tar.gz "
                        f"-C {remote_stage}"
                    )
                    code, output = session.run(extract, stream=self._log, timeout=120)
                    if code != 0:
                        raise RuntimeError("VPS 解压部署包失败")
                    uid_code, uid_output = session.run("id -u")
                    if uid_code != 0:
                        raise RuntimeError("无法确认 VPS 用户权限")
                    use_sudo = uid_output.strip().splitlines()[-1] != "0"
                    if use_sudo and not values.get("sudo_password"):
                        sudo_code, _ = session.run("sudo -n true")
                        if sudo_code != 0:
                            raise RuntimeError(
                                "当前用户不是 root，也没有免密 sudo；请填写 sudo 密码"
                            )
                    install_dir = values["install_dir"].strip()
                    command = (
                        f"INSTALL_DIR={shlex.quote(install_dir)} bash "
                        f"{shlex.quote(remote_stage + '/payload/remote_install.sh')} "
                        f"{shlex.quote(remote_stage + '/payload')} "
                        f"{shlex.quote(remote_stage + '/config.env')}"
                    )
                    code, output = session.run(
                        command,
                        sudo=use_sudo,
                        stream=self._log,
                        timeout=1800,
                    )
                    if code != 0 or "TG115_RESULT=SUCCESS" not in output:
                        raise RuntimeError("远程安装失败，请查看上方安装日志")
                    self._log("基础部署完成，Bot 容器已启动。", "ok")
                    self._show_info("部署成功", deploy_success_message(destination))
                finally:
                    try:
                        if re_safe_remote_stage(remote_stage):
                            session.run(f"rm -rf -- {remote_stage}", timeout=60)
                    except Exception:  # noqa: BLE001 - best-effort remote cleanup
                        self._log("警告：VPS 临时部署目录未能自动清理。")
                    session.close()

        self._run_worker(action)

    def open_clouddrive(self) -> None:
        values = self._snapshot()

        def action() -> None:
            self._validate(values)
            if self.tunnel:
                url = f"http://127.0.0.1:{self.tunnel.port}"
                webbrowser.open(url)
                self._log(f"CloudDrive2 管理页：{url}")
                return
            session = self._new_session(values)
            try:
                code, _ = session.run(
                    "curl -fsS --max-time 10 http://127.0.0.1:19798/ >/dev/null"
                )
                if code != 0:
                    raise RuntimeError(
                        "VPS 本机的 CloudDrive2 19798 端口没有响应，请先完成部署"
                    )
                tunnel = Tunnel(session)
                tunnel.start()
                self.tunnel = tunnel
                url = f"http://127.0.0.1:{tunnel.port}"
                self._log(f"SSH 安全隧道已开启：{url}")
                webbrowser.open(url)
                self._show_info(
                    "CloudDrive2 管理页",
                    "管理页已通过 SSH 隧道打开。部署器关闭时隧道会自动关闭。",
                )
            except Exception:
                session.close()
                raise

        self._run_worker(action)

    def repair_clouddrive(self) -> None:
        values = self._snapshot()

        def action() -> None:
            destination = destination_type(values)
            label = DESTINATION_LABELS[destination]
            self._validate_connection(values)
            if not re.fullmatch(
                r"/opt/[A-Za-z0-9._-]+(?:/[A-Za-z0-9._-]+)*",
                values["install_dir"],
            ):
                raise ValueError("安装目录只能是 /opt/ 下不含空格的安全路径")
            if any(
                part in {".", ".."} for part in values["install_dir"].split("/")
            ):
                raise ValueError("安装目录不能包含 . 或 ..")
            repair_script = resource_path("payload/repair_clouddrive_network.sh")
            if not repair_script.is_file():
                raise RuntimeError("部署器内部修复脚本缺失，请重新下载完整安装包")

            session = self._new_session(values)
            # UUID plus mkdir mode 700 prevents cross-user stage reuse.
            remote_stage = f"/tmp/tg115-deploy-{uuid.uuid4().hex}"  # nosec B108
            try:
                code, _ = session.run(f"mkdir -m 700 {remote_stage}")
                if code != 0:
                    raise RuntimeError("无法在 VPS 创建临时修复目录")
                sftp = session.sftp()
                try:
                    remote_script = f"{remote_stage}/repair_clouddrive_network.sh"
                    sftp.put(str(repair_script), remote_script)
                    sftp.chmod(remote_script, 0o700)
                finally:
                    sftp.close()

                uid_code, uid_output = session.run("id -u")
                if uid_code != 0 or not uid_output.strip():
                    raise RuntimeError("无法确认 VPS 用户权限")
                use_sudo = uid_output.strip().splitlines()[-1] != "0"
                if use_sudo and not values.get("sudo_password"):
                    sudo_code, _ = session.run("sudo -n true")
                    if sudo_code != 0:
                        raise RuntimeError("需要 sudo 密码才能修复 CloudDrive2 网络")

                install_dir = values["install_dir"].strip()
                # 只修网络：脚本的 --network-only 模式不跑网盘写入校验，
                # 因此“修复成功”只代表连接恢复，不代表网盘已可用。
                command = (
                    f"INSTALL_DIR={shlex.quote(install_dir)} bash "
                    f"{shlex.quote(remote_script)} --network-only"
                )
                self._log(
                    "开始修复 CloudDrive2 网络连接（只修 Docker 网络和端口连通，"
                    "不做网盘写入校验）……"
                )
                code, output = session.run(
                    command,
                    sudo=use_sudo,
                    stream=self._log,
                    timeout=240,
                )
                if code != 0 or "TG115_REPAIR=SUCCESS" not in output:
                    raise RuntimeError(describe_failure("repair", output, destination))
                self._log(
                    "CloudDrive2 网络连接已修复（未做网盘写入校验）。", "ok"
                )
                self._show_info(
                    "网络连接已修复",
                    "CloudDrive2 与 Bot 之间的网络连接已经修好，但这只说明连接通了，"
                    f"还没有验证能不能写入 {label}。\n\n"
                    "下一步：点击“登录并挂载网盘”，登录 CloudDrive2 并挂载 "
                    f"{label}，确认 WebDAV 根目录指向该网盘；"
                    "然后回到部署器点击“检查是否可用”，做一次真实写入测试。",
                )
            finally:
                try:
                    if re_safe_remote_stage(remote_stage):
                        session.run(f"rm -rf -- {remote_stage}", timeout=60)
                except Exception:  # noqa: BLE001 - best-effort remote cleanup
                    self._log("警告：VPS 临时修复目录未能自动清理。")
                session.close()

        self._run_worker(action)

    def verify(self) -> None:
        values = self._snapshot()

        def action() -> None:
            destination = destination_type(values)
            self._validate_connection(values)
            if not re.fullmatch(
                r"/opt/[A-Za-z0-9._-]+(?:/[A-Za-z0-9._-]+)*",
                values["install_dir"],
            ):
                raise ValueError("安装目录只能是 /opt/ 下不含空格的安全路径")
            if any(
                part in {".", ".."} for part in values["install_dir"].split("/")
            ):
                raise ValueError("安装目录不能包含 . 或 ..")
            session = self._new_session(values)
            try:
                install_dir = values["install_dir"].strip()
                uid_code, uid_output = session.run("id -u")
                if uid_code != 0 or not uid_output.strip():
                    raise RuntimeError("无法确认 VPS 用户权限")
                use_sudo = uid_output.strip().splitlines()[-1] != "0"
                if use_sudo and not values.get("sudo_password"):
                    sudo_code, _ = session.run("sudo -n true")
                    if sudo_code != 0:
                        raise RuntimeError("需要 sudo 密码才能检查服务")
                command = (
                    f"cd {shlex.quote(install_dir)} && docker compose ps && "
                    "docker compose exec -T tg115-bot "
                    "python -m app.verify_destination && "
                    "docker compose logs --tail=40 tg115-bot"
                )
                code, output = session.run(
                    command, sudo=use_sudo, stream=self._log, timeout=120
                )
                if code != 0:
                    # 两个分支都经 CD2 WebDAV，因此这些诊断对两者同样适用。
                    # 认证/路径/协议/容器/超时分别给结论和下一步，不再只报一句总错误。
                    raise RuntimeError(describe_failure("verify", output, destination))
                if "TG115_DESTINATION=OK" not in output:
                    detail = format_diagnostic_tail(output)
                    message = (
                        f"{DESTINATION_LABELS[destination]} 的 WebDAV 写入测试"
                        "没有返回通过标记。\n"
                        "下一步：点“修复网络”（只修网络），"
                        "成功后再点“检查是否可用”。"
                    )
                    if detail:
                        message = f"{message}\n{detail}"
                    raise RuntimeError(message)
                self._log(
                    f"{DESTINATION_LABELS[destination]} 验收通过（CloudDrive2 WebDAV）："
                    "测试文件已写入、校验、改名并清理。",
                    "ok",
                )
                self._show_info("验收通过", verify_success_message(destination))
            finally:
                session.close()

        self._run_worker(action)

    def _on_close(self) -> None:
        if self.busy and not messagebox.askyesno(
            "确认退出", "部署仍在进行。现在退出可能中断操作，确定退出吗？"
        ):
            return
        self._stop_ui_drain()
        if self.tunnel:
            try:
                self.tunnel.close()
            except Exception as exc:  # noqa: BLE001 - GUI shutdown boundary
                self._log(f"警告：关闭 SSH 隧道时发生错误：{exc}")
        self.root.destroy()


def re_safe_remote_stage(path: str) -> bool:
    # Only a UUID suffix accepted by this guard can be deleted recursively.
    prefix = "/tmp/tg115-deploy-"  # nosec B108
    suffix = path.removeprefix(prefix)
    return path.startswith(prefix) and len(suffix) == 32 and all(
        char in "0123456789abcdef" for char in suffix
    )


def main() -> None:
    root = tk.Tk()
    try:
        style = ttk.Style(root)
        if "vista" in style.theme_names():
            style.theme_use("vista")
    except tk.TclError:
        pass
    InstallerApp(root)
    root.mainloop()


def packaged_self_test(result_path: Path) -> int:
    required = (
        "payload/remote_install.sh",
        "payload/manage.sh",
        "payload/repair_clouddrive_network.sh",
        "payload/docker-compose.yml",
        "payload/.dockerignore",
        "payload/Dockerfile",
        "payload/requirements.txt",
        "payload/app/__init__.py",
        "payload/app/config.py",
        "payload/app/main.py",
        "payload/app/db.py",
        "payload/app/resources.py",
        "payload/app/rclone_client.py",
        "payload/app/verify_destination.py",
    )
    missing = [name for name in required if not resource_path(name).is_file()]
    ui_result = "SKIPPED"
    root = None
    if not missing:
        try:
            root = tk.Tk()
            root.withdraw()
            app = InstallerApp(root)
            root.update_idletasks()
            app.values["destination_type"].set(DESTINATION_GDRIVE)
            root.update_idletasks()
            if app.values["deploy_clouddrive2"].get() != "true":
                raise RuntimeError("Google Drive mode disabled the shared CloudDrive2 layer")
            if str(app.cd2_checkbox.cget("state")) == str(tk.DISABLED):
                raise RuntimeError("Google Drive mode disabled the CloudDrive2 checkbox")
            if (
                str(app.verify_button.cget("text")) != "检查是否可用"
                or not app.verify_button.cget("command")
                or str(app.verify_button.cget("state")) == str(tk.DISABLED)
            ):
                raise RuntimeError("Destination verification action is missing or disabled")
            if str(app.cloud_button.cget("state")) != tk.NORMAL:
                raise RuntimeError("CloudDrive2 admin page is disabled for Google Drive")
            if str(app.repair_button.cget("state")) != tk.NORMAL:
                raise RuntimeError("CloudDrive2 repair is disabled for Google Drive")
            if not app._target_frames[DESTINATION_GDRIVE].grid_info():
                raise RuntimeError("Google Drive target path is hidden")
            if app._target_frames[DESTINATION_CLOUDDRIVE2].grid_info():
                raise RuntimeError("115 target path is still visible for Google Drive")
            app.values["destination_type"].set(DESTINATION_CLOUDDRIVE2)
            root.update_idletasks()
            if str(app.cloud_button.cget("state")) != tk.NORMAL:
                raise RuntimeError("CloudDrive2 admin page is disabled for 115")
            if app.values["deploy_clouddrive2"].get() != "true":
                raise RuntimeError("115 mode lost the CloudDrive2 container choice")
            if not app._target_frames[DESTINATION_CLOUDDRIVE2].grid_info():
                raise RuntimeError("115 target path is hidden")
            if [card.step_title for card in app._step_cards] != [
                title for title, _purpose in BEGINNER_STEPS
            ]:
                raise RuntimeError("beginner steps are missing or out of order")
            if app._advanced_open.get() or app._advanced_body.winfo_manager():
                raise RuntimeError("advanced options are not collapsed by default")
            if str(app.vps_profile_combo.cget("state")) != "readonly":
                raise RuntimeError("VPS profile selector is editable")
            if app.values["vps_profile"].get() != VPS_PROFILE_MINIMUM:
                raise RuntimeError("default VPS profile is not the minimum one")
            if (
                app.values["local_budget_gb"].get(),
                app.values["min_free_disk_gb"].get(),
            ) != ("2", "6"):
                raise RuntimeError("minimum profile did not apply the 2GB/6GB defaults")
            app.vps_profile_combo.set(VPS_PROFILE_LABELS[VPS_PROFILE_STANDARD])
            root.update_idletasks()
            if app.values["vps_profile"].get() != VPS_PROFILE_STANDARD:
                raise RuntimeError("standard profile was not recorded")
            if (
                app.values["local_budget_gb"].get(),
                app.values["min_free_disk_gb"].get(),
            ) != ("20", "20"):
                raise RuntimeError("standard profile did not apply 20GB/20GB")
            app.vps_profile_combo.set(VPS_PROFILE_LABELS[VPS_PROFILE_MINIMUM])
            root.update_idletasks()
            if (
                app.values["local_budget_gb"].get(),
                app.values["min_free_disk_gb"].get(),
            ) != ("2", "6"):
                raise RuntimeError("minimum profile did not restore 2GB/6GB")
            memory_config = {
                key: value
                for key, value in (
                    line.split("=", 1)
                    for line in app._build_config(app._snapshot()).splitlines()
                    if "=" in line
                )
            }
            expected_soft, expected_hard = VPS_PROFILE_MEMORY_LIMITS[VPS_PROFILE_MINIMUM]
            if memory_config["MEMORY_SOFT_MIN_MB"] != expected_soft:
                raise RuntimeError("minimum profile did not emit its memory soft limit")
            if memory_config["MEMORY_HARD_MIN_MB"] != expected_hard:
                raise RuntimeError("minimum profile did not emit its memory hard limit")
            app._stop_ui_drain()
            ui_result = "OK"
        except Exception as exc:  # noqa: BLE001 - packaged runtime self-test
            ui_result = f"FAILED: {type(exc).__name__}: {exc}"
        finally:
            if root is not None:
                root.destroy()
    failed = bool(missing) or ui_result != "OK"
    lines = [
        f"app_version={APP_VERSION}",
        f"paramiko_version={paramiko.__version__}",
        f"payload_missing={','.join(missing)}",
        f"ui_self_test={ui_result}",
        f"result={'FAILED' if failed else 'OK'}",
    ]
    result_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return 1 if failed else 0


if __name__ == "__main__":
    if "--self-test" in sys.argv:
        try:
            index = sys.argv.index("--self-test-result")
            destination = Path(sys.argv[index + 1])
        except (ValueError, IndexError):
            destination = Path(tempfile.gettempdir()) / "tg115-self-test.txt"
        raise SystemExit(packaged_self_test(destination))
    main()
