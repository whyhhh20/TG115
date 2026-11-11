from __future__ import annotations

import asyncio
import os
import sys
import uuid

from .config import Settings
from .main import destination_profile
from .rclone_client import RcloneClient

# 两条分支都经 CloudDrive2 WebDAV 传输，验收只证明 CloudDrive2 接收并校验了
# 文件；115 或 Google Drive 官方端是否可见，仍由用户 /confirm 人工确认。
TRANSPORT_LABEL = "CloudDrive2"


async def verify_destination(
    settings: Settings,
    client: RcloneClient | None = None,
) -> str:
    """Write a tiny probe file through rclone to prove the target is usable."""
    rclone = client or RcloneClient(settings)
    token = uuid.uuid4().hex
    local_path = settings.data_dir / f".tg115-verify-{token}.bin"
    remote_temp = f".tg115-verify-{token}.uploading"
    remote_final = f".tg115-verify-{token}.ok"
    payload = os.urandom(256)
    verification_succeeded = False

    local_path.write_bytes(payload)
    try:
        await rclone.prepare_destination()
        await rclone.upload(local_path, remote_temp)
        uploaded_size = await rclone.remote_size(remote_temp)
        if uploaded_size != len(payload):
            raise RuntimeError(
                f"{TRANSPORT_LABEL} 临时测试文件大小错误："
                f"{uploaded_size} != {len(payload)}"
            )
        await rclone.move(remote_temp, remote_final)
        final_size = await rclone.remote_size(remote_final)
        if final_size != len(payload):
            raise RuntimeError(
                f"{TRANSPORT_LABEL} 最终测试文件大小错误："
                f"{final_size} != {len(payload)}"
            )
        if await rclone.exists(remote_temp):
            raise RuntimeError(f"{TRANSPORT_LABEL} 临时测试文件改名后仍然存在")
        await rclone.remove(remote_final)
        if await rclone.exists(remote_final):
            raise RuntimeError(f"{TRANSPORT_LABEL} 测试文件清理失败")
        verification_succeeded = True
        return remote_final
    finally:
        try:
            local_path.unlink(missing_ok=True)
        except OSError:
            pass
        if not verification_succeeded:
            for remote_path in (remote_temp, remote_final):
                try:
                    await rclone.remove(remote_path)
                except Exception as exc:  # noqa: BLE001 - best-effort cleanup
                    print(
                        f"TG115_CLEANUP_WARNING={remote_path}: {exc}",
                        file=sys.stderr,
                    )


async def async_main() -> int:
    settings = Settings.from_env()
    profile = destination_profile(getattr(settings, "destination_type", None))
    remote_path = await verify_destination(settings)
    print("TG115_DESTINATION=OK")
    print(f"TG115_DESTINATION_TYPE={profile.key}")
    print(f"TG115_DESTINATION_LABEL={profile.label}")
    print(f"TG115_VERIFIED_TRANSPORT={TRANSPORT_LABEL}")
    print(f"TG115_TEST_PATH={remote_path}")
    print(f"TG115_OFFICIAL_CONFIRM=PENDING:{profile.official}")
    return 0


def main() -> int:
    try:
        return asyncio.run(async_main())
    except Exception as exc:  # noqa: BLE001 - CLI error boundary
        print(f"TG115_DESTINATION=FAILED: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
