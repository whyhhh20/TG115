"""上传到 VPS 的部署包不能夹带本地字节码缓存（__pycache__ / *.pyc）。"""
from __future__ import annotations

import compileall
import shutil
import sys
import tarfile
import tempfile
import unittest
from pathlib import Path

SOURCE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SOURCE))

import installer

PAYLOAD = SOURCE / "payload"


def archive_names(payload: Path, root: Path) -> list[str]:
    archive = root / "payload.tar.gz"
    installer.build_payload_archive(payload, archive)
    with tarfile.open(archive, "r:gz") as tar:
        return tar.getnames()


class PayloadArchiveTests(unittest.TestCase):
    def test_bytecode_caches_are_left_out(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            payload = root / "payload"
            (payload / "app" / "__pycache__").mkdir(parents=True)
            (payload / "app" / "main.py").write_text("x = 1\n", encoding="utf-8")
            (payload / "app" / "__pycache__" / "main.cpython-313.pyc").write_bytes(b"\0")
            (payload / "app" / "stray.pyc").write_bytes(b"\0")
            (payload / "app" / "legacy.pyo").write_bytes(b"\0")
            (payload / "__pycache__").mkdir()
            (payload / "__pycache__" / "x.pyc").write_bytes(b"\0")
            (payload / "remote_install.sh").write_text("#!/bin/bash\n", encoding="utf-8")

            names = archive_names(payload, root)

        self.assertIn("payload/app/main.py", names)
        self.assertIn("payload/remote_install.sh", names)
        self.assertFalse([n for n in names if "__pycache__" in n], names)
        self.assertFalse([n for n in names if n.endswith((".pyc", ".pyo"))], names)

    def test_real_payload_archive_has_no_bytecode_even_after_compiling(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            payload = root / "payload"
            shutil.copytree(PAYLOAD, payload)
            self.assertTrue(compileall.compile_dir(payload / "app", quiet=1))
            self.assertTrue(list(payload.rglob("*.pyc")), "precondition: caches exist")

            names = archive_names(payload, root)

        self.assertIn("payload/remote_install.sh", names)
        self.assertIn("payload/app/main.py", names)
        self.assertIn("payload/.dockerignore", names)
        self.assertFalse([n for n in names if "__pycache__" in n or n.endswith(".pyc")])

    def test_filter_keeps_ordinary_files(self) -> None:
        for name in ("payload", "payload/app/pycache_helper.py", "payload/app/notes.pyc.txt"):
            info = tarfile.TarInfo(name)
            self.assertIs(installer.payload_tar_filter(info), info, name)

    def test_deploy_uses_the_filtered_archive_builder(self) -> None:
        source = (SOURCE / "installer.py").read_text(encoding="utf-8")
        deploy = source[source.index("    def deploy(self"):]
        deploy = deploy[: deploy.index("\n    def ", 1)]
        self.assertIn("build_payload_archive(payload, archive)", deploy)
        self.assertNotIn("tar.add(", deploy)


class DockerignoreTests(unittest.TestCase):
    def test_build_context_drops_bytecode_after_reincluding_app(self) -> None:
        lines = (PAYLOAD / ".dockerignore").read_text(encoding="utf-8").splitlines()
        reinclude = lines.index("!app/**")
        for pattern in ("**/__pycache__", "**/*.pyc", "**/*.pyo"):
            with self.subTest(pattern=pattern):
                self.assertIn(pattern, lines)
                # .dockerignore 后写的规则优先：必须写在 !app/** 之后才生效。
                self.assertGreater(lines.index(pattern), reinclude)


if __name__ == "__main__":
    unittest.main()
