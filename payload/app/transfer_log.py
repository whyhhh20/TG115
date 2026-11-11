"""Per-task rclone transfer logs with hard size limits.

``tg115.log`` stays the shared diagnostic log (2 MiB x 2 files) and must not
grow with every transfer. Each transfer instead writes at most one
``logs/transfers/<task_id>.log``:

* one file per task, capped at 1 MiB by keeping only the newest tail;
* the whole directory is capped at 8 MiB or 10 files, oldest removed first;
* the file is deleted as soon as its task reaches ``completed``, so a
  successful transfer leaves nothing behind;
* failed or unfinished tasks keep their own file for diagnosis.

The task database rows are never touched here: completed rows still serve
de-duplication and ``/confirm``.
"""
from __future__ import annotations

import logging
from collections.abc import Callable
from pathlib import Path

TRANSFER_LOG_MAX_BYTES = 1024 * 1024
TRANSFER_DIR_MAX_BYTES = 8 * 1024 * 1024
TRANSFER_DIR_MAX_FILES = 10

log = logging.getLogger("tg115.transfer_log")


def transfers_dir(log_dir: Path) -> Path:
    return Path(log_dir) / "transfers"


def task_log_path(log_dir: Path, task_id: int) -> Path:
    return transfers_dir(log_dir) / f"{int(task_id)}.log"


def task_id_from_log(path: Path) -> int | None:
    """Return the task id encoded in a transfer log name, else ``None``."""
    stem = path.stem
    return int(stem) if stem.isdigit() else None


def _trim_to_tail(path: Path, max_bytes: int) -> None:
    size = path.stat().st_size
    if size <= max_bytes:
        return
    with path.open("rb") as handle:
        handle.seek(size - max_bytes)
        tail = handle.read(max_bytes)
    with path.open("wb") as handle:
        handle.write(tail)


def append_transfer_log(
    log_dir: Path,
    task_id: int,
    text: str,
    *,
    max_bytes: int = TRANSFER_LOG_MAX_BYTES,
) -> Path | None:
    """Append ``text`` to this task's log, keeping at most ``max_bytes``.

    Returns the log path, or ``None`` when nothing was written. A logging
    failure is never fatal: the transfer itself already succeeded or failed on
    its own terms.
    """
    if not text:
        return None
    if not text.endswith("\n"):
        text += "\n"
    path = task_log_path(log_dir, task_id)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("ab") as handle:
            handle.write(text.encode("utf-8", errors="replace"))
        _trim_to_tail(path, max_bytes)
    except OSError as exc:
        log.warning("无法写入任务 #%s 的传输日志：%s", task_id, exc)
        return None
    prune_transfers(log_dir)
    return path


def remove_task_log(log_dir: Path, task_id: int) -> bool:
    """Delete this task's log. Missing files count as success."""
    path = task_log_path(log_dir, task_id)
    try:
        path.unlink(missing_ok=True)
    except OSError as exc:
        log.warning("无法删除任务 #%s 的传输日志：%s", task_id, exc)
        return False
    return True


def prune_transfers(
    log_dir: Path,
    *,
    max_bytes: int = TRANSFER_DIR_MAX_BYTES,
    max_files: int = TRANSFER_DIR_MAX_FILES,
) -> int:
    """Drop the oldest transfer logs until the directory fits both limits."""
    directory = transfers_dir(log_dir)
    try:
        entries = [path for path in directory.iterdir() if path.is_file()]
    except OSError:
        return 0
    sized: list[tuple[float, str, int, Path]] = []
    for path in entries:
        try:
            stat = path.stat()
        except OSError:
            continue
        sized.append((stat.st_mtime, path.name, stat.st_size, path))
    sized.sort(key=lambda item: (item[0], item[1]))
    total = sum(item[2] for item in sized)
    removed = 0
    while sized and (total > max_bytes or len(sized) > max_files):
        _, _, size, path = sized.pop(0)
        try:
            path.unlink()
        except OSError as exc:
            log.warning("无法删除旧的传输日志 %s：%s", path.name, exc)
            continue
        total -= size
        removed += 1
    return removed


def sweep_transfers(
    log_dir: Path,
    is_completed: Callable[[int], bool],
    *,
    max_bytes: int = TRANSFER_DIR_MAX_BYTES,
    max_files: int = TRANSFER_DIR_MAX_FILES,
) -> dict[str, int]:
    """Startup cleanup: drop logs of completed tasks, then enforce the caps.

    ``is_completed`` is asked per log file so no full task listing is needed;
    a database error keeps the file instead of failing startup.
    """
    directory = transfers_dir(log_dir)
    try:
        entries = sorted(path for path in directory.iterdir() if path.is_file())
    except OSError:
        entries = []
    removed_completed = 0
    for path in entries:
        task_id = task_id_from_log(path)
        if task_id is None:
            continue
        try:
            completed = bool(is_completed(task_id))
        except Exception as exc:  # noqa: BLE001 - startup must not fail here
            log.warning("无法确认任务 #%s 的状态，保留其传输日志：%s", task_id, exc)
            continue
        if not completed:
            continue
        try:
            path.unlink()
        except OSError as exc:
            log.warning("无法删除任务 #%s 的残留传输日志：%s", task_id, exc)
            continue
        removed_completed += 1
    pruned = prune_transfers(log_dir, max_bytes=max_bytes, max_files=max_files)
    return {"completed": removed_completed, "pruned": pruned}
