import json
import unittest

from bot import compose
from tests.test_decision import DATA, seed_items


def ctx(trigger_id):
    trigger = next(t for t in seed_items("triggers_seed.json", "triggers") if t["id"] == trigger_id)
    merchant = next(m for m in seed_items("merchants_seed.json", "merchants") if m["merchant_id"] == trigger["merchant_id"])
    category = json.loads((DATA / "categories" / f"{merchant['category_slug']}.json").read_text())
    customer = next((c for c in seed_items("customers_seed.json", "customers")
                     if c["customer_id"] == trigger.get("customer_id")), None)
    return category, merchant, trigger, customer


class ComposeTest(unittest.TestCase):
    def test_contract_keys_and_determinism(self):
        for tid in ("trg_001_research_digest_dentists", "trg_003_recall_due_priya", "trg_010_ipl_match_delhi"):
            with self.subTest(trigger=tid):
                first = compose(*ctx(tid))
                self.assertEqual(sorted(first), ["body", "cta", "rationale", "send_as", "suppression_key"])
                self.assertTrue(first["body"])
                self.assertEqual(first, compose(*ctx(tid)))

    def test_customer_facing_when_customer_given(self):
        self.assertEqual(compose(*ctx("trg_003_recall_due_priya"))["send_as"], "merchant_on_behalf")

    def test_restraint_is_explicit(self):
        category, merchant, trigger, _ = ctx("trg_008_curious_ask_studio11")
        fake = dict(trigger, id="t_dip", kind="perf_dip", payload={"placeholder": True})
        out = compose(category, merchant, fake)        # Studio11 is growing and above peers
        self.assertEqual(out["body"], "")
        self.assertIn("Deliberately not sending", out["rationale"])


if __name__ == "__main__":
    unittest.main()
