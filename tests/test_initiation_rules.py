import unittest
from datetime import date

from src.initiation_rules import (evaluate, funding_gap,
                                  changed_protected_fields, peak_overlap_headcount,
                                  suggest_windows, windows_overlap)


def proj(**over):
    base = {"id": 1, "title": "甲号楼", "owner_unit": "甲单位",
            "owner_consent": True, "estimated_cost": 1000,
            "available_fund": 1000, "start_date": "2026-10-01",
            "end_date": "2026-10-31", "resettlement_people": 30,
            "zone_code": "Z-A"}
    base.update(over)
    return base


class InitiationRulesTest(unittest.TestCase):
    def test_funding_gap(self):
        self.assertEqual(funding_gap(900, 600), 300)
        self.assertEqual(funding_gap(500, 600), -100)

    def test_consent_and_fund_blockers_keep_pending(self):
        missing_consent = evaluate(proj(owner_consent=False), 100, [])
        self.assertFalse(missing_consent["approvable"])
        self.assertIn("产权单位尚未签署加固同意书",
                      "；".join(missing_consent["blockers"]))

        short = evaluate(proj(estimated_cost=900, available_fund=600), 100, [])
        self.assertFalse(short["approvable"])
        self.assertTrue(any("资金缺口300" in b for b in short["blockers"]))

    def test_overlap_and_capacity(self):
        self.assertTrue(windows_overlap(date(2026, 10, 1), date(2026, 10, 31),
                                        date(2026, 10, 20), date(2026, 11, 5)))
        self.assertFalse(windows_overlap(date(2026, 10, 1), date(2026, 10, 31),
                                         date(2026, 10, 31), date(2026, 11, 5)))
        approved = [proj(id=2, start_date="2026-10-10", end_date="2026-10-20",
                         resettlement_people=60)]
        result = evaluate(proj(resettlement_people=41), 100, approved)
        self.assertFalse(result["approvable"])
        self.assertEqual(result["overlap_peak"], 60)
        self.assertTrue(any("峰值101人" in b and "超过容量100人" in b
                            for b in result["blockers"]))

    def test_peak_counts_only_true_overlap(self):
        approved = [
            proj(id=2, start_date="2026-09-01", end_date="2026-09-30",
                 resettlement_people=90),
            proj(id=3, start_date="2026-10-15", end_date="2026-10-20",
                 resettlement_people=40),
        ]
        peak = peak_overlap_headcount(proj(), approved)
        self.assertEqual(peak, 40)

    def test_suggested_windows_fit_capacity(self):
        approved = [proj(id=2, start_date="2026-10-01", end_date="2026-11-30",
                         resettlement_people=80)]
        suggestions = suggest_windows(proj(resettlement_people=30), 100,
                                      approved)
        self.assertTrue(suggestions)
        first = suggestions[0]
        self.assertEqual((date.fromisoformat(first["end_date"])
                          - date.fromisoformat(first["start_date"])).days, 30)
        self.assertFalse(windows_overlap(
            date.fromisoformat(first["start_date"]),
            date.fromisoformat(first["end_date"]),
            date(2026, 10, 1), date(2026, 11, 30)))

    def test_no_suggestion_when_project_exceeds_capacity(self):
        self.assertEqual(suggest_windows(proj(resettlement_people=120), 100,
                                         []), [])

    def test_unregistered_zone_blocks(self):
        result = evaluate(proj(), None, [])
        self.assertFalse(result["approvable"])
        self.assertTrue(any("容量未登记" in b for b in result["blockers"]))

    def test_changed_protected_fields_detects_any_approval_value(self):
        before = proj()
        after = proj(estimated_cost=1200)
        self.assertEqual(changed_protected_fields(before, after),
                         ["estimated_cost"])


if __name__ == "__main__":
    unittest.main()
