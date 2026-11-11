from __future__ import annotations

import asyncio
import json
import logging
import os
from collections.abc import Callable
from pathlib import Path
from typing import Any

from .config import (
    DESTINATION_CLOUDDRIVE2,
    DESTINATION_GDRIVE,
    SUPPORTED_DESTINATIONS,
    Settings,
)
from .transfer_log import append_transfer_log

# CloudDrive2 is the single transport layer: 115 and Google Drive are just
# different mount points inside the same WebDAV endpoint, so both branches use
# this one remote and never create a Google ``type = drive`` remote.
CD2_REMOTE_NAME = "cd2"

# Both the in-memory tail and the per-task log are bounded; the text embedded in
# an exception is bounded again so a chatty failure cannot flood the database.
PROCESS_TAIL_LIMIT = 64 * 1024
ERROR_TEXT_LIMIT = 4000



class RcloneError(RuntimeError):
    pass


async def _read_process_tail(
    stream: asyncio.StreamReader | None, limit: int = PROCESS_TAIL_LIMIT
) -> str:
    if stream is None:
        return ""
    tail = bytearray()
    while True:
        chunk = await stream.read(8192)
        if not chunk:
            break
        tail.extend(chunk)
        if len(tail) > limit:
            del tail[:-limit]
    return tail.decode("utf-8", errors="replace").strip()


def _error_text(text: str, limit: int = ERROR_TEXT_LIMIT) -> str:
    """Bound the text handed to an exception (and therefore to the database)."""
    if len(text) <= limit:
        return text
    return "…（已截断）" + text[-limit:]


class RcloneUploadStream:
    """Async file-like sink that applies subprocess backpressure to Telethon."""

    def __init__(
        self,
        process: asyncio.subprocess.Process,
        *,
        log_dir: Path | None = None,
        task_id: int | None = None,
        redact: Callable[[str], str] | None = None,
    ):
        if process.stdin is None:
            raise RuntimeError("rclone 流式进程没有标准输入")
        self.process = process
        self.stdin = process.stdin
        self.bytes_written = 0
        self.log_dir = Path(log_dir) if log_dir is not None else None
        self.task_id = task_id
        self._redact = redact or (lambda text: text)
        self._stdout_task = asyncio.create_task(
            _read_process_tail(process.stdout), name="rclone-stream-stdout"
        )
        self._stderr_task = asyncio.create_task(
            _read_process_tail(process.stderr), name="rclone-stream-stderr"
        )
        self._finished = False

    def _write_transfer_log(self, stdout: str, stderr: str) -> None:
        """Append this attempt's output to ``transfers/<task_id>.log``.

        Only the bounded tails kept in memory are written, so the file can never
        grow past the client's limit, and the password is redacted first.
        """
        if self.task_id is None or self.log_dir is None:
            return
        parts = []
        if stderr:
            parts.append(f"[stderr]\n{stderr}")
        if stdout:
            parts.append(f"[stdout]\n{stdout}")
        if not parts:
            return
        append_transfer_log(
            self.log_dir, self.task_id, self._redact("\n".join(parts))
        )

    async def write(self, data: bytes) -> int:
        if self._finished:
            raise RcloneError("rclone 流式写入已经结束")
        self.stdin.write(data)
        await self.stdin.drain()
        self.bytes_written += len(data)
        return len(data)

    def tell(self) -> int:
        return self.bytes_written

    def flush(self) -> None:
        # Telethon calls flush synchronously after all awaited writes.
        return None

    async def finish(self) -> None:
        if self._finished:
            return
        self._finished = True
        self.stdin.close()
        try:
            await self.stdin.wait_closed()
        except (BrokenPipeError, ConnectionResetError):
            pass
        return_code = await self.process.wait()
        stdout, stderr = await asyncio.gather(
            self._stdout_task, self._stderr_task
        )
        self._write_transfer_log(stdout, stderr)
        if return_code != 0:
            raise RcloneError(
                f"rclone 流式上传退出码 {return_code}："
                f"{_error_text(stderr or stdout or '未知错误')}"
            )

    async def abort(self) -> None:
        if self.process.returncode is None:
            try:
                self.process.terminate()
            except ProcessLookupError:
                pass
            try:
                await asyncio.wait_for(self.process.wait(), timeout=10)
            except TimeoutError:
                try:
                    self.process.kill()
                except ProcessLookupError:
                    pass
                await self.process.wait()
        self._finished = True
        stdout, stderr = await asyncio.gather(
            self._stdout_task, self._stderr_task, return_exceptions=True
        )
        self._write_transfer_log(
            stdout if isinstance(stdout, str) else "",
            stderr if isinstance(stderr, str) else "",
        )


class RcloneClient:
    def __init__(self, settings: Settings):
        self.settings = settings
        self.log = logging.getLogger("tg115.rclone")
        self._config_lock = asyncio.Lock()
        self._config_ready = False

    # -- destination selection ------------------------------------------------
    def _destination(self) -> str:
        value = (
            getattr(self.settings, "destination_type", DESTINATION_CLOUDDRIVE2)
            or DESTINATION_CLOUDDRIVE2
        )
        if value not in SUPPORTED_DESTINATIONS:
            raise RcloneError(f"未知的网盘目标类型：{value!r}")
        return str(value)

    def _is_gdrive(self) -> bool:
        return self._destination() == DESTINATION_GDRIVE

    def _target_path(self) -> str:
        """Relative path below the CloudDrive2 WebDAV root.

        ``destination_target`` is what the deployer selected; the ``cd2_target``
        fallback keeps hand-built settings objects working.
        """
        raw = getattr(self.settings, "destination_target", None)
        if not isinstance(raw, str) or not raw:
            raw = getattr(self.settings, "cd2_target", "") or ""
        return str(raw).strip("/")

    def destination_label(self) -> str:
        label = getattr(self.settings, "destination_label", None)
        if isinstance(label, str) and label:
            return label
        try:
            return "Google Drive" if self._is_gdrive() else "115"
        except RcloneError:
            return "115"

    # -- process helpers ------------------------------------------------------
    async def _run(
        self,
        *args: str,
        timeout: float | None = None,
        check: bool = True,
        task_id: int | None = None,
    ) -> tuple[int, str, str]:
        """Run rclone and keep only the bounded tails of its output.

        ``communicate()`` would hold the whole stderr of a large upload in
        memory, so both pipes are drained by tail readers instead. When
        ``task_id`` is given the same text is appended to that task's transfer
        log, which the service deletes once the task is completed.
        """
        command = [
            "rclone",
            "--config",
            str(self.settings.rclone_config_path),
            *args,
        ]
        self.log.debug("运行 rclone：%s", " ".join(self._redact(command)))
        process = await asyncio.create_subprocess_exec(
            *command,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout_task = asyncio.create_task(
            _read_process_tail(process.stdout), name="rclone-stdout"
        )
        stderr_task = asyncio.create_task(
            _read_process_tail(process.stderr), name="rclone-stderr"
        )
        try:
            await asyncio.wait_for(process.wait(), timeout=timeout)
        except asyncio.CancelledError:
            await self._terminate(process)
            out, err = await asyncio.gather(
                stdout_task, stderr_task, return_exceptions=True
            )
            self._append_transfer_log(
                task_id,
                out if isinstance(out, str) else "",
                err if isinstance(err, str) else "",
            )
            raise
        except TimeoutError as exc:
            await self._terminate(process)
            out, err = await asyncio.gather(
                stdout_task, stderr_task, return_exceptions=True
            )
            self._append_transfer_log(
                task_id,
                out if isinstance(out, str) else "",
                err if isinstance(err, str) else "",
            )
            raise RcloneError(f"rclone 超时：{' '.join(args[:2])}") from exc
        out, err = await asyncio.gather(stdout_task, stderr_task)
        self._append_transfer_log(task_id, out, err)
        if check and process.returncode != 0:
            raise RcloneError(
                f"rclone 退出码 {process.returncode}："
                f"{_error_text(err or out or '未知错误')}"
            )
        return int(process.returncode or 0), out, err

    async def _terminate(self, process: asyncio.subprocess.Process) -> None:
        """Stop a running rclone process without leaving it behind."""
        if process.returncode is None:
            try:
                process.terminate()
            except ProcessLookupError:
                pass
            try:
                await asyncio.wait_for(process.wait(), timeout=10)
                return
            except TimeoutError:
                pass
        if process.returncode is None:
            try:
                process.kill()
            except ProcessLookupError:
                pass
        await process.wait()

    def _append_transfer_log(
        self, task_id: int | None, stdout: str, stderr: str
    ) -> None:
        if task_id is None:
            return
        log_dir = getattr(self.settings, "log_dir", None)
        if log_dir is None:
            return
        parts = []
        if stderr:
            parts.append(f"[stderr]\n{stderr}")
        if stdout:
            parts.append(f"[stdout]\n{stdout}")
        if not parts:
            return
        append_transfer_log(
            Path(log_dir), task_id, self._redact_text("\n".join(parts))
        )

    def _redact_text(self, text: str) -> str:
        for secret in self._secret_values():
            text = text.replace(secret, "***")
        return text

    def _secret_values(self) -> set[str]:
        secrets: set[str] = set()
        for attribute in ("cd2_password",):
            value = getattr(self.settings, attribute, "")
            if isinstance(value, str) and value.strip():
                secrets.add(value)
                secrets.add(value.strip())
        return secrets

    def _redact(self, command: list[str]) -> list[str]:
        secrets = self._secret_values()
        redacted = []
        for item in command:
            if item in secrets:
                redacted.append("***")
            else:
                redacted.append(item)
        return redacted

    # -- config ---------------------------------------------------------------
    async def ensure_config(self) -> None:
        """Create or reconcile the one CloudDrive2 WebDAV remote.

        Both destinations share it: the selected network drive is mounted inside
        CloudDrive2 by the user, and only the relative target path differs.
        """
        if self._config_ready:
            return
        async with self._config_lock:
            if self._config_ready:
                return
            path = self.settings.rclone_config_path
            path.parent.mkdir(parents=True, exist_ok=True)
            await self._ensure_cd2_config(path)
            try:
                os.chmod(path, 0o600)
            except OSError:
                self.log.warning("无法把 rclone 配置权限设置为 600")
            self._config_ready = True

    async def _ensure_cd2_config(self, path: Path) -> None:
        common = (
            "url",
            self.settings.cd2_url,
            "vendor",
            "other",
            "user",
            self.settings.cd2_user,
            "pass",
            self.settings.cd2_password,
            "--obscure",
            "--non-interactive",
        )
        updated = False
        if path.exists() and path.stat().st_size > 0:
            code, _, _ = await self._run(
                "config",
                "update",
                CD2_REMOTE_NAME,
                *common,
                timeout=30,
                check=False,
            )
            updated = code == 0
        if not updated:
            await self._run(
                "config",
                "create",
                CD2_REMOTE_NAME,
                "webdav",
                *common,
                timeout=30,
            )

    # -- remote paths ---------------------------------------------------------
    def remote(self, relative_path: str = "") -> str:
        # Fail closed on an unknown destination before any path is built.
        self._destination()
        target = self._target_path()
        relative = relative_path.strip("/")
        path = "/".join(part for part in (target, relative) if part)
        return f"{CD2_REMOTE_NAME}:{path}"

    async def prepare_destination(self) -> None:
        await self.ensure_config()
        remote = self.remote()
        if not remote.endswith(":"):
            await self._run("mkdir", remote, timeout=45)
        # The remote root always exists; lsd is the actual reachability probe.
        await self._run("lsd", remote, "--max-depth", "1", timeout=45)

    async def healthy(self) -> bool:
        try:
            await self.prepare_destination()
            return True
        except Exception as exc:  # noqa: BLE001 - health boundary for external CLI
            self.log.warning(
                "%s 健康检查失败：%s", self.destination_label(), exc
            )
            return False

    async def upload(
        self, local_path: Path, remote_temp: str, *, task_id: int | None = None
    ) -> None:
        kwargs: dict[str, Any] = {} if task_id is None else {"task_id": task_id}
        await self._run(
            "copyto",
            str(local_path),
            self.remote(remote_temp),
            "--transfers",
            "1",
            "--checkers",
            "2",
            "--retries",
            "2",
            "--low-level-retries",
            "5",
            "--contimeout",
            "20s",
            "--timeout",
            "5m",
            "--stats",
            "15s",
            "--stats-one-line",
            timeout=None,
            **kwargs,
        )

    async def open_upload_stream(
        self, remote_temp: str, exact_size: int, *, task_id: int | None = None
    ) -> RcloneUploadStream:
        await self.ensure_config()
        command = [
            "rclone",
            "--config",
            str(self.settings.rclone_config_path),
            "rcat",
            self.remote(remote_temp),
            "--size",
            str(exact_size),
            "--contimeout",
            "20s",
            "--timeout",
            "5m",
            "--retries",
            "1",
            "--low-level-retries",
            "5",
            "--stats",
            "0",
        ]
        self.log.debug("运行 rclone 流式写入：%s", " ".join(self._redact(command)))
        process = await asyncio.create_subprocess_exec(
            *command,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        return RcloneUploadStream(
            process,
            log_dir=getattr(self.settings, "log_dir", None),
            task_id=task_id,
            redact=self._redact_text,
        )

    async def remote_size(self, relative_path: str) -> int:
        _, out, _ = await self._run(
            "size", self.remote(relative_path), "--json", timeout=60
        )
        try:
            payload = json.loads(out)
            return int(payload["bytes"])
        except (ValueError, KeyError, TypeError) as exc:
            raise RcloneError(f"无法解析远端大小：{out!r}") from exc

    async def exists(self, relative_path: str) -> bool:
        code, out, err = await self._run(
            "lsjson",
            self.remote(relative_path),
            "--stat",
            timeout=45,
            check=False,
        )
        if code == 0:
            return True
        if code in {3, 4}:
            return False
        raise RcloneError(
            f"无法确认远端文件是否存在（rclone 退出码 {code}）："
            f"{err or out or '未知错误'}"
        )

    async def move(self, source: str, destination: str) -> None:
        await self._run(
            "moveto",
            self.remote(source),
            self.remote(destination),
            "--retries",
            "3",
            timeout=120,
        )

    async def remove(self, relative_path: str) -> None:
        code, out, err = await self._run(
            "deletefile", self.remote(relative_path), timeout=60, check=False
        )
        if code not in {0, 3, 4}:
            self.log.warning(
                "清理远端临时文件失败（rclone 退出码 %s）：%s",
                code,
                err or out or "未知错误",
            )
