"""Use the installed Paramiko API signatures, not permissive fake kwargs."""
from __future__ import annotations

import shlex
import unittest
from unittest.mock import Mock, create_autospec

import paramiko

import installer
from tests.test_installer_ssh_stream import FakeChannel, make_session


class ParamikoApiCompatibilityTests(unittest.TestCase):
    def test_sudo_wraps_compound_command_in_one_shell(self) -> None:
        command = "cd /opt/tg115 && printf '%s' '$HOME'; printf done"
        channel = FakeChannel([])
        make_session(channel).run(command, sudo=True)
        actual = channel.exec_calls[0][0]
        self.assertEqual(
            shlex.split(actual),
            ["sudo", "-S", "-p", "", "--", "sh", "-c", command],
        )

    def test_remote_setup_uses_real_channel_method_signatures(self) -> None:
        for sudo in (False, True):
            with self.subTest(sudo=sudo):
                channel = paramiko.Channel(1)
                channel.exit_status = 0
                get_pty = create_autospec(channel.get_pty, return_value=None)
                exec_command = create_autospec(channel.exec_command, return_value=None)
                channel.get_pty = get_pty
                channel.exec_command = exec_command
                channel.sendall = create_autospec(channel.sendall, return_value=None)
                channel.send = create_autospec(channel.send, return_value=7)
                channel.recv_ready = Mock(return_value=False)
                channel.recv_stderr_ready = Mock(return_value=False)
                channel.exit_status_ready = Mock(return_value=True)
                channel.recv_exit_status = Mock(return_value=0)
                channel.close = Mock()
                transport = Mock()
                transport.is_active.return_value = True
                transport.open_session.return_value = channel
                session = installer.RemoteSession.__new__(installer.RemoteSession)
                session.client = Mock()
                session.client.get_transport.return_value = transport
                session.values = {"sudo_password": "fixture-password"}
                code, _ = session.run("printf ready", sudo=sudo, timeout=1)
                self.assertEqual(code, 0)
                get_pty.assert_called_once()
                exec_command.assert_called_once()
                channel.close.assert_called_once()


if __name__ == "__main__":
    unittest.main()
