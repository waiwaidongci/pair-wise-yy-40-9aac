import tempfile
import unittest
from datetime import date, timedelta
from pathlib import Path

from src.domain import ConflictError, PermissionDenied, ValidationError
from src.initiation_service import DecisionConflict, InitiationService
from src.repository import Repository


def at(offset):
    return (date.today() + timedelta(days=offset)).isoformat()


def payload(**over):
    base = {"title": "甲号楼加固", "owner_unit": "甲产权单位",
            "owner_consent": True, "estimated_cost": 800000,
            "available_fund": 800000, "start_date": at(10),
            "end_date": at(40), "resettlement_people": 30,
            "zone_code": "Z-A", "external_ref": "T-1"}
    base.update(over)
    return base


class InitiationWorkflowTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.repo = Repository(str(Path(self.tmp.name) / "test.db"))
        self.svc = InitiationService(self.repo)
        self.svc.upsert_zone({"zone_code": "Z-A", "capacity": 100},
                             "demo", "review_board")

    def tearDown(self):
        self.repo.close()
        self.tmp.cleanup()

    def test_register_blocked_then_approve_after_fix(self):
        created = self.svc.register(payload(owner_consent=False,
                                            external_ref="T-2"),
                                    "clerk", "initiator")
        self.assertEqual(created["status"], "pending")
        self.assertFalse(created["approvable"])
        self.assertTrue(created["blockers"])
        with self.assertRaises(DecisionConflict):
            self.svc.approve(created["id"], created["version"], "board",
                             "review_board")
        fixed = self.svc.revise(created["id"],
                                {"expected_version": created["version"],
                                 "owner_consent": True},
                                "clerk", "initiator")
        approved = self.svc.approve(fixed["id"], fixed["version"], "board",
                                    "review_board")
        self.assertEqual(approved["status"], "approved")
        self.assertTrue(approved["approval"]["valid"])
        self.assertEqual(len(self.svc.list_approvals("viewer",
                                                      approved["id"])), 1)

    def test_funding_gap_keeps_pending(self):
        created = self.svc.register(payload(estimated_cost=900000,
                                            available_fund=600000,
                                            external_ref="T-3"),
                                    "clerk", "initiator")
        self.assertEqual(created["funding_gap"], 300000)
        self.assertFalse(created["approvable"])

    def test_capacity_conflict_returns_adjustable_windows(self):
        first = self.svc.register(payload(resettlement_people=80,
                                          start_date=at(10), end_date=at(40),
                                          external_ref="T-4"),
                                  "clerk", "initiator")
        self.svc.approve(first["id"], first["version"], "board",
                         "review_board")
        second = self.svc.register(payload(resettlement_people=40,
                                           start_date=at(20), end_date=at(30),
                                           external_ref="T-5"),
                                   "clerk", "initiator")
        self.assertFalse(second["approvable"])
        with self.assertRaises(DecisionConflict) as caught:
            self.svc.approve(second["id"], second["version"], "board",
                             "review_board")
        suggestions = caught.exception.window_suggestions
        self.assertTrue(suggestions)
        chosen = suggestions[0]
        moved = self.svc.revise(second["id"], {
            "expected_version": second["version"],
            "start_date": chosen["start_date"],
            "end_date": chosen["end_date"]}, "clerk", "initiator")
        approved = self.svc.approve(moved["id"], moved["version"], "board",
                                    "review_board")
        self.assertEqual(approved["status"], "approved")

    def test_changing_approval_values_invalidates_and_requires_reapproval(self):
        created = self.svc.register(payload(external_ref="T-6"), "clerk",
                                    "initiator")
        approved = self.svc.approve(created["id"], created["version"], "board",
                                    "review_board")
        revised = self.svc.revise(approved["id"], {
            "expected_version": approved["version"],
            "estimated_cost": 1200000}, "clerk", "initiator")
        self.assertEqual(revised["status"], "pending")
        self.assertFalse(revised["approval"]["valid"])
        self.assertIn("estimated_cost",
                      revised["approval"]["drifted_fields"])
        records = self.svc.list_approvals("viewer", approved["id"])
        self.assertEqual(len(records), 1)
        self.assertFalse(records[0]["valid"])
        self.assertIn("estimated_cost", records[0]["invalidation_reason"])
        view = self.svc.get_project(approved["id"], "viewer")
        self.assertFalse(view["approvable"])

    def test_non_protected_change_keeps_approval(self):
        created = self.svc.register(payload(external_ref="T-7"), "clerk",
                                    "initiator")
        approved = self.svc.approve(created["id"], created["version"], "board",
                                    "review_board")
        revised = self.svc.revise(approved["id"], {
            "expected_version": approved["version"],
            "title": "甲号楼加固（更名）"}, "clerk", "initiator")
        self.assertEqual(revised["status"], "approved")
        self.assertTrue(revised["approval"]["valid"])

    def test_version_conflict_and_permissions(self):
        created = self.svc.register(payload(external_ref="T-8"), "clerk",
                                    "initiator")
        with self.assertRaises(ConflictError):
            self.svc.revise(created["id"],
                            {"expected_version": created["version"] + 5,
                             "title": "x"}, "clerk", "initiator")
        with self.assertRaises(PermissionDenied):
            self.svc.approve(created["id"], created["version"], "spy",
                             "viewer")
        with self.assertRaises(PermissionDenied):
            self.svc.register(payload(external_ref="T-9"), "spy", "viewer")

    def test_date_order_validation(self):
        with self.assertRaises(ValidationError):
            self.svc.register(payload(start_date=at(40), end_date=at(10),
                                      external_ref="T-10"),
                              "clerk", "initiator")

    def test_audit_chain_intact(self):
        created = self.svc.register(payload(external_ref="T-11"), "clerk",
                                    "initiator")
        self.svc.approve(created["id"], created["version"], "board",
                         "review_board")
        self.assertTrue(self.repo.verify_audit_chain())


if __name__ == "__main__":
    unittest.main()
