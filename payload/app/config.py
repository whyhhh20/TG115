from __future__ import annotations

import base64
import math
import os
from dataclasses import dataclass
from pathlib import Path

DESTINATION_CLOUDDRIVE2 = "clouddrive2"
DESTINATION_GDRIVE = "gdrive"

# 传输方式：auto = 单文件不超过本地预算时先落盘再上传、超过时流式；
# stream = 所有文件都直接 Telegram → rclone rcat → CloudDrive2，不占 Bot 本地磁盘。
TRANSFER_MODE_AUTO = "auto"
TRANSFER_MODE_STREAM = "stream"
TRANSFER_MODES = (TRANSFER_MODE_AUTO, TRANSFER_MODE_STREAM)
SUPPORTED_DESTINATIONS = (DESTINATION_CLOUDDRIVE2, DESTINATION_GDRIVE)

# The UI exposes exactly two choices and both are transferred through
# CloudDrive2, so the accepted values are the internal keys plus the labels the
# deployer writes. ``115`` is the historical name of the CloudDrive2 path.
DESTINATION_ALIASES = {
    "clouddrive2": DESTINATION_CLOUDDRIVE2,
    "115": DESTINATION_CLOUDDRIVE2,
    "gdrive": DESTINATION_GDRIVE,
    "google_drive": DESTINATION_GDRIVE,
}

# Variables written by the removed direct-Google implementation. Seeing them
# without CloudDrive2 settings means an old .env is being reused; the only valid
# fix is to re-run the deployer with CloudDrive2 WebDAV credentials.
_LEGACY_GDRIVE_ENV = (
    "GDRIVE_TOKEN_B64",
    "GDRIVE_CLIENT_ID_B64",
    "GDRIVE_CLIENT_SECRET_B64",
    "GDRIVE_ROOT_FOLDER_ID_B64",
    "GDRIVE_TARGET_PATH_B64",
)

_CD2_ENV = (
    "CD2_WEBDAV_URL_B64",
    "CD2_WEBDAV_USERNAME_B64",
    "CD2_WEBDAV_PASSWORD_B64",
)


def _decode_b64(name: str, *, required: bool = True, default: str = "") -> str:
    value = os.getenv(name, "")
    if not value:
        if required:
            raise RuntimeError(f"缺少必填配置：{name}")
        return default
    try:
        return base64.b64decode(value.encode("ascii"), validate=True).decode("utf-8")
    except Exception as exc:
        raise RuntimeError(f"配置 {name} 不是有效的 Base64 UTF-8 数据") from exc


def _int(name: str, default: int) -> int:
    value = os.getenv(name)
    try:
        return int(value) if value is not None else default
    except ValueError as exc:
        raise RuntimeError(f"配置 {name} 必须是整数") from exc


def _float(name: str, default: float) -> float:
    value = os.getenv(name)
    try:
        return float(value) if value is not None else default
    except ValueError as exc:
        raise RuntimeError(f"配置 {name} 必须是数字") from exc


def _has_control_chars(value: str) -> bool:
    return any(ord(char) < 0x20 or ord(char) == 0x7F for char in value)


def _destination_type() -> str:
    raw = (os.getenv("DESTINATION_TYPE") or "").strip().lower()
    if not raw:
        return DESTINATION_CLOUDDRIVE2
    destination = DESTINATION_ALIASES.get(raw)
    if destination is None:
        raise RuntimeError(
            "DESTINATION_TYPE 只能是 clouddrive2（115）或 gdrive（Google Drive），"
            f"当前为 {raw!r}"
        )
    return destination


def _require_cd2_credentials() -> None:
    """Both destinations talk to CloudDrive2, so the WebDAV login is mandatory.

    A config that only carries the removed Google OAuth variables must fail
    loudly instead of silently falling back to a direct Google Drive remote.
    """
    missing = [name for name in _CD2_ENV if not os.getenv(name)]
    if not missing:
        return
    legacy = [name for name in _LEGACY_GDRIVE_ENV if os.getenv(name)]
    if legacy:
        raise RuntimeError(
            f"缺少必填配置：{missing[0]}；检测到旧版直连 Google Drive 配置"
            f"（{'、'.join(legacy)}）。直连方式已移除，115 和 Google Drive 都必须"
            "通过 CloudDrive2 传输，请重新运行部署器并填写 CloudDrive2 WebDAV "
            "地址、用户名和密码"
        )
    raise RuntimeError(f"缺少必填配置：{missing[0]}")


def _relative_target(name: str, value: str) -> str:
    """Normalise a user supplied path below the remote root.

    Absolute paths, backslash separators, control characters and ``.``/``..``
    segments are rejected so a configured target can never escape the folder the
    user selected. An empty value means the WebDAV root already is the target
    folder; the mount name is never guessed.
    """
    raw = value.strip()
    if not raw:
        return ""
    if _has_control_chars(raw):
        raise RuntimeError(f"配置 {name} 含有控制字符")
    if "\\" in raw:
        raise RuntimeError(f"配置 {name} 只能使用 / 作为路径分隔符")
    if raw.startswith("/"):
        raise RuntimeError(f"配置 {name} 必须是根目录下的相对路径")
    parts = [part for part in raw.split("/") if part]
    for part in parts:
        if part in {".", ".."}:
            raise RuntimeError(f"配置 {name} 不允许包含 . 或 .. 路径片段")
    return "/".join(parts)


@dataclass(frozen=True)
class Settings:
    api_id: int
    api_hash: str
    bot_token: str
    allowed_user_id: int
    cd2_url: str
    cd2_user: str
    cd2_password: str
    cd2_target: str
    data_dir: Path
    download_dir: Path
    log_dir: Path
    rclone_config_path: Path
    local_budget_bytes: int
    min_free_disk_bytes: int
    control_interval: float
    cpu_target_low: float
    cpu_target_high: float
    cpu_pressure: float
    memory_soft_min_bytes: int
    memory_hard_min_bytes: int
    ramp_up_step: int
    ramp_down_factor: float
    max_retries: int
    remote_health_interval: float
    # Appended with a default so older positional/keyword construction still works.
    destination_type: str = DESTINATION_CLOUDDRIVE2
    transfer_mode: str = TRANSFER_MODE_AUTO

    @property
    def always_stream(self) -> bool:
        return self.transfer_mode == TRANSFER_MODE_STREAM

    @property
    def destination_label(self) -> str:
        if self.destination_type == DESTINATION_GDRIVE:
            return "Google Drive"
        return "115"

    @property
    def destination_target(self) -> str:
        """Path below the CloudDrive2 WebDAV root for the selected destination.

        The deployer writes the path of the selected destination into
        ``CD2_TARGET_PATH_B64``, so both destinations read the same field.
        """
        return self.cd2_target

    @classmethod
    def from_env(cls) -> Settings:
        api_id = _int("TELEGRAM_API_ID", 0)
        allowed_user_id = _int("ALLOWED_USER_ID", 0)
        if api_id <= 0:
            raise RuntimeError("TELEGRAM_API_ID 必须大于 0")
        if allowed_user_id <= 0:
            raise RuntimeError("ALLOWED_USER_ID 必须大于 0")

        destination_type = _destination_type()
        transfer_mode = (
            os.getenv("TRANSFER_MODE", TRANSFER_MODE_AUTO).strip().lower()
            or TRANSFER_MODE_AUTO
        )
        if transfer_mode not in TRANSFER_MODES:
            raise RuntimeError(
                "TRANSFER_MODE 只能是 auto 或 stream，当前值：" + transfer_mode
            )
        _require_cd2_credentials()

        data_dir = Path(os.getenv("DATA_DIR", "/data"))
        download_dir = Path(os.getenv("DOWNLOAD_DIR", "/downloads"))
        log_dir = Path(os.getenv("LOG_DIR", "/logs"))
        rclone_config_path = Path(
            os.getenv("RCLONE_CONFIG_PATH", "/config/rclone/rclone.conf")
        )
        budget_gb = _float("LOCAL_TEMP_BUDGET_GB", 20.0)
        min_free_gb = _float("MIN_FREE_DISK_GB", 20.0)
        if (
            not math.isfinite(budget_gb)
            or not math.isfinite(min_free_gb)
            or budget_gb <= 0
            or min_free_gb <= 0
        ):
            raise RuntimeError("磁盘预算和安全线必须是大于 0 的有限数字")

        settings = cls(
            api_id=api_id,
            api_hash=_decode_b64("TELEGRAM_API_HASH_B64"),
            bot_token=_decode_b64("BOT_TOKEN_B64"),
            allowed_user_id=allowed_user_id,
            cd2_url=_decode_b64("CD2_WEBDAV_URL_B64"),
            cd2_user=_decode_b64("CD2_WEBDAV_USERNAME_B64"),
            cd2_password=_decode_b64("CD2_WEBDAV_PASSWORD_B64"),
            cd2_target=_relative_target(
                "CD2_TARGET_PATH_B64",
                _decode_b64("CD2_TARGET_PATH_B64", required=False),
            ),
            data_dir=data_dir,
            download_dir=download_dir,
            log_dir=log_dir,
            rclone_config_path=rclone_config_path,
            local_budget_bytes=int(budget_gb * 1024**3),
            min_free_disk_bytes=int(min_free_gb * 1024**3),
            control_interval=max(1.0, _float("CONTROL_INTERVAL_SECONDS", 3.0)),
            cpu_target_low=_float("CPU_TARGET_LOW_PERCENT", 60.0),
            cpu_target_high=_float("CPU_TARGET_HIGH_PERCENT", 80.0),
            cpu_pressure=_float("CPU_PRESSURE_PERCENT", 90.0),
            memory_soft_min_bytes=_int("MEMORY_SOFT_MIN_MB", 1024) * 1024**2,
            memory_hard_min_bytes=_int("MEMORY_HARD_MIN_MB", 512) * 1024**2,
            ramp_up_step=max(1, _int("RAMP_UP_STEP", 1)),
            ramp_down_factor=min(0.9, max(0.1, _float("RAMP_DOWN_FACTOR", 0.5))),
            max_retries=max(1, _int("MAX_RETRIES", 3)),
            remote_health_interval=max(
                10.0, _float("REMOTE_HEALTH_INTERVAL_SECONDS", 30.0)
            ),
            destination_type=destination_type,
            transfer_mode=transfer_mode,
        )
        if not (
            0
            < settings.cpu_target_low
            < settings.cpu_target_high
            < settings.cpu_pressure
            <= 100
        ):
            raise RuntimeError("CPU 阈值必须满足 0 < LOW < HIGH < PRESSURE <= 100")
        for path in (
            settings.data_dir,
            settings.download_dir,
            settings.log_dir,
            settings.rclone_config_path.parent,
        ):
            path.mkdir(parents=True, exist_ok=True)
        return settings
