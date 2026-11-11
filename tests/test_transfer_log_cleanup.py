"""Transfer-log cleanup: bounded rclone output and per-task log removal.

The contract these tests pin down:

* ``logs/tg115.log`` is 2 MiB x 2 and is never cleared wholesale;
* each task owns at most one ``logs/transfers/<task_id>.log`` (1 MiB);
* rclone output is kept as a bounded tail, never through unbounded
  ``communicate()``, and the password is redacted before writing;
* a completed task deletes its own log immediately, and a failed delete is only
  a warning;
* startup drops leftovers of completed tasks and trims the directory to
  8 MiB / 10 files;
* task database rows are never deleted.
"""
from __future__ import annotations

import asyncio
import logging
import os
import subprocess
import sys
import tempfile
import unittest
from logging.handlers import RotatingFileHandler
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "payload"))

from app.main import TransferService, _task_log_kwargs, setup_logging
from app.rclone_client import (
    ERROR_TEXT_LIMIT,
    PROCESS_TAIL_LIMIT,
    RcloneClient,
    RcloneError,
    _read_process_tail,
)
from app.transfer_log import (
    TRANSFER_DIR_MAX_FILES,
    TRANSFER_LOG_MAX_BYTES,
    append_transfer_log,
    prune_transfers,
    remove_task_log,
    sweep_transfers,
    task_log_path,
    transfers_dir,
)

PASSWORD = "cd2-secret-pass"  # pragma: allowlist secret


def launch_kwargs() -> dict[str, object]:
    if os.name == "nt":
        return {"creationflags": subprocess.CREATE_NO_WINDOW}
    return {}


def rclone_settings(log_dir: Path, **overrides: object) -> SimpleNamespace:
    values: dict[str, object] = {
        "rclone_config_path": Path("rclone.conf"),
        "cd2_target": "",
        "cd2_password": PASSWORD,
        "log_dir": log_dir,
    }
    values.update(overrides)
    return SimpleNamespace(**values)


class TransferLogFileTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.log_dir = Path(self.temp.name) / "logs"
        self.log_dir.mkdir()

    def test_append_keeps_one_file_per_task_and_caps_it(self) -> None:
        chunk = "x" * (600 * 1024)
        append_transfer_log(self.log_dir, 7, chunk + "FIRST")
        append_transfer_log(self.log_dir, 7, chunk + "SECOND")
        path = append_transfer_log(self.log_dir, 7, chunk + "THIRD")

        self.assertIsNotNone(path)
        assert path is not None
        self.assertEqual(path, task_log_path(self.log_dir, 7))
        self.assertEqual(
            [item.name for item in transfers_dir(self.log_dir).iterdir()],
            ["7.log"],
        )
        self.assertLessEqual(path.stat().st_size, TRANSFER_LOG_MAX_BYTES)
        text = path.read_text(encoding="utf-8", errors="replace")
        self.assertIn("THIRD", text)
        self.assertNotIn("FIRST", text)

    def test_remove_deletes_the_file_and_tolerates_a_missing_one(self) -> None:
        append_transfer_log(self.log_dir, 3, "done")
        self.assertTrue(remove_task_log(self.log_dir, 3))
        self.assertFalse(task_log_path(self.log_dir, 3).exists())
        self.assertTrue(remove_task_log(self.log_dir, 3))

    def test_remove_failure_is_reported_without_raising(self) -> None:
        # A directory in place of the file cannot be unlinked on any platform.
        path = task_log_path(self.log_dir, 4)
        path.mkdir(parents=True)
        with self.assertLogs("tg115.transfer_log", level="WARNING"):
            self.assertFalse(remove_task_log(self.log_dir, 4))
        self.assertTrue(path.is_dir())

    def test_prune_keeps_only_the_newest_ten_files(self) -> None:
        directory = transfers_dir(self.log_dir)
        directory.mkdir(parents=True, exist_ok=True)
        for index in range(14):
            path = directory / f"{index}.log"
            path.write_text("x" * 10, encoding="utf-8")
            os.utime(path, (1_000 + index, 1_000 + index))

        removed = prune_transfers(self.log_dir)

        self.assertEqual(removed, 14 - TRANSFER_DIR_MAX_FILES)
        remaining = {item.name for item in directory.iterdir()}
        self.assertEqual(
            remaining, {f"{index}.log" for index in range(4, 14)}
        )

    def test_prune_respects_the_total_byte_budget(self) -> None:
        directory = transfers_dir(self.log_dir)
        directory.mkdir(parents=True, exist_ok=True)
        for index in range(3):
            path = directory / f"{index}.log"
            path.write_text("x" * (5 * 1024 * 1024), encoding="utf-8")
            os.utime(path, (1_000 + index, 1_000 + index))

        removed = prune_transfers(self.log_dir)

        self.assertEqual(removed, 2)
        self.assertEqual(
            [item.name for item in directory.iterdir()], ["2.log"]
        )

    def test_sweep_removes_completed_leftovers_and_keeps_unfinished(self) -> None:
        append_transfer_log(self.log_dir, 1, "completed task")
        append_transfer_log(self.log_dir, 2, "failed task")
        append_transfer_log(self.log_dir, 3, "unknown task")

        swept = sweep_transfers(
            self.log_dir, lambda task_id: task_id in {1, 3}
        )

        self.assertEqual(swept["completed"], 2)
        self.assertEqual(
            [item.name for item in transfers_dir(self.log_dir).iterdir()],
            ["2.log"],
        )
        self.assertTrue(task_log_path(self.log_dir, 2).exists())

    def test_sweep_keeps_the_file_when_the_task_state_is_unreadable(self) -> None:
        append_transfer_log(self.log_dir, 5, "still useful")

        def broken(_: int) -> bool:
            raise RuntimeError("database is locked")

        with self.assertLogs("tg115.transfer_log", level="WARNING"):
            swept = sweep_transfers(self.log_dir, broken)

        self.assertEqual(swept["completed"], 0)
        self.assertTrue(task_log_path(self.log_dir, 5).exists())

    def test_sweep_ignores_files_that_are_not_task_logs(self) -> None:
        directory = transfers_dir(self.log_dir)
        directory.mkdir(parents=True, exist_ok=True)
        (directory / "notes.txt").write_text("keep", encoding="utf-8")

        swept = sweep_transfers(self.log_dir, lambda _: True)

        self.assertEqual(swept["completed"], 0)
        self.assertTrue((directory / "notes.txt").exists())


class BoundedRcloneOutputTests(unittest.TestCase):
    """Real subprocesses, with ``rclone`` replaced by a python one-liner."""

    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.log_dir = Path(self.temp.name) / "logs"
        self.log_dir.mkdir()
        self.original = asyncio.create_subprocess_exec
        self.addCleanup(self._stop_patch)

    def _stop_patch(self) -> None:
        patcher = getattr(self, "patcher", None)
        if patcher is not None:
            patcher.stop()

    def patch_script(self, script: str, *, wait_for_ready: bool = False) -> None:
        original = self.original

        async def launch(program: str, *args: str, **kwargs: object):
            self.assertEqual(program, "rclone")
            kwargs.update(launch_kwargs())
            process = await original(
                sys.executable, "-c", script, **kwargs  # type: ignore[arg-type]
            )
            if wait_for_ready:
                # Start the timeout only after the child has flushed stderr;
                # Python startup under Windows load can exceed 0.2 seconds.
                assert process.stdout is not None
                try:
                    ready = await asyncio.wait_for(process.stdout.readline(), 10)
                    self.assertEqual(ready.strip(), b"READY")
                except (TimeoutError, asyncio.IncompleteReadError):
                    if process.returncode is None:
                        process.kill()
                    await process.wait()
                    raise
            return process

        self.patcher = patch(
            "app.rclone_client.asyncio.create_subprocess_exec", launch
        )
        self.patcher.start()

    def test_failure_keeps_only_the_tail_and_writes_the_task_log(self) -> None:
        self.patch_script(
            "import sys\n"
            "sys.stderr.write('A' * 200000)\n"
            "sys.stderr.write('TAIL-MARKER')\n"
            "sys.stderr.flush()\n"
            "sys.exit(1)\n"
        )
        client = RcloneClient(rclone_settings(self.log_dir))

        with self.assertRaises(RcloneError) as captured:
            asyncio.run(client._run("copyto", "a.bin", "cd2:a.bin", task_id=7))

        message = str(captured.exception)
        self.assertIn("TAIL-MARKER", message)
        self.assertLessEqual(len(message), ERROR_TEXT_LIMIT + 100)
        self.assertLess(len(message), 200000)

        path = task_log_path(self.log_dir, 7)
        self.assertTrue(path.is_file())
        self.assertLessEqual(path.stat().st_size, TRANSFER_LOG_MAX_BYTES)
        text = path.read_text(encoding="utf-8", errors="replace")
        self.assertIn("TAIL-MARKER", text)
        self.assertLess(len(text), 200000)

    def test_timeout_keeps_the_task_log(self) -> None:
        self.patch_script(
            "import sys, time\n"
            "time.sleep(0.3)\n"  # Startup may take longer than the transfer timeout.
            "sys.stderr.write('TIMEOUT-MARKER')\n"
            "sys.stderr.flush()\n"
            "sys.stdout.write('READY\\n')\n"
            "sys.stdout.flush()\n"
            "time.sleep(30)\n",
            wait_for_ready=True,
        )
        client = RcloneClient(rclone_settings(self.log_dir))

        with self.assertRaises(RcloneError):
            asyncio.run(
                client._run("copyto", "a.bin", "cd2:a.bin", timeout=0.2, task_id=9)
            )

        text = task_log_path(self.log_dir, 9).read_text(
            encoding="utf-8", errors="replace"
        )
        self.assertIn("TIMEOUT-MARKER", text)

    def test_password_is_redacted_before_it_reaches_the_log(self) -> None:
        self.patch_script(
            "import sys\n"
            f"sys.stderr.write('auth failed for {PASSWORD}')\n"
            "sys.exit(1)\n"
        )
        client = RcloneClient(rclone_settings(self.log_dir))

        with self.assertRaises(RcloneError):
            asyncio.run(client._run("copyto", "a.bin", "cd2:a.bin", task_id=8))

        text = task_log_path(self.log_dir, 8).read_text(
            encoding="utf-8", errors="replace"
        )
        self.assertNotIn(PASSWORD, text)
        self.assertIn("***", text)

    def test_run_without_task_id_writes_no_transfer_log(self) -> None:
        self.patch_script("import sys\nsys.stderr.write('probe')\n")
        client = RcloneClient(rclone_settings(self.log_dir))

        code, _, err = asyncio.run(client._run("lsd", "cd2:", task_id=None))

        self.assertEqual(code, 0)
        self.assertIn("probe", err)
        self.assertFalse(transfers_dir(self.log_dir).exists())

    def test_upload_without_task_id_keeps_the_old_call_shape(self) -> None:
        self.patch_script("import sys\nsys.stderr.write('copied')\n")
        client = RcloneClient(rclone_settings(self.log_dir))
        local = Path(self.temp.name) / "movie.bin"
        local.write_bytes(b"1234")

        asyncio.run(client.upload(local, ".uploading-movie.bin"))

        self.assertFalse(transfers_dir(self.log_dir).exists())

    def test_upload_with_task_id_writes_the_task_log(self) -> None:
        self.patch_script("import sys\nsys.stderr.write('copied 4 bytes')\n")
        client = RcloneClient(rclone_settings(self.log_dir))
        local = Path(self.temp.name) / "movie.bin"
        local.write_bytes(b"1234")

        asyncio.run(
            client.upload(local, ".uploading-movie.bin", task_id=11)
        )

        text = task_log_path(self.log_dir, 11).read_text(
            encoding="utf-8", errors="replace"
        )
        self.assertIn("copied 4 bytes", text)

    def test_stream_finish_writes_the_task_log(self) -> None:
        client = RcloneClient(rclone_settings(self.log_dir))
        client._config_ready = True

        class Stdin:
            def write(self, data: bytes) -> None:
                return None

            async def drain(self) -> None:
                return None

            def close(self) -> None:
                return None

            async def wait_closed(self) -> None:
                return None

        class Process:
            def __init__(self) -> None:
                self.stdin = Stdin()
                self.stdout = asyncio.StreamReader()
                self.stderr = asyncio.StreamReader()
                self.stdout.feed_eof()
                self.stderr.feed_data(b"rcat progress line")
                self.stderr.feed_eof()
                self.returncode = None

            async def wait(self) -> int:
                self.returncode = 0
                return 0

        async def scenario() -> None:
            process = Process()
            spawn = AsyncMock(return_value=process)
            with patch(
                "app.rclone_client.asyncio.create_subprocess_exec", new=spawn
            ):
                stream = await client.open_upload_stream(
                    "large.bin", 3, task_id=12
                )
                await stream.write(b"abc")
                await stream.finish()

        asyncio.run(scenario())
        text = task_log_path(self.log_dir, 12).read_text(
            encoding="utf-8", errors="replace"
        )
        self.assertIn("rcat progress line", text)

    def test_stream_failure_message_stays_bounded(self) -> None:
        client = RcloneClient(rclone_settings(self.log_dir))
        client._config_ready = True

        class Stdin:
            def write(self, data: bytes) -> None:
                return None

            async def drain(self) -> None:
                return None

            def close(self) -> None:
                return None

            async def wait_closed(self) -> None:
                return None

        class Process:
            def __init__(self) -> None:
                self.stdin = Stdin()
                self.stdout = asyncio.StreamReader()
                self.stderr = asyncio.StreamReader()
                self.stdout.feed_eof()
                self.stderr.feed_data(b"B" * 200000 + b"STREAM-TAIL")
                self.stderr.feed_eof()
                self.returncode = None

            async def wait(self) -> int:
                self.returncode = 1
                return 1

        async def scenario() -> None:
            process = Process()
            spawn = AsyncMock(return_value=process)
            with patch(
                "app.rclone_client.asyncio.create_subprocess_exec", new=spawn
            ):
                stream = await client.open_upload_stream(
                    "large.bin", 3, task_id=13
                )
                await stream.write(b"abc")
                await stream.finish()

        with self.assertRaises(RcloneError) as captured:
            asyncio.run(scenario())
        self.assertIn("STREAM-TAIL", str(captured.exception))
        self.assertLessEqual(len(str(captured.exception)), ERROR_TEXT_LIMIT + 100)
        text = task_log_path(self.log_dir, 13).read_text(
            encoding="utf-8", errors="replace"
        )
        self.assertIn("STREAM-TAIL", text)
        self.assertLess(len(text), 200000)

    def test_tail_reader_keeps_only_the_last_chunks(self) -> None:
        async def scenario() -> str:
            reader = asyncio.StreamReader()
            reader.feed_data(b"C" * (PROCESS_TAIL_LIMIT + 5000))
            reader.feed_data(b"END")
            reader.feed_eof()
            return await _read_process_tail(reader)

        text = asyncio.run(scenario())
        self.assertTrue(text.endswith("END"))
        self.assertEqual(len(text), PROCESS_TAIL_LIMIT)


class FakeDB:
    def __init__(self, tasks: dict[int, dict[str, object]]) -> None:
        self.tasks = tasks

    def get(self, task_id: int) -> dict[str, object] | None:
        task = self.tasks.get(task_id)
        return dict(task) if task is not None else None

    def update(self, task_id: int, **fields: object) -> None:
        self.tasks.setdefault(task_id, {}).update(fields)


class ServiceTransferLogTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.log_dir = self.root / "logs"
        self.log_dir.mkdir()
        self.settings = SimpleNamespace(
            log_dir=self.log_dir,
            max_retries=1,
            destination_type="clouddrive2",
            destination_target="telegram",
            cd2_target="telegram",
        )

    def service(self, task: dict[str, object]) -> TransferService:
        service = TransferService.__new__(TransferService)
        service.settings = self.settings
        service.db = FakeDB({21: task, 22: {}})  # type: ignore[assignment]
        service.log = Mock()
        service._notify = AsyncMock()
        return service

    @staticmethod
    def task() -> dict[str, object]:
        return {
            "id": 21,
            "file_name": "video.mp4",
            "file_size": 4,
            "destination_type": "clouddrive2",
            "state": "verifying",
        }

    def test_completed_task_deletes_its_transfer_log(self) -> None:
        append_transfer_log(self.log_dir, 21, "rclone copyto finished")
        service = self.service(self.task())

        asyncio.run(
            service._complete_cloud_receive(
                21, self.task(), None, "video.mp4"
            )
        )

        self.assertEqual(service.db.tasks[21]["state"], "completed")  # type: ignore[attr-defined]
        self.assertFalse(task_log_path(self.log_dir, 21).exists())
        # The task row is kept for de-duplication and /confirm.
        self.assertEqual(service.db.tasks[21]["id"], 21)  # type: ignore[attr-defined]

    def test_delete_failure_only_warns_and_keeps_the_task_completed(self) -> None:
        service = self.service(self.task())

        with patch("app.main.remove_task_log", return_value=False):
            asyncio.run(
                service._complete_cloud_receive(
                    21, self.task(), None, "video.mp4"
                )
            )

        self.assertEqual(service.db.tasks[21]["state"], "completed")  # type: ignore[attr-defined]
        service.log.warning.assert_called_once()

    def test_real_delete_failure_keeps_the_task_completed(self) -> None:
        blocked = task_log_path(self.log_dir, 21)
        blocked.mkdir(parents=True)
        service = self.service(self.task())

        with self.assertLogs("tg115.transfer_log", level="WARNING"):
            asyncio.run(
                service._complete_cloud_receive(
                    21, self.task(), None, "video.mp4"
                )
            )

        self.assertEqual(service.db.tasks[21]["state"], "completed")  # type: ignore[attr-defined]
        self.assertTrue(blocked.is_dir())

    def test_startup_sweep_drops_completed_leftovers(self) -> None:
        append_transfer_log(self.log_dir, 21, "completed leftover")
        append_transfer_log(self.log_dir, 22, "failed attempt")
        service = self.service(self.task())
        service.db.tasks[21]["state"] = "completed"  # type: ignore[attr-defined]
        service.db.tasks[22]["state"] = "upload_failed_retained"  # type: ignore[attr-defined]

        service._sweep_transfer_logs()

        self.assertFalse(task_log_path(self.log_dir, 21).exists())
        self.assertTrue(task_log_path(self.log_dir, 22).exists())

    def test_startup_sweep_is_skipped_without_a_log_dir(self) -> None:
        service = self.service(self.task())
        service.settings = SimpleNamespace(max_retries=1)

        service._sweep_transfer_logs()

        self.assertFalse(self.log_dir.joinpath("transfers").exists())


class LoggingSetupTests(unittest.TestCase):
    def test_shared_log_is_two_megabytes_by_two_files(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            settings = SimpleNamespace(log_dir=Path(temp) / "logs")
            root = logging.getLogger()
            saved_handlers = root.handlers[:]
            saved_level = root.level
            try:
                setup_logging(settings)  # type: ignore[arg-type]
                rotating = [
                    handler
                    for handler in root.handlers
                    if isinstance(handler, RotatingFileHandler)
                ]
                self.assertEqual(len(rotating), 1)
                self.assertEqual(rotating[0].maxBytes, 2 * 1024**2)
                self.assertEqual(rotating[0].backupCount, 2)
                self.assertEqual(
                    Path(rotating[0].baseFilename),
                    Path(temp) / "logs" / "tg115.log",
                )
            finally:
                for handler in root.handlers[:]:
                    if handler not in saved_handlers:
                        handler.close()
                root.handlers[:] = saved_handlers
                root.setLevel(saved_level)


class TaskLogKwargsTests(unittest.TestCase):
    def test_task_id_is_passed_only_when_supported(self) -> None:
        class Legacy:
            async def upload(self, local_path: Path, remote: str) -> None:
                return None

        class Modern:
            async def upload(
                self, local_path: Path, remote: str, *, task_id: int | None = None
            ) -> None:
                return None

        class Permissive:
            async def upload(self, *args: object, **kwargs: object) -> None:
                return None

        self.assertEqual(_task_log_kwargs(Legacy().upload, 5), {})
        self.assertEqual(_task_log_kwargs(Modern().upload, 5), {"task_id": 5})
        self.assertEqual(
            _task_log_kwargs(Permissive().upload, 5), {"task_id": 5}
        )


if __name__ == "__main__":
    unittest.main()
