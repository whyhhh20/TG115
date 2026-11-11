"""RemoteSession.run 的 SSH 输出流回归测试。

用确定性的 fake channel 复现三类现场故障：
1. 只有换行才交给 stream，CR 进度条/无换行输出在界面上表现为“假卡”；
2. 每个 recv 分片独立 decode，多字节 UTF-8 被切断后变成乱码；
3. 返回输出与单行缓冲无上限，长时间构建会持续涨内存；
另外覆盖默认有限超时、通道 EOF/断连检测、空闲提示与 channel 关闭。
"""

from __future__ import annotations

import threading
import unittest
from unittest import mock

import installer


class FakeTransport:
    def __init__(self, channel: FakeChannel, *, active: bool = True) -> None:
        self.channel = channel
        self.active = active
        self.open_calls: list[dict[str, object]] = []

    def is_active(self) -> bool:
        return self.active

    def open_session(self, **kwargs: object) -> FakeChannel:
        self.open_calls.append(kwargs)
        self.channel.open_kwargs = kwargs
        return self.channel


class FakeChannel:
    """按脚本吐字节的 channel；polls 上限保证 RED 阶段不会真的挂死。"""

    def __init__(
        self,
        chunks: list[bytes],
        *,
        exit_status: int = 0,
        stderr_chunks: list[bytes] | None = None,
        exit_ready_when_empty: bool = True,
        eof_when_empty: bool = False,
        delay_polls: int = 0,
        max_polls: int = 4000,
        block_pty: bool = False,
        on_recv=None,
    ) -> None:
        self.chunks = list(chunks)
        self.stderr_chunks = list(stderr_chunks or [])
        self.exit_status = exit_status
        self.exit_ready_when_empty = exit_ready_when_empty
        self.eof_when_empty = eof_when_empty
        self.delay_polls = delay_polls
        self.block_pty = block_pty
        self.unblock = threading.Event()
        self.settimeout_calls: list[object] = []
        self.max_polls = max_polls
        self.on_recv = on_recv
        self.polls = 0
        self.closed = False
        self.eof_received = False
        self.close_calls = 0
        self.exit_status_calls = 0
        self.exec_calls: list[tuple[str, object]] = []
        self.pty_kwargs: dict[str, object] = {}
        self.open_kwargs: dict[str, object] = {}
        self.sent: list[tuple[bytes, object]] = []

    # --- 建立阶段 ---
    def settimeout(self, timeout: object) -> None:
        self.settimeout_calls.append(timeout)

    def get_pty(self, **kwargs: object) -> None:
        self.pty_kwargs = kwargs
        if self.block_pty:
            self.unblock.wait(5)

    def exec_command(self, command: str, timeout: object = None) -> None:
        self.exec_calls.append((command, timeout))

    def sendall(self, data: bytes, timeout: object = None) -> None:
        self.sent.append((data, timeout))

    def send(self, data: bytes) -> None:
        self.sent.append((data, None))

    # --- 读取阶段 ---
    def recv_ready(self) -> bool:
        self.polls += 1
        if self.polls > self.max_polls:
            raise AssertionError(
                "RemoteSession.run 在无进展的情况下轮询超过上限，疑似死等"
            )
        if self.polls <= self.delay_polls:
            return False
        if not self.chunks and self.eof_when_empty:
            self.eof_received = True
        return bool(self.chunks)

    def recv(self, size: int) -> bytes:
        if self.chunks:
            chunk = self.chunks.pop(0)
            if self.on_recv is not None:
                self.on_recv(chunk)
            return chunk
        return b""

    def recv_stderr_ready(self) -> bool:
        return bool(self.stderr_chunks)

    def recv_stderr(self, size: int) -> bytes:
        if self.stderr_chunks:
            return self.stderr_chunks.pop(0)
        return b""

    def exit_status_ready(self) -> bool:
        # paramiko 在通道关闭时也会置位 status_event，即使没收到退出状态。
        return not self.chunks and (self.exit_ready_when_empty or self.eof_received)

    def recv_exit_status(self) -> int:
        self.exit_status_calls += 1
        return self.exit_status

    def close(self) -> None:
        self.closed = True
        self.close_calls += 1
        self.unblock.set()


def make_session(channel: FakeChannel, *, active: bool = True):
    session = installer.RemoteSession.__new__(installer.RemoteSession)
    session.values = {
        "vps_host": "127.0.0.1",
        "vps_port": "22",
        "vps_user": "tester",
        "auth_method": "密码",
        "vps_password": "secret",
        "sudo_password": "sudo-secret",
    }
    transport = FakeTransport(channel, active=active)
    session.client = mock.Mock()
    session.client.get_transport.return_value = transport
    return session


class RunStreamTests(unittest.TestCase):
    def test_cr_progress_is_streamed_before_newline(self) -> None:
        streamed: list[str] = []

        def on_recv(chunk: bytes) -> None:
            if chunk == b"Pulling 55%\r":
                self.assertIn(
                    "Pulling 10%",
                    streamed,
                    "CR 进度必须在下一块数据到达前就交给 stream，否则界面看起来卡死",
                )

        channel = FakeChannel(
            [b"Pulling 10%\r", b"Pulling 55%\r", b"Pulling 100%\n"],
            on_recv=on_recv,
        )
        code, output = make_session(channel).run("docker pull x", stream=streamed.append)

        self.assertEqual(code, 0)
        self.assertEqual(streamed, ["Pulling 10%", "Pulling 55%", "Pulling 100%"])
        self.assertIn("Pulling 100%", output)

    def test_multibyte_utf8_split_across_chunks_is_decoded(self) -> None:
        text = "安装完成 SUCCESS\n"
        data = text.encode("utf-8")
        cut = data.index("安".encode()) + 1
        channel = FakeChannel([data[:cut], data[cut : cut + 1], data[cut + 1 :]])
        streamed: list[str] = []

        code, output = make_session(channel).run("bash install.sh", stream=streamed.append)

        self.assertEqual(code, 0)
        self.assertNotIn("\ufffd", output)
        self.assertEqual(output, text)
        self.assertEqual(streamed, ["安装完成 SUCCESS"])

    def test_returned_output_is_bounded_but_keeps_tail_marker(self) -> None:
        chunks = [b"x" * 65536 for _ in range(8)] + [b"SUCCESS\n"]
        channel = FakeChannel(chunks)

        with mock.patch.object(installer, "SSH_OUTPUT_TAIL_LIMIT", 65536):
            code, output = make_session(channel).run("bash install.sh")

        self.assertEqual(code, 0)
        self.assertIn("SUCCESS", output)
        self.assertLessEqual(
            len(output),
            65536 + 65536,
            "返回输出必须保留有界尾部，而不是把整段构建日志留在内存里",
        )

    def test_single_line_without_newline_is_bounded(self) -> None:
        streamed: list[str] = []
        channel = FakeChannel([b"a" * 300000, b"SUCCESS\n"])

        with mock.patch.object(installer, "SSH_LINE_BUFFER_LIMIT", 4096):
            code, output = make_session(channel).run(
                "bash install.sh", stream=streamed.append
            )

        self.assertEqual(code, 0)
        self.assertTrue(streamed, "无换行的长输出必须被及时交出，不能一直攒在行缓冲里")
        self.assertIn("SUCCESS", output)

    def test_default_timeout_is_finite_and_closes_channel(self) -> None:
        channel = FakeChannel([], exit_ready_when_empty=False, max_polls=2000)
        session = make_session(channel)

        with (
            mock.patch.object(installer, "SSH_RUN_DEFAULT_TIMEOUT", 0.2),
            self.assertRaises(TimeoutError),
        ):
            session.run("bash install.sh")

        self.assertTrue(channel.closed, "超时后必须关闭 channel")
        self.assertEqual(channel.close_calls, 1)

    def test_channel_eof_without_exit_status_raises_and_closes(self) -> None:
        channel = FakeChannel(
            [],
            exit_status=-1,
            exit_ready_when_empty=False,
            eof_when_empty=True,
            max_polls=200,
        )

        with (
            mock.patch.object(installer, "SSH_EXIT_STATUS_GRACE", 0.05),
            self.assertRaises(RuntimeError) as ctx,
        ):
            make_session(channel).run("bash install.sh")

        self.assertIn("断开", str(ctx.exception))
        self.assertTrue(channel.closed)

    def test_idle_remote_emits_distinguishable_hint(self) -> None:
        streamed: list[str] = []
        channel = FakeChannel([b"done\n"], delay_polls=4)

        with mock.patch.object(installer, "SSH_IDLE_HINT_INTERVAL", 0.05):
            code, output = make_session(channel).run(
                "bash install.sh", stream=streamed.append
            )

        self.assertEqual(code, 0)
        hints = [line for line in streamed if "等待远端输出" in line]
        self.assertTrue(hints, "空闲时应该提示 SSH 仍连接，而不是让人以为远端已死")
        self.assertIn("SSH", hints[0])
        self.assertNotIn("等待远端输出", output, "提示不能混进命令输出")
        self.assertIn("done", streamed)

    def test_channel_operations_use_timeouts(self) -> None:
        channel = FakeChannel([b"ok\n"])
        session = make_session(channel)

        session.run("bash install.sh", sudo=True)

        self.assertIsNotNone(channel.open_kwargs.get("timeout"))
        self.assertEqual(
            channel.settimeout_calls,
            [installer.SSH_CHANNEL_OP_TIMEOUT],
            "paramiko 5 的 get_pty/exec_command 没有 timeout 参数，必须用 settimeout 限时",
        )
        self.assertTrue(channel.sent, "sudo 密码必须通过 sendall 发送")
        self.assertEqual(channel.sent[0][0], b"sudo-secret\n")

    def test_hung_pty_request_times_out_and_closes_channel(self) -> None:
        channel = FakeChannel([], block_pty=True)

        with (
            mock.patch.object(installer, "SSH_CHANNEL_OP_TIMEOUT", 0.2),
            self.assertRaises(TimeoutError),
        ):
            make_session(channel).run("bash install.sh")

        self.assertTrue(channel.closed, "PTY 请求卡住时必须关闭 channel")

    def test_run_keeps_tuple_return_and_command(self) -> None:
        channel = FakeChannel([b"SSH_OK\n"], exit_status=0)
        code, output = make_session(channel).run("printf 'SSH_OK\\n'")

        self.assertIsInstance(code, int)
        self.assertIsInstance(output, str)
        self.assertEqual(code, 0)
        self.assertIn("SSH_OK", output)
        self.assertEqual(channel.exec_calls[0][0], "printf 'SSH_OK\\n'")

    def test_inactive_transport_still_rejected(self) -> None:
        channel = FakeChannel([])
        session = make_session(channel, active=False)

        with self.assertRaises(RuntimeError):
            session.run("id -u")


if __name__ == "__main__":
    unittest.main()
