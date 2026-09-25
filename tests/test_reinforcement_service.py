import tempfile
import unittest
from pathlib import Path

from src.domain import ConflictError, PermissionDenied, ValidationError
from src.repository import Repository
from src.service import Service


def payload(**overrides):
    data = dict(
        building_name="1号楼", owner_name="产权甲", owner_consent=False,
        estimate=1_000_000, funds_available=800_000, zone="A",
        construction_start="2026-10-01", construction_end="2026-11-30",
        resettlement_count=50,
    )
    data.update(overrides)
    return data


class ReinforcementServiceTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.repo = Repository(str(Path(self.tmp.name) / "test.db"))
        self.service = Service(self.repo)

    def tearDown(self):
        self.repo.close()
        self.tmp.cleanup()

    def test_missing_consent_or_funds_stays_pending(self):
        project = self.service.create_reinforcement(payload(), "u1", "assessor")
        self.assertEqual(project["status"], "pending")
        self.assertFalse(project["approvable"])
        self.assertIn("owner_consent", project["blocker_codes"])
        self.assertIn("funding", project["blocker_codes"])
        self.assertEqual(project["funding_gap"], 200_000.0)
        with self.assertRaises(ConflictError):
            self.service.approve_reinforcement(
                project["id"], project["version"], "board", "review_board")

    def test_only_review_board_approves_and_zone_role(self):
        project = self.service.create_reinforcement(
            payload(owner_consent=True, funds_available=1_000_000), "u1", "assessor")
        with self.assertRaises(PermissionDenied):
            self.service.approve_reinforcement(
                project["id"], project["version"], "u1", "assessor")
        with self.assertRaises(PermissionDenied):
            self.service.register_zone({"zone": "A", "capacity": 60},
                                       "u1", "assessor")

    def test_approve_after_all_blockers_cleared(self):
        project = self.service.create_reinforcement(payload(), "u1", "assessor")
        self.service.register_zone({"zone": "A", "capacity": 60},
                                   "board", "review_board")
        project = self.service.update_reinforcement(project["id"], {
            "owner_consent": True, "funds_available": 1_000_000,
            "expected_version": project["version"],
        }, "u1", "assessor")
        approved = self.service.approve_reinforcement(
            project["id"], project["version"], "board", "review_board")
        self.assertEqual(approved["status"], "approved")
        self.assertIsNotNone(approved["approval_snapshot"])
        self.assertEqual(approved["approved_by"], "board")

    def test_capacity_conflict_returns_adjustable_window(self):
        self.service.register_zone({"zone": "A", "capacity": 60},
                                   "board", "review_board")
        first = self.service.create_reinforcement(
            payload(owner_consent=True, funds_available=1_000_000),
            "u1", "assessor")
        first = self.service.approve_reinforcement(
            first["id"], first["version"], "board", "review_board")

        second = self.service.create_reinforcement(
            payload(owner_consent=True, funds_available=1_000_000),
            "u2", "assessor")
        self.assertIn("capacity", second["blocker_codes"])
        self.assertIsNotNone(second["suggested_window"])
        with self.assertRaises(ConflictError) as ctx:
            self.service.approve_reinforcement(
                second["id"], second["version"], "board", "review_board")
        self.assertIsNotNone(ctx.exception.details["suggested_window"])

        window = second["suggested_window"]
        second = self.service.update_reinforcement(second["id"], {
            "expected_version": second["version"], **window,
        }, "u2", "assessor")
        second = self.service.approve_reinforcement(
            second["id"], second["version"], "board", "review_board")
        self.assertEqual(second["status"], "approved")

    def test_approval_voided_after_guarded_change_and_reapproved(self):
        self.service.register_zone({"zone": "A", "capacity": 60},
                                   "board", "review_board")
        project = self.service.create_reinforcement(
            payload(owner_consent=True, funds_available=1_000_000),
            "u1", "assessor")
        project = self.service.approve_reinforcement(
            project["id"], project["version"], "board", "review_board")

        # 估价上浮 -> 原批复失效，回到待立项，资金卡点重新出现
        changed = self.service.update_reinforcement(project["id"], {
            "estimate": 1_300_000, "expected_version": project["version"],
        }, "u1", "assessor")
        self.assertEqual(changed["status"], "pending")
        self.assertIsNone(changed["approval_snapshot"])
        self.assertIsNotNone(changed["voided_at"])
        self.assertIn("estimate", changed["voided_reason"])
        self.assertIn("funding", changed["blocker_codes"])

        # 版本过期的改动被拒绝
        with self.assertRaises(ConflictError):
            self.service.update_reinforcement(project["id"], {
                "funds_available": 1_300_000, "expected_version": project["version"],
            }, "u1", "assessor")

        changed = self.service.update_reinforcement(project["id"], {
            "funds_available": 1_300_000,
            "expected_version": changed["version"],
        }, "u1", "assessor")
        reapproved = self.service.approve_reinforcement(
            changed["id"], changed["version"], "board", "review_board")
        self.assertEqual(reapproved["status"], "approved")
        self.assertTrue(self.repo.verify_audit_chain())

    def test_rename_does_not_void_approval(self):
        self.service.register_zone({"zone": "A", "capacity": 60},
                                   "board", "review_board")
        project = self.service.create_reinforcement(
            payload(owner_consent=True, funds_available=1_000_000),
            "u1", "assessor")
        project = self.service.approve_reinforcement(
            project["id"], project["version"], "board", "review_board")
        changed = self.service.update_reinforcement(project["id"], {
            "building_name": "1号楼（加固）", "expected_version": project["version"],
        }, "u1", "assessor")
        self.assertEqual(changed["status"], "approved")
        self.assertIsNone(changed["voided_at"])

    def test_validation_errors(self):
        with self.assertRaises(ValidationError):
            self.service.create_reinforcement(
                payload(construction_end="2026-09-01"), "u1", "assessor")
        with self.assertRaises(ValidationError):
            self.service.create_reinforcement(
                payload(resettlement_count=-3), "u1", "assessor")
        with self.assertRaises(ValidationError):
            self.service.create_reinforcement(
                payload(owner_consent="yes"), "u1", "assessor")

    def test_list_shows_blockers(self):
        self.service.create_reinforcement(payload(), "u1", "assessor")
        items = self.service.list_reinforcement("viewer")
        self.assertEqual(len(items), 1)
        self.assertTrue(items[0]["blockers"])


if __name__ == "__main__":
    unittest.main()
