"""Real-rclone tests that never touch a real cloud account.

Two levels, both driven through the same ``RcloneClient`` the application uses:

* ``[cd2] type = alias`` stands in for the CloudDrive2 WebDAV mount, so the
  normal, streaming, rename and delete paths run as real subprocesses.
* A local ``rclone serve webdav`` instance stands in for CloudDrive2 itself, so
  ``ensure_config`` really creates the ``cd2`` WebDAV remote and the transfers
  really go over HTTP with WebDAV authentication.

Neither level proves anything about the 115 or Google Drive backends: those are
mounted inside CloudDrive2 by the user and are out of reach of these tests.

Run with TG115_RCLONE_EXE pointing at a verified rclone binary.
"""
from __future__ import annotations

import asyncio
import os
import socket
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "payload"))
from app.rclone_client import RcloneClient
from app.verify_destination import verify_destination

RCLONE = os.environ.get("TG115_RCLONE_EXE", "")


def free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.bind(("127.0.0.1", 0))
        return int(probe.getsockname()[1])


def launch_kwargs() -> dict[str, object]:
    if os.name == "nt":
        return {"creationflags": subprocess.CREATE_NO_WINDOW}
    return {}


def cd2_settings(
    root: Path, config: Path, destination: str, target: str
) -> SimpleNamespace:
    """Settings shaped exactly like the one config.Settings builds.

    Both destinations carry the same CD2 credentials; only the relative target
    path differs, which is what the deployer writes into CD2_TARGET_PATH_B64.
    """
    return SimpleNamespace(
        destination_type=destination,
        destination_label="Google Drive" if destination == "gdrive" else "115",
        destination_target=target,
        cd2_target=target,
        cd2_url="http://127.0.0.1:1/dav",
        cd2_user="cd2-user",
        cd2_password="cd2-password",  # pragma: allowlist secret
        rclone_config_path=config,
        data_dir=root,
    )


@unittest.skipUnless(RCLONE, "set TG115_RCLONE_EXE for real local rclone tests")
class RealRcloneAliasTests(unittest.TestCase):
    """The client's file operations, with an alias standing in for CD2."""

    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.remote = self.root / "remote"
        self.remote.mkdir()
        self.config = self.root / "rclone.conf"
        self.config.write_text(
            "[cd2]\ntype = alias\nremote = " + self.remote.as_posix() + "\n",
            encoding="utf-8",
        )
        # The application calls the executable by name. Redirect just this
        # subprocess call to the explicitly supplied, verified binary.
        original = asyncio.create_subprocess_exec

        async def launch(program: str, *args: str, **kwargs):
            self.assertEqual(program, "rclone")
            kwargs.update(launch_kwargs())
            return await original(RCLONE, *args, **kwargs)

        self.patcher = patch(
            "app.rclone_client.asyncio.create_subprocess_exec", launch
        )
        self.patcher.start()
        self.addCleanup(self.patcher.stop)

    def client(self, destination: str, target: str) -> RcloneClient:
        settings = cd2_settings(self.root, self.config, destination, target)
        client = RcloneClient(settings)
        # The alias already is the remote; creating a WebDAV remote would point
        # at a server that does not exist in this test.
        client._config_ready = True
        return client

    def test_copy_verify_rename_and_delete_for_both_destinations(self) -> None:
        for destination, target in (
            ("clouddrive2", "115/测试目录"),
            ("gdrive", "GoogleDrive/测试目录"),
        ):
            with self.subTest(destination=destination):
                client = self.client(destination, target)
                self.assertTrue(client.remote("x").startswith("cd2:"))

                async def scenario(client=client, target=target, destination=destination) -> None:
                    await client.prepare_destination()
                    data = bytes(range(256)) * 17
                    source = self.root / f"source-{destination}.bin"
                    source.write_bytes(data)
                    await client.upload(source, ".uploading-file.bin")
                    self.assertEqual(
                        await client.remote_size(".uploading-file.bin"), len(data)
                    )
                    await client.move(".uploading-file.bin", "已完成.bin")
                    self.assertFalse(await client.exists(".uploading-file.bin"))
                    self.assertTrue(await client.exists("已完成.bin"))
                    self.assertEqual(
                        (self.remote / target / "已完成.bin").read_bytes(), data
                    )
                    await client.remove("已完成.bin")
                    self.assertFalse(await client.exists("已完成.bin"))

                asyncio.run(scenario())

    def test_rcat_stream_and_zero_byte_file_for_both_destinations(self) -> None:
        for destination, target in (
            ("clouddrive2", "115/流式"),
            ("gdrive", "GoogleDrive/流式"),
        ):
            with self.subTest(destination=destination):
                client = self.client(destination, target)

                async def scenario(client=client, target=target, destination=destination) -> None:
                    await client.prepare_destination()
                    chunks = [
                        b"first chunk",
                        bytes(range(256)) * 32,
                        b"last chunk",
                    ]
                    stream = await client.open_upload_stream(
                        "stream.bin", sum(map(len, chunks))
                    )
                    for chunk in chunks:
                        await stream.write(chunk)
                    await stream.finish()
                    self.assertEqual(
                        (self.remote / target / "stream.bin").read_bytes(),
                        b"".join(chunks),
                    )
                    empty = await client.open_upload_stream("empty.bin", 0)
                    await empty.finish()
                    self.assertEqual(await client.remote_size("empty.bin"), 0)

                asyncio.run(scenario())

    def test_destination_verifier_cleans_its_real_probe_files(self) -> None:
        target = "GoogleDrive/校验"
        settings = cd2_settings(self.root, self.config, "gdrive", target)
        client = self.client("gdrive", target)
        result = asyncio.run(verify_destination(settings, client))
        self.assertTrue(result.endswith(".ok"))
        self.assertEqual(list((self.remote / target).iterdir()), [])
        self.assertEqual(list(self.root.glob(".tg115-verify-*")), [])


@unittest.skipUnless(RCLONE, "set TG115_RCLONE_EXE for real local rclone tests")
class RealRcloneWebdavTests(unittest.TestCase):
    """``ensure_config`` plus transfers against a local WebDAV server.

    This is the closest local stand-in for CloudDrive2: the ``cd2`` remote is
    created by rclone itself as ``type = webdav`` and every operation goes over
    HTTP with WebDAV authentication.
    """

    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.storage = self.root / "webdav-root"
        self.storage.mkdir()
        self.config = self.root / "rclone.conf"
        self.port = free_port()
        self.url = f"http://127.0.0.1:{self.port}/"
        self.server = subprocess.Popen(
            [
                RCLONE,
                "serve",
                "webdav",
                self.storage.as_posix(),
                "--addr",
                f"127.0.0.1:{self.port}",
                "--user",
                "cd2-user",
                "--pass",
                "cd2-password",
                "--log-level",
                "ERROR",
            ],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            **launch_kwargs(),
        )
        self.addCleanup(self._stop_server)
        self._wait_for_server()
        # Same redirect as the alias tests: the application spawns ``rclone`` by
        # name, the test supplies the verified binary.
        original = asyncio.create_subprocess_exec

        async def launch(program: str, *args: str, **kwargs):
            self.assertEqual(program, "rclone")
            kwargs.update(launch_kwargs())
            return await original(RCLONE, *args, **kwargs)

        self.patcher = patch(
            "app.rclone_client.asyncio.create_subprocess_exec", launch
        )
        self.patcher.start()
        self.addCleanup(self.patcher.stop)

    def _stop_server(self) -> None:
        if self.server.poll() is None:
            self.server.terminate()
            try:
                self.server.wait(timeout=10)
            except subprocess.TimeoutExpired:
                self.server.kill()
                self.server.wait(timeout=10)

    def _wait_for_server(self) -> None:
        deadline = time.monotonic() + 30
        while time.monotonic() < deadline:
            if self.server.poll() is not None:
                self.fail("rclone serve webdav 提前退出")
            with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
                probe.settimeout(0.5)
                if probe.connect_ex(("127.0.0.1", self.port)) == 0:
                    return
            time.sleep(0.2)
        self.fail("rclone serve webdav 未在 30 秒内就绪")

    def client(self, destination: str, target: str) -> RcloneClient:
        settings = SimpleNamespace(
            destination_type=destination,
            destination_label="Google Drive" if destination == "gdrive" else "115",
            destination_target=target,
            cd2_target=target,
            cd2_url=self.url,
            cd2_user="cd2-user",
            cd2_password="cd2-password",  # pragma: allowlist secret
            rclone_config_path=self.config,
            data_dir=self.root,
        )
        return RcloneClient(settings)

    def test_webdav_remote_is_created_and_used_for_both_destinations(self) -> None:
        for destination, target in (
            ("clouddrive2", "115/Telegram"),
            ("gdrive", "GoogleDrive/Telegram"),
        ):
            with self.subTest(destination=destination):
                client = self.client(destination, target)

                async def scenario(client=client, target=target, destination=destination) -> None:
                    await client.ensure_config()
                    self.assertTrue(await client.healthy())
                    data = b"webdav-payload" * 512
                    source = self.root / f"webdav-{destination}.bin"
                    source.write_bytes(data)
                    await client.upload(source, "uploading.bin")
                    self.assertEqual(
                        await client.remote_size("uploading.bin"), len(data)
                    )
                    await client.move("uploading.bin", "final.bin")
                    self.assertEqual(
                        (self.storage / target / "final.bin").read_bytes(), data
                    )
                    stream = await client.open_upload_stream(
                        "stream.bin", len(data)
                    )
                    await stream.write(data)
                    await stream.finish()
                    self.assertEqual(
                        (self.storage / target / "stream.bin").read_bytes(), data
                    )
                    await client.remove("final.bin")
                    await client.remove("stream.bin")
                    self.assertFalse(await client.exists("final.bin"))

                asyncio.run(scenario())

                text = self.config.read_text(encoding="utf-8")
                self.assertIn("[cd2]", text)
                self.assertIn("type = webdav", text)
                self.assertNotIn("[gdrive]", text)
                self.assertNotIn("type = drive", text)


if __name__ == "__main__":
    unittest.main()
