"""Regression coverage for the target-switch boundaries found during review.

Both UI options transfer through CloudDrive2, so the binding is the CD2
endpoint/user plus the selected relative path; only the final netdisk type
separates 115 from Google Drive.
"""
from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "payload"))
from app.db import TaskDB, destination_matches
from app.main import DESTINATION_CLOUDDRIVE2, DESTINATION_GDRIVE, TransferService


def binding(
    target: str = "",
    root: str = "",
    account: str = "clouddrive2:account-one",
    destination_type: str = DESTINATION_GDRIVE,
) -> dict[str, str]:
    return {
        "destination_type": destination_type,
        "destination_target": target,
        "destination_root": root,
        "destination_account": account,
    }


def service_for(destination_type: str, target: str = "") -> TransferService:
    service = TransferService.__new__(TransferService)
    service.settings = SimpleNamespace(
        destination_type=destination_type,
        destination_target=target,
        cd2_target=target,
        cd2_url="http://clouddrive2:19798/dav",
        cd2_user="owner",
        cd2_password="local-test-password",
    )
    return service


class DestinationSwitchRegressionTests(unittest.TestCase):
    def test_empty_root_and_path_are_bound_values(self) -> None:
        original = binding()
        self.assertTrue(destination_matches(original, original))
        self.assertFalse(destination_matches(original, binding(root="folder-two")))
        self.assertFalse(destination_matches(original, binding(target="another/path")))

    def test_final_netdisk_is_part_of_the_binding(self) -> None:
        google = binding()
        legacy = binding(destination_type=DESTINATION_CLOUDDRIVE2)
        # 同一个 CD2 端点、同一条路径，只换最终网盘也必须隔离。
        self.assertFalse(destination_matches(google, legacy))
        self.assertFalse(destination_matches(legacy, google))

    def test_sql_filter_matches_the_same_exact_binding_rule(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            db = TaskDB(Path(temp) / "tasks.db")
            try:
                task, _ = db.create_task(
                    chat_id=1, message_id=1, sender_id=1,
                    file_name="one.bin", file_size=1, destination=binding(),
                )
                self.assertEqual(
                    [
                        row["id"]
                        for row in db.list_states(["queued"], destination=binding())
                    ],
                    [task["id"]],
                )
                self.assertEqual(
                    db.list_states(["queued"], destination=binding(root="another-root")),
                    [],
                )
                self.assertEqual(
                    db.list_states(["queued"], destination=binding(target="another-path")),
                    [],
                )
                self.assertEqual(
                    db.list_states(
                        ["queued"],
                        destination=binding(
                            destination_type=DESTINATION_CLOUDDRIVE2
                        ),
                    ),
                    [],
                )
            finally:
                db.close()

    def test_same_media_may_be_transferred_once_per_destination(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            db = TaskDB(Path(temp) / "tasks.db")
            try:
                common = {
                    "chat_id": 1, "sender_id": 1, "media_key": "document:42",
                    "file_name": "one.bin", "file_size": 1,
                }
                first, created = db.create_task(
                    message_id=1, destination=binding(target="folder-one"), **common
                )
                self.assertTrue(created)
                duplicate, created = db.create_task(
                    message_id=2, destination=binding(target="folder-one"), **common
                )
                self.assertFalse(created)
                self.assertEqual(duplicate["id"], first["id"])
                other, created = db.create_task(
                    message_id=3, destination=binding(target="folder-two"), **common
                )
                self.assertTrue(
                    created,
                    "a previous destination must not consume the new destination's "
                    "duplicate key",
                )
                self.assertNotEqual(other["id"], first["id"])
                switched, created = db.create_task(
                    message_id=4,
                    destination=binding(
                        target="folder-one",
                        destination_type=DESTINATION_CLOUDDRIVE2,
                    ),
                    **common,
                )
                self.assertTrue(
                    created, "the other netdisk on the same CD2 must be separate"
                )
                self.assertNotEqual(switched["id"], first["id"])
            finally:
                db.close()

    def test_both_netdisks_bind_the_clouddrive2_identity(self) -> None:
        google = service_for(DESTINATION_GDRIVE, "telegram")._binding()
        legacy = service_for(DESTINATION_CLOUDDRIVE2, "telegram")._binding()
        for current in (google, legacy):
            self.assertEqual(current["destination_target"], "telegram")
            self.assertEqual(current["destination_root"], "")
            self.assertTrue(current["destination_account"].startswith("clouddrive2:"))
            self.assertNotIn("local-test-password", str(current))
        self.assertEqual(
            google["destination_account"], legacy["destination_account"]
        )
        self.assertNotEqual(
            google["destination_type"], legacy["destination_type"]
        )

    def test_identity_follows_the_cd2_endpoint_and_user(self) -> None:
        service = service_for(DESTINATION_GDRIVE, "telegram")
        original = service._binding()
        service.settings.cd2_password = "another-password"
        self.assertEqual(service._binding(), original)
        service.settings.cd2_url = "http://other-nas:19798/dav"
        self.assertNotEqual(service._binding(), original)
        service.settings.cd2_url = "http://clouddrive2:19798/dav"
        service.settings.cd2_user = "another-user"
        self.assertNotEqual(service._binding(), original)


if __name__ == "__main__":
    unittest.main()
