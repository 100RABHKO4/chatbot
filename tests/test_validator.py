import json
import unittest
from pathlib import Path

from validator import FactBank, Validator, extract_numbers

DATA = Path(__file__).resolve().parent.parent / "dataset"


def load(name, key, ident):
    items = json.loads((DATA / name).read_text())[key]
    return next(i for i in items if i.get("merchant_id" if key == "merchants" else
                                         "customer_id" if key == "customers" else "id") == ident)


class ExtractNumbersTest(unittest.TestCase):
    def test_grouping_currency_percent_and_glued_tokens(self):
        values = [v for _, v in extract_numbers("₹1,499 and 2.1% vs 3.0%, Studio11, p.14, 5, 6pm, 1,00,000")]
        self.assertEqual(values, [1499, 2.1, 3.0, 14, 5, 6, 100000])


class NumberGroundingTest(unittest.TestCase):
    def setUp(self):
        self.category = json.loads((DATA / "categories" / "dentists.json").read_text())
        self.merchant = load("merchants_seed.json", "merchants", "m_001_drmeera_dentist_delhi")
        self.trigger = load("triggers_seed.json", "triggers", "trg_003_recall_due_priya")
        self.customer = load("customers_seed.json", "customers", "c_001_priya_for_m001")
        self.v = Validator(self.category, self.merchant, self.trigger, self.customer)

    def test_grounded_numbers_pass(self):
        body = ("Dr. Meera, your CTR is 2.1% vs 3.0% for peers; 2,410 views and 78 patients lapsed. "
                "JIDA Oct 2026, p.14: a 2,100-patient trial showed 38% fewer caries. "
                "Slots: Wed 5 Nov, 6pm or Thu 6 Nov, 5pm. Cleaning @ ₹299.")
        self.assertEqual(self.v.check_numbers(body), [])

    def test_fabricated_numbers_rejected(self):
        issues = self.v.check_numbers("412 of your patients were affected; revenue up 73%. Only ₹1,420 total.")
        self.assertEqual(sorted(i.detail for i in issues), ["1,420", "412", "73"])

    def test_derived_value_must_be_registered(self):
        self.assertTrue(self.v.check_numbers("that's 462 patients"))
        self.v.register(462)
        self.assertFalse(self.v.check_numbers("that's 462 patients"))

    def test_negative_delta_renders_as_percent(self):
        bharat = load("merchants_seed.json", "merchants", "m_002_bharat_dentist_mumbai")
        v = Validator(merchant=bharat)
        self.assertEqual(v.check_numbers("calls down 50% and views down 22% this week"), [])

    def test_fact_bank_ignores_booleans_and_none(self):
        bank = FactBank.from_contexts({"a": True, "b": None, "c": [False]})
        self.assertEqual(len(bank), 0)


class TextRulesTest(unittest.TestCase):
    def setUp(self):
        self.v = Validator(category=json.loads((DATA / "categories" / "dentists.json").read_text()))

    def test_raw_codes_and_placeholders_rejected(self):
        issues = self.v.check_codes("Your 6_month_cleaning is due; signal ctr_below_peer_median; Hi {name} {{1}} None")
        codes = sorted(i.code for i in issues)
        self.assertEqual(codes, ["nullish", "placeholder", "placeholder", "raw_code", "raw_code"])

    def test_jargon_rejected(self):
        self.assertTrue(self.v.check_codes("Based on the trigger payload we think..."))

    def test_urls_rejected(self):
        for body in ("read https://x.org/a", "see www.example.com", "visit magicpin.com/blog", "go to site.in"):
            self.assertTrue(self.v.check_urls(body), body)

    def test_citations_and_titles_are_not_urls(self):
        self.assertEqual(self.v.check_urls("Dr. Meera — JIDA Oct 2026, p.14. Mfr. Z batch AT2024-1102."), [])

    def test_intent_mode_mirrors_judge_substring_check(self):
        self.assertEqual(self.v.check_phrases("Do you want this?"), [])
        found = {i.detail for i in self.v.check_phrases("Do your team know? How about Friday?", intent_mode=True)}
        self.assertEqual(found, {"do you", "how about"})

    def test_generic_antipatterns_always_rejected(self):
        self.assertTrue(self.v.check_phrases("I hope you're doing well. Would you like to know more?"))

    def test_category_taboos(self):
        found = {i.detail for i in self.v.check_taboos("Guaranteed results, FDA-approved and 100% safe")}
        self.assertEqual(found, {"guaranteed", "fda-approved", "100% safe"})
        self.assertEqual(self.v.check_taboos("Scaling reduces caries risk"), [])

    def test_basics_and_repeat(self):
        self.assertEqual(self.v.validate("   ").codes(), ["empty_body"])
        self.assertEqual(self.v.validate(None).codes(), ["empty_body"])
        self.assertIn("too_short", self.v.validate("Hi").codes())
        self.assertIn("too_long", self.v.validate("word " * 300).codes())
        body = "Dr. Meera, your aligner post is drafted. Reply YES to publish."
        self.assertIn("repeat", self.v.validate(body, previous_bodies=["dr. meera,  your aligner post is drafted. reply yes to publish."]).codes())

    def test_clean_body_passes(self):
        self.assertTrue(self.v.validate("Dr. Meera, your aligner post is drafted. Reply YES to publish.").ok)


if __name__ == "__main__":
    unittest.main()
