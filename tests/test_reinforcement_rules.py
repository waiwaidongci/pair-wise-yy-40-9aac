import unittest

from src import reinforcement_rules as rr
from src.domain import ValidationError


def approved_project(pid, start, end, count, zone="A"):
    return {
        "id": pid, "status": rr.APPROVED, "zone": zone,
        "construction_start": start, "construction_end": end,
        "resettlement_count": count,
    }


def pending_project(start="2026-10-01", end="2026-11-30", count=50,
                    pid=99, consent=True, estimate=100.0, funds=100.0,
                    zone="A", owner="产权甲"):
    return {
        "id": pid, "status": rr.PENDING, "zone": zone, "owner_name": owner,
        "owner_consent": consent, "estimate": estimate,
        "funds_available": funds, "construction_start": start,
        "construction_end": end, "resettlement_count": count,
    }


class ReinforcementRulesTest(unittest.TestCase):
    def test_funding_gap(self):
        self.assertEqual(rr.funding_gap(120, 100), 20.0)
        self.assertEqual(rr.funding_gap(100, 130), 0.0)

    def test_consent_and_funding_blockers(self):
        result = rr.evaluate_project(
            pending_project(consent=False, estimate=200, funds=150),
            100, [])
        self.assertEqual(result["blocker_codes"], ["owner_consent", "funding"])
        self.assertEqual(result["funding_gap"], 50.0)
        self.assertFalse(result["approvable"])

    def test_zone_missing_is_blocker(self):
        result = rr.evaluate_project(pending_project(), None, [])
        self.assertIn("capacity", result["blocker_codes"])
        self.assertIsNone(result["suggested_window"])

    def test_capacity_overlap_and_suggested_window(self):
        # 已批 60 人占满容量，新项目 50 人落在同一窗口
        existing = approved_project(1, "2026-09-15", "2026-11-15", 60)
        result = rr.evaluate_project(pending_project(count=50), 60, [existing])
        self.assertEqual(result["blocker_codes"], ["capacity"])
        window = result["suggested_window"]
        self.assertIsNotNone(window)
        self.assertGreater(window["construction_start"], "2026-11-15")
        # 建议窗口与在批项目首尾相接，不再超载
        self.assertFalse(rr.windows_overlap(
            window["construction_start"], window["construction_end"],
            "2026-09-15", "2026-11-15"))

    def test_overlap_uses_closed_intervals(self):
        self.assertTrue(rr.windows_overlap("2026-10-01", "2026-10-10",
                                           "2026-10-10", "2026-10-20"))
        self.assertFalse(rr.windows_overlap("2026-10-01", "2026-10-10",
                                            "2026-10-11", "2026-10-20"))

    def test_no_feasible_window_when_single_project_exceeds_capacity(self):
        result = rr.evaluate_project(pending_project(count=80), 60, [])
        self.assertIn("capacity", result["blocker_codes"])
        self.assertIsNone(result["suggested_window"])

    def test_clean_window_is_approvable(self):
        result = rr.evaluate_project(pending_project(), 60, [])
        self.assertEqual(result["blocker_codes"], [])
        self.assertTrue(result["approvable"])

    def test_excludes_self_from_occupancy(self):
        # 列表里混入自己（批准后重算场景），不应把自己算进叠加人数
        me = approved_project(99, "2026-10-01", "2026-11-30", 50)
        me["status"] = rr.APPROVED
        project = pending_project()
        project["status"] = rr.APPROVED
        result = rr.evaluate_project(project, 60, [me])
        self.assertNotIn("capacity", result["blocker_codes"])

    def test_window_validation(self):
        with self.assertRaises(ValidationError):
            rr.parse_window({"construction_start": "2026-11-01",
                             "construction_end": "2026-10-01"})
        with self.assertRaises(ValidationError):
            rr.parse_window({"construction_start": "10/01/2026",
                             "construction_end": "2026-10-10"})


if __name__ == "__main__":
    unittest.main()
