from __future__ import annotations

import sqlite3
import threading
import time
from collections.abc import Iterable, Mapping
from pathlib import Path
from typing import Any

from .naming import safe_file_name

# 升级前创建的部署只有 CloudDrive2 一个目标。没有目标绑定的历史任务只允许
# 继续走这个默认目标，避免切换网盘后把旧任务悄悄写到另一个网盘。
LEGACY_DESTINATION = "clouddrive2"

DESTINATION_COLUMNS = (
    "destination_type",
    "destination_target",
    "destination_root",
    "destination_account",
)

LOCAL_STATES = (
    "reserved",
    "downloading",
    "downloaded",
    "waiting_upload",
    "uploading",
    "verifying",
    "finalizing",
    "cleanup_pending",
    "upload_failed_retained",
    "verification_failed_retained",
)

GROWING_STATES = (
    "reserved",
    "downloading",
)

# 尚未结束、仍可能被自动调度写入网盘的状态。
PENDING_STATES = (
    "queued",
    "reserved",
    "downloading",
    "streaming",
    "downloaded",
    "waiting_upload",
    "uploading",
    "verifying",
    "finalizing",
    "cleanup_pending",
    "upload_failed_retained",
    "verification_failed_retained",
)


def destination_values(destination: Mapping[str, Any] | None) -> dict[str, str]:
    """Normalise a destination binding into the four persisted columns."""
    data = dict(destination or {})
    return {
        column: str(data.get(column) or "") for column in DESTINATION_COLUMNS
    }


def destination_matches(
    task: Mapping[str, Any], destination: Mapping[str, Any] | None
) -> bool:
    """Whether a persisted task may still write to the current destination.

    A task without any stored type was created before destination binding
    existed, so it may only continue on the legacy CloudDrive2 target. Tasks
    that do carry a type must match the configured target/root/account exactly,
    so changing the destination never silently relocates unfinished work.
    """
    current = destination_values(destination)
    stored_type = str(task.get("destination_type") or "")
    if not stored_type:
        return current["destination_type"] == LEGACY_DESTINATION
    if stored_type != current["destination_type"]:
        return False
    for column in ("destination_target", "destination_root", "destination_account"):
        if str(task.get(column) or "") != current[column]:
            return False
    return True


def _destination_sql(
    destination: Mapping[str, Any] | None,
) -> tuple[str, list[Any]]:
    current = destination_values(destination)
    clause = (
        "(("
        "(destination_type IS NULL OR destination_type = '') AND ? = ?"
        ") OR ("
        "destination_type = ?"
        " AND COALESCE(destination_target, '') = ?"
        " AND COALESCE(destination_root, '') = ?"
        " AND COALESCE(destination_account, '') = ?"
        "))"
    )
    params: list[Any] = [
        LEGACY_DESTINATION,
        current["destination_type"],
        current["destination_type"],
        current["destination_target"],
        current["destination_root"],
        current["destination_account"],
    ]
    return clause, params


class TaskDB:
    def __init__(self, path: Path):
        self.path = path
        self._lock = threading.RLock()
        self._conn = sqlite3.connect(
            path, timeout=30, check_same_thread=False, isolation_level=None
        )
        self._conn.row_factory = sqlite3.Row
        with self._lock:
            self._conn.execute("PRAGMA journal_mode=WAL")
            self._conn.execute("PRAGMA synchronous=FULL")
            self._conn.execute("PRAGMA foreign_keys=ON")
            self._conn.execute("PRAGMA busy_timeout=30000")
            self._conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS tasks (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    chat_id INTEGER NOT NULL,
                    message_id INTEGER NOT NULL,
                    sender_id INTEGER NOT NULL,
                    media_key TEXT,
                    file_name TEXT NOT NULL,
                    file_size INTEGER NOT NULL CHECK(file_size >= 0),
                    transfer_mode TEXT NOT NULL DEFAULT 'local',
                    destination_type TEXT,
                    destination_target TEXT,
                    destination_root TEXT,
                    destination_account TEXT,
                    state TEXT NOT NULL,
                    local_path TEXT,
                    remote_path TEXT,
                    downloaded_bytes INTEGER NOT NULL DEFAULT 0,
                    uploaded_bytes INTEGER NOT NULL DEFAULT 0,
                    download_retries INTEGER NOT NULL DEFAULT 0,
                    upload_retries INTEGER NOT NULL DEFAULT 0,
                    error TEXT,
                    wait_reason TEXT,
                    created_at REAL NOT NULL,
                    updated_at REAL NOT NULL,
                    next_retry_at REAL NOT NULL DEFAULT 0,
                    started_at REAL,
                    UNIQUE(chat_id, message_id)
                );
                CREATE INDEX IF NOT EXISTS idx_tasks_state_created
                    ON tasks(state, created_at);
                CREATE INDEX IF NOT EXISTS idx_tasks_retry
                    ON tasks(state, next_retry_at);
                """
            )
            columns = {
                row["name"]
                for row in self._conn.execute("PRAGMA table_info(tasks)").fetchall()
            }
            if "media_key" not in columns:
                self._conn.execute("ALTER TABLE tasks ADD COLUMN media_key TEXT")
            if "transfer_mode" not in columns:
                self._conn.execute(
                    "ALTER TABLE tasks ADD COLUMN transfer_mode TEXT "
                    "NOT NULL DEFAULT 'local'"
                )
            if "started_at" not in columns:
                # 第一次真正开始传输的时间：只发一次“开始传输”通知，并计算用时。
                self._conn.execute("ALTER TABLE tasks ADD COLUMN started_at REAL")
            for column in DESTINATION_COLUMNS:
                if column not in columns:
                    self._conn.execute(
                        f"ALTER TABLE tasks ADD COLUMN {column} TEXT"
                    )
            self._conn.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_tasks_destination
                ON tasks(destination_type, state)
                """
            )
            # 旧索引只按 (sender_id, media_key) 唯一，换网盘或换目录后重新转发
            # 同一个文件会被判成重复并退回旧目标的旧任务，导致新目标永远收不到。
            # 去重必须按完整目标绑定隔离，同时保留同一目标的去重。
            self._conn.execute("DROP INDEX IF EXISTS idx_tasks_sender_media")
            self._conn.execute(
                """
                CREATE UNIQUE INDEX IF NOT EXISTS idx_tasks_sender_media_destination
                ON tasks(
                    sender_id,
                    media_key,
                    COALESCE(destination_type, ''),
                    COALESCE(destination_target, ''),
                    COALESCE(destination_root, ''),
                    COALESCE(destination_account, '')
                )
                WHERE media_key IS NOT NULL
                """
            )

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    @staticmethod
    def _dict(row: sqlite3.Row | None) -> dict[str, Any] | None:
        return dict(row) if row is not None else None

    def create_task(
        self,
        *,
        chat_id: int,
        message_id: int,
        sender_id: int,
        media_key: str | None = None,
        file_name: str,
        file_size: int,
        destination: Mapping[str, Any] | None = None,
    ) -> tuple[dict[str, Any], bool]:
        now = time.time()
        binding = destination_values(destination)
        with self._lock:
            try:
                cursor = self._conn.execute(
                    """
                    INSERT INTO tasks (
                        chat_id, message_id, sender_id, media_key, file_name,
                        file_size, destination_type, destination_target,
                        destination_root, destination_account, state,
                        created_at, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'queued', ?, ?)
                    """,
                    (
                        chat_id,
                        message_id,
                        sender_id,
                        media_key,
                        file_name,
                        file_size,
                        binding["destination_type"] or None,
                        binding["destination_target"] or None,
                        binding["destination_root"] or None,
                        binding["destination_account"] or None,
                        now,
                        now,
                    ),
                )
                row = self._conn.execute(
                    "SELECT * FROM tasks WHERE id = ?", (cursor.lastrowid,)
                ).fetchone()
                return dict(row), True
            except sqlite3.IntegrityError:
                row = self._conn.execute(
                    "SELECT * FROM tasks WHERE chat_id = ? AND message_id = ?",
                    (chat_id, message_id),
                ).fetchone()
                if row is None and media_key:
                    # 只有同一目标绑定内的媒体才算重复；换网盘/目录后重新转发
                    # 同一个文件必须能建立新任务。
                    row = self._conn.execute(
                        """
                        SELECT * FROM tasks
                        WHERE sender_id = ? AND media_key = ?
                          AND COALESCE(destination_type, '') = ?
                          AND COALESCE(destination_target, '') = ?
                          AND COALESCE(destination_root, '') = ?
                          AND COALESCE(destination_account, '') = ?
                        ORDER BY id ASC
                        LIMIT 1
                        """,
                        (
                            sender_id,
                            media_key,
                            binding["destination_type"],
                            binding["destination_target"],
                            binding["destination_root"],
                            binding["destination_account"],
                        ),
                    ).fetchone()
                if row is None:
                    raise
                return dict(row), False

    def get(self, task_id: int) -> dict[str, Any] | None:
        with self._lock:
            return self._dict(
                self._conn.execute(
                    "SELECT * FROM tasks WHERE id = ?", (task_id,)
                ).fetchone()
            )

    def list_states(
        self,
        states: Iterable[str],
        *,
        limit: int = 200,
        ready_only: bool = False,
        after_id: int = 0,
        destination: Mapping[str, Any] | None = None,
    ) -> list[dict[str, Any]]:
        state_values = tuple(states)
        if not state_values:
            return []
        placeholders = ", ".join("?" for _ in state_values)
        # placeholders are generated only from the tuple length; values stay bound.
        query = f"SELECT * FROM tasks WHERE state IN ({placeholders})"  # nosec B608
        params: list[Any] = list(state_values)
        if destination is not None:
            clause, clause_params = _destination_sql(destination)
            query += f" AND {clause}"  # nosec B608
            params.extend(clause_params)
        if after_id > 0:
            query += " AND id > ?"
            params.append(after_id)
        if ready_only:
            query += " AND next_retry_at <= ?"
            params.append(time.time())
        query += " ORDER BY id ASC LIMIT ?"
        params.append(limit)
        with self._lock:
            return [
                dict(row)
                for row in self._conn.execute(query, params).fetchall()
            ]

    def list_recent(self, limit: int = 20) -> list[dict[str, Any]]:
        with self._lock:
            return [
                dict(row)
                for row in self._conn.execute(
                    "SELECT * FROM tasks ORDER BY id DESC LIMIT ?", (limit,)
                ).fetchall()
            ]

    def counts(
        self, destination: Mapping[str, Any] | None = None
    ) -> dict[str, int]:
        query = "SELECT state, COUNT(*) AS count FROM tasks"
        params: list[Any] = []
        if destination is not None:
            clause, clause_params = _destination_sql(destination)
            query += f" WHERE {clause}"  # nosec B608
            params.extend(clause_params)
        query += " GROUP BY state"
        with self._lock:
            return {
                row["state"]: row["count"]
                for row in self._conn.execute(query, params).fetchall()
            }

    def pending_mismatch_count(
        self, destination: Mapping[str, Any] | None
    ) -> int:
        """Count unfinished tasks that belong to another destination."""
        if destination is None:
            return 0
        placeholders = ", ".join("?" for _ in PENDING_STATES)
        clause, clause_params = _destination_sql(destination)
        query = (
            f"SELECT COUNT(*) AS count FROM tasks "  # nosec B608
            f"WHERE state IN ({placeholders}) AND NOT COALESCE({clause}, 0)"
        )
        params: list[Any] = list(PENDING_STATES) + clause_params
        with self._lock:
            row = self._conn.execute(query, params).fetchone()
            return int(row["count"]) if row is not None else 0

    def bind_unbound_tasks(
        self, destination: Mapping[str, Any] | None
    ) -> int:
        """Adopt tasks created before destination binding existed.

        Only called by the service for the legacy CloudDrive2 destination so an
        upgrade keeps resuming its own unfinished tasks. A task without a
        stored type can only have come from the CloudDrive2-only builds, so
        any other destination refuses to claim it instead of relocating
        old work.
        """
        binding = destination_values(destination)
        if binding["destination_type"] != LEGACY_DESTINATION:
            return 0
        with self._lock:
            cursor = self._conn.execute(
                """
                UPDATE tasks
                SET destination_type = ?, destination_target = ?,
                    destination_root = ?, destination_account = ?
                WHERE destination_type IS NULL OR destination_type = ''
                """,
                (
                    binding["destination_type"],
                    binding["destination_target"] or None,
                    binding["destination_root"] or None,
                    binding["destination_account"] or None,
                ),
            )
            return int(cursor.rowcount or 0)

    def adopt_legacy_account(
        self, destination: Mapping[str, Any] | None
    ) -> int:
        """Stamp the account fingerprint on tasks recorded before it existed.

        Older builds stored the destination type/target/root but no account
        identity. Only rows whose type, target and root already match the
        current destination are upgraded, so this can never move work onto a
        different disk; tasks bound to another account keep their own value and
        stay blocked.
        """
        binding = destination_values(destination)
        if binding["destination_type"] != LEGACY_DESTINATION:
            # 只有升级前的 CloudDrive2 目标可以补写指纹。Google Drive 任务来自
            # 旧版直连实现，补写会把它们悄悄改成 CD2 任务，必须拒绝。
            return 0
        if not binding["destination_account"]:
            return 0
        with self._lock:
            cursor = self._conn.execute(
                """
                UPDATE tasks
                SET destination_account = ?
                WHERE destination_type = ?
                  AND COALESCE(destination_target, '') = ?
                  AND COALESCE(destination_root, '') = ?
                  AND (destination_account IS NULL OR destination_account = '')
                """,
                (
                    binding["destination_account"],
                    binding["destination_type"],
                    binding["destination_target"],
                    binding["destination_root"],
                ),
            )
            return int(cursor.rowcount or 0)

    def used_local_bytes(self) -> int:
        placeholders = ",".join("?" for _ in LOCAL_STATES)
        local_sum_query = (
            "SELECT COALESCE(SUM(file_size), 0) AS total "  # nosec B608
            f"FROM tasks WHERE state IN ({placeholders}) "
            "AND transfer_mode != 'stream'"
        )
        with self._lock:
            row = self._conn.execute(
                local_sum_query,
                LOCAL_STATES,
            ).fetchone()
            return int(row["total"])

    def reserve(
        self,
        task_id: int,
        *,
        budget_bytes: int,
        current_free_bytes: int,
        minimum_free_bytes: int,
        always_stream: bool = False,
    ) -> tuple[bool, str]:
        """放行一个排队任务。

        流式任务不占本地预算，但仍受真实磁盘安全线约束（CloudDrive2 会缓存）。
        ``always_stream`` 对应 TRANSFER_MODE=stream：所有文件都走流式。
        """
        placeholders = ",".join("?" for _ in LOCAL_STATES)
        local_sum_query = (
            "SELECT COALESCE(SUM(file_size), 0) AS total "  # nosec B608
            f"FROM tasks WHERE state IN ({placeholders}) "
            "AND transfer_mode != 'stream'"
        )
        growth_placeholders = ",".join("?" for _ in GROWING_STATES)
        growth_query = (
            "SELECT COALESCE(SUM(CASE "  # nosec B608
            "WHEN file_size > downloaded_bytes "
            "THEN file_size - downloaded_bytes ELSE 0 END), 0) AS total "
            f"FROM tasks WHERE state IN ({growth_placeholders}) "
            "AND transfer_mode != 'stream'"
        )
        with self._lock:
            self._conn.execute("BEGIN IMMEDIATE")
            try:
                task = self._conn.execute(
                    "SELECT * FROM tasks WHERE id = ?", (task_id,)
                ).fetchone()
                if task is None or task["state"] != "queued":
                    self._conn.execute("ROLLBACK")
                    return False, "任务状态已经改变"
                row = self._conn.execute(
                    local_sum_query,
                    LOCAL_STATES,
                ).fetchone()
                used = int(row["total"])
                growth_row = self._conn.execute(
                    growth_query,
                    GROWING_STATES,
                ).fetchone()
                pending_growth = int(growth_row["total"])
                size = int(task["file_size"])
                if always_stream or size > budget_bytes:
                    if current_free_bytes <= minimum_free_bytes:
                        reason = "等待真实磁盘空间恢复后使用流式模式"
                        self._conn.execute(
                            """
                            UPDATE tasks SET wait_reason = ?, updated_at = ?
                            WHERE id = ?
                            """,
                            (reason, time.time(), task_id),
                        )
                        self._conn.execute("COMMIT")
                        return False, reason
                    self._conn.execute(
                        """
                        UPDATE tasks
                        SET state = 'reserved', transfer_mode = 'stream',
                            wait_reason = NULL, error = NULL, updated_at = ?
                        WHERE id = ?
                        """,
                        (time.time(), task_id),
                    )
                    self._conn.execute("COMMIT")
                    if always_stream:
                        return True, "TRANSFER_MODE=stream，使用流式模式"
                    return True, "单文件超过本地预算，已切换流式模式"
                if used + size > budget_bytes:
                    reason = "等待本地临时空间额度"
                    self._conn.execute(
                        """
                        UPDATE tasks SET wait_reason = ?, updated_at = ?
                        WHERE id = ?
                        """,
                        (reason, time.time(), task_id),
                    )
                    self._conn.execute("COMMIT")
                    return False, reason
                if (
                    current_free_bytes - pending_growth - size
                    < minimum_free_bytes
                ):
                    self._conn.execute(
                        """
                        UPDATE tasks SET wait_reason = ?, updated_at = ?
                        WHERE id = ?
                        """,
                        ("等待真实磁盘空间", time.time(), task_id),
                    )
                    self._conn.execute("COMMIT")
                    return False, "等待真实磁盘空间"
                self._conn.execute(
                    """
                    UPDATE tasks
                    SET state = 'reserved', transfer_mode = 'local',
                        wait_reason = NULL, error = NULL, updated_at = ?
                    WHERE id = ?
                    """,
                    (time.time(), task_id),
                )
                self._conn.execute("COMMIT")
                return True, ""
            except Exception:
                self._conn.execute("ROLLBACK")
                raise

    def update(self, task_id: int, **fields: Any) -> None:
        allowed = {
            "state",
            "transfer_mode",
            "local_path",
            "remote_path",
            "downloaded_bytes",
            "uploaded_bytes",
            "download_retries",
            "upload_retries",
            "error",
            "wait_reason",
            "next_retry_at",
            "started_at",
        }
        unknown = set(fields) - allowed
        if unknown:
            raise ValueError(f"不允许更新字段：{sorted(unknown)}")
        fields["updated_at"] = time.time()
        assignments = ", ".join(f"{key} = ?" for key in fields)
        values = list(fields.values()) + [task_id]
        with self._lock:
            self._conn.execute(
                # assignments contains names from the fixed allowed set above.
                f"UPDATE tasks SET {assignments} WHERE id = ?",  # nosec B608
                values,
            )

    def confirm_115(
        self, task_id: int
    ) -> tuple[str, dict[str, Any] | None]:
        """Atomically record a user's official-client confirmation.

        Only a completed Bot transfer can be confirmed. This prevents an
        accidental /confirm from changing a task that is still transferring.
        """
        with self._lock:
            self._conn.execute("BEGIN IMMEDIATE")
            try:
                row = self._conn.execute(
                    "SELECT * FROM tasks WHERE id = ?", (task_id,)
                ).fetchone()
                if row is None:
                    self._conn.execute("ROLLBACK")
                    return "missing", None
                if row["state"] == "confirmed":
                    self._conn.execute("ROLLBACK")
                    return "already", dict(row)
                if row["state"] != "completed":
                    self._conn.execute("ROLLBACK")
                    return "invalid", dict(row)
                self._conn.execute(
                    """
                    UPDATE tasks
                    SET state='confirmed', error=NULL, wait_reason=NULL,
                        next_retry_at=0, updated_at=?
                    WHERE id=? AND state='completed'
                    """,
                    (time.time(), task_id),
                )
                updated = self._conn.execute(
                    "SELECT * FROM tasks WHERE id = ?", (task_id,)
                ).fetchone()
                self._conn.execute("COMMIT")
                return "confirmed", dict(updated)
            except Exception:
                self._conn.execute("ROLLBACK")
                raise

    def confirm_delivery(
        self, task_id: int
    ) -> tuple[str, dict[str, Any] | None]:
        """Destination-neutral alias for :meth:`confirm_115`.

        The recorded confirmation means "the user saw the finished file in the
        destination's own client"; the historical method name is kept so older
        callers and tests keep working.
        """
        return self.confirm_115(task_id)

    def recover(self, download_dir: Path) -> dict[str, int]:
        recovered = {"queued": 0, "waiting_upload": 0, "failed": 0}
        with self._lock:
            rows = self._conn.execute(
                """
                SELECT * FROM tasks
                WHERE state IN (
                    'reserved', 'downloading', 'downloaded', 'waiting_upload',
                    'uploading', 'verifying', 'finalizing', 'streaming',
                    'upload_failed_retained', 'verification_failed_retained'
                )
                """
            ).fetchall()
            for row in rows:
                task_id = int(row["id"])
                local_path = Path(row["local_path"]) if row["local_path"] else None
                if row["transfer_mode"] == "stream":
                    self._conn.execute(
                        """
                        UPDATE tasks SET state='queued', local_path=NULL,
                            downloaded_bytes=0, upload_retries=0,
                            wait_reason='流式任务在服务重启后自动重新排队',
                            next_retry_at=0, updated_at=?
                        WHERE id=?
                        """,
                        (time.time(), task_id),
                    )
                    recovered["queued"] += 1
                    continue
                if (
                    local_path
                    and local_path.is_file()
                    and not str(local_path).endswith(".part")
                    and local_path.stat().st_size == int(row["file_size"])
                ):
                    self._conn.execute(
                        """
                        UPDATE tasks SET state='waiting_upload', error=NULL,
                            upload_retries=0, next_retry_at=0,
                            wait_reason='服务重启后恢复上传', updated_at=?
                        WHERE id=?
                        """,
                        (time.time(), task_id),
                    )
                    recovered["waiting_upload"] += 1
                    continue
                expected_final = download_dir / (
                    f"{task_id}-"
                    f"{safe_file_name(row['file_name'], str(task_id))}"
                )
                if (
                    expected_final.is_file()
                    and expected_final.stat().st_size == int(row["file_size"])
                ):
                    self._conn.execute(
                        """
                        UPDATE tasks SET state='waiting_upload',
                            local_path=?, downloaded_bytes=file_size,
                            error=NULL, upload_retries=0, next_retry_at=0,
                            wait_reason='已恢复下载完成但未登记的本地文件',
                            updated_at=?
                        WHERE id=?
                        """,
                        (str(expected_final), time.time(), task_id),
                    )
                    recovered["waiting_upload"] += 1
                    continue
                if row["state"] in {
                    "upload_failed_retained",
                    "verification_failed_retained",
                }:
                    self._conn.execute(
                        """
                        UPDATE tasks SET state='queued', local_path=NULL,
                            downloaded_bytes=0, download_retries=0,
                            upload_retries=0,
                            wait_reason='保留记录对应的本地文件已不存在，自动重新排队',
                            next_retry_at=0, updated_at=?
                        WHERE id=?
                        """,
                        (time.time(), task_id),
                    )
                    recovered["queued"] += 1
                    continue
                part_path = download_dir / f"{task_id}.part"
                if part_path.exists():
                    try:
                        part_path.unlink()
                    except OSError:
                        self._conn.execute(
                            """
                            UPDATE tasks SET state='download_failed',
                                error='重启后无法清理不完整文件', updated_at=?
                            WHERE id=?
                            """,
                            (time.time(), task_id),
                        )
                        recovered["failed"] += 1
                        continue
                self._conn.execute(
                    """
                    UPDATE tasks SET state='queued', local_path=NULL,
                        downloaded_bytes=0, download_retries=0,
                        upload_retries=0,
                        wait_reason='本地文件不完整或不存在，服务重启后重新排队',
                        updated_at=?
                    WHERE id=?
                    """,
                    (time.time(), task_id),
                )
                recovered["queued"] += 1
        return recovered
