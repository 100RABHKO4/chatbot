import copy
import glob
import json
import re
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from decision import FAMILY_OF_KIND, VeraEngine
from state import ContextStore
from validator import INTENT_FORBIDDEN, Validator

ROOT = Path(__file__).resolve().parent.parent
DATA = ROOT / "dataset"
NOW = "2026-04-26T10:30:00Z"


def seed_items(name, key):
    return json.loads((DATA / name).read_text())[key]


def load_seed_store(with_customers=True):
    store = ContextStore()
    for f in sorted((DATA / "categories").glob("*.json")):
        cat = json.loads(f.read_text())
        store.put("category", cat["slug"], 1, cat)
    for m in seed_items("merchants_seed.json", "merchants"):
        store.put("merchant", m["merchant_id"], 1, m)
    if with_customers:
        for c in seed_items("customers_seed.json", "customers"):
            store.put("customer", c["customer_id"], 1, c)
    for t in seed_items("triggers_seed.json", "triggers"):
        store.put("trigger", t["id"], 1, t)
    return store


def tick(engine, store, trigger_ids, now=NOW):
    return engine.tick({"now": now, "available_triggers": list(trigger_ids)}, store)["actions"]


def single(trigger_id, store=None):
    actions = tick(VeraEngine(), store or load_seed_store(), [trigger_id])
    return actions[0] if actions else None


class ExpandedDataset:
    """Runs the official generator once into a temp dir (deterministic seed)."""
    _dir = None

    @classmethod
    def path(cls):
        if cls._dir is None:
            cls._dir = tempfile.mkdtemp(prefix="vera_expanded_")
            subprocess.run([sys.executable, str(DATA / "generate_dataset.py"), "--seed-dir", str(DATA),
                            "--out", cls._dir], check=True, capture_output=True)
        return Path(cls._dir)

    @classmethod
    def store(cls):
        base, store = cls.path(), ContextStore()
        for f in sorted((base / "categories").glob("*.json")):
            c = json.loads(f.read_text())
            store.put("category", c["slug"], 1, c)
        for scope, key in (("merchants", "merchant_id"), ("customers", "customer_id"), ("triggers", "id")):
            for f in sorted((base / scope).glob("*.json")):
                x = json.loads(f.read_text())
                store.put(scope[:-1], x[key], 1, x)
        return store


def assert_clean(test, action):
    body = action["body"]
    lower = body.lower()
    for phrase in INTENT_FORBIDDEN:
        test.assertNotIn(phrase, lower, body)
    test.assertIsNone(re.search(r"\b[a-z0-9]+(?:_[a-z0-9]+)+\b", body), body)
    test.assertNotIn("Dr. Dr.", body)
    test.assertNotIn("http", lower)
    test.assertNotRegex(body, r"\{|\}|\bNone\b|\bnull\b")
    for field in ("conversation_id", "merchant_id", "customer_id", "send_as", "trigger_id", "template_name",
                  "template_params", "body", "cta", "suppression_key", "rationale"):
        test.assertIn(field, action)
    test.assertIn(action["send_as"], ("vera", "merchant_on_behalf"))
    test.assertIn(action["cta"], ("binary_yes_no", "binary_confirm_cancel", "open_ended", "multi_choice_slot", "none"))


# ------------------------------------------------------------ categories
class CategoryStrategyTest(unittest.TestCase):
    def test_dentist_is_clinical_and_cited(self):
        a = single("trg_001_research_digest_dentists")
        assert_clean(self, a)
        self.assertTrue(a["body"].startswith("Dr. Meera,"))
        self.assertIn("JIDA Oct 2026, p.14", a["body"])
        self.assertIn("124", a["body"])   # cohort from customer_aggregate

    def test_salon_curious_ask_uses_real_offer(self):
        a = single("trg_008_curious_ask_studio11")
        assert_clean(self, a)
        self.assertIn("Haircut @ ₹99", a["body"])
        self.assertEqual(a["cta"], "open_ended")

    def test_restaurant_weekend_ipl_is_contrarian(self):
        a = single("trg_010_ipl_match_delhi")
        assert_clean(self, a)
        self.assertIn("12%", a["body"])
        self.assertIn("skip a dine-in match promo", a["body"])

    def test_gym_seasonal_dip_reframes(self):
        a = single("trg_014_seasonal_acquisition_dip_powerhouse")
        assert_clean(self, a)
        self.assertIn("245", a["body"])
        self.assertIn("Apr-Jun", a["body"])

    def test_pharmacy_supply_alert_is_precise(self):
        a = single("trg_018_supply_atorvastatin_recall")
        assert_clean(self, a)
        for token in ("AT2024-1102", "AT2024-1108", "atorvastatin", "240"):
            self.assertIn(token, a["body"])
        self.assertNotRegex(a["body"].lower(), r"\bcure\b|\bdiagnos")

    def test_every_seed_trigger_produces_a_clean_action(self):
        store = load_seed_store()
        for t in seed_items("triggers_seed.json", "triggers"):
            with self.subTest(trigger=t["id"]):
                a = single(t["id"], store)
                self.assertIsNotNone(a, t["id"])
                assert_clean(self, a)
                self.assertEqual(a["trigger_id"], t["id"])
                self.assertEqual(a["merchant_id"], t["merchant_id"])
                self.assertEqual(a["suppression_key"], t["suppression_key"])


# ------------------------------------------------------------ customers
class CustomerFacingTest(unittest.TestCase):
    def test_recall_sent_on_behalf_with_real_slots_and_language(self):
        a = single("trg_003_recall_due_priya")
        assert_clean(self, a)
        self.assertEqual((a["send_as"], a["customer_id"], a["cta"]),
                         ("merchant_on_behalf", "c_001_priya_for_m001", "multi_choice_slot"))
        self.assertIn("Wed 5 Nov, 6pm", a["body"])
        self.assertIn("ready hai", a["body"])            # hi-en mix honoured

    def test_senior_refill_goes_via_son_in_hindi(self):
        a = single("trg_019_chronic_refill_grandfather")
        self.assertTrue(a["body"].startswith("Namaste"))
        self.assertIn("Mr. Sharma ji", a["body"])
        self.assertIn("CONFIRM", a["body"])

    def test_child_customer_addresses_parent(self):
        a = single("trg_017_kids_yoga_trial_followup_karthik")
        self.assertTrue(a["body"].startswith("Hi Sumitra"))

    def test_missing_customer_context_becomes_merchant_approval(self):
        a = single("trg_003_recall_due_priya", load_seed_store(with_customers=False))
        assert_clean(self, a)
        self.assertEqual((a["send_as"], a["customer_id"]), ("vera", None))
        self.assertNotIn("Priya", a["body"])

    def test_no_consent_customer_is_suppressed(self):
        store = load_seed_store()
        store.put("trigger", "t_c015", 1, {"id": "t_c015", "scope": "customer", "kind": "recall_due",
                                           "merchant_id": "m_010_sunrisepharm_pharmacy_lucknow",
                                           "customer_id": "c_015_anonymous_for_m010", "payload": {},
                                           "urgency": 3, "suppression_key": "k"})
        engine = VeraEngine()
        self.assertEqual(tick(engine, store, ["t_c015"]), [])
        self.assertIn("no recorded consent", engine.last_decisions[0]["reason"])

    def test_reminders_disabled_and_scope_mismatch_is_suppressed(self):
        store = load_seed_store()
        cust = {"customer_id": "c_x", "merchant_id": "m_003_studio11_salon_hyderabad",
                "identity": {"name": "Asha", "language_pref": "en"}, "state": "active",
                "preferences": {"channel": "whatsapp", "reminder_opt_in": False},
                "consent": {"opted_in_at": "2025-01-01", "scope": ["promotional_offers"]}}
        store.put("customer", "c_x", 1, cust)
        store.put("trigger", "t_x", 1, {"id": "t_x", "scope": "customer", "kind": "appointment_tomorrow",
                                        "merchant_id": cust["merchant_id"], "customer_id": "c_x",
                                        "payload": {"placeholder": True}, "urgency": 2, "suppression_key": "kx"})
        self.assertEqual(tick(VeraEngine(), store, ["t_x"]), [])
        cust2 = copy.deepcopy(cust)
        cust2["preferences"]["reminder_opt_in"] = True
        store.put("customer", "c_x", 2, cust2)
        self.assertEqual(len(tick(VeraEngine(), store, ["t_x"])), 1)

    def test_no_medicine_language_outside_pharmacies(self):
        store = ExpandedDataset.store()
        a = single("trg_081_chronic_refill_due_m_011_dr_sameer_dent", store)
        self.assertIsNotNone(a)
        self.assertNotRegex(a["body"].lower(), r"medicine|refill|stock")


# ------------------------------------------------------------ ranking
class RankingTest(unittest.TestCase):
    M001 = ["trg_001_research_digest_dentists", "trg_002_compliance_dci_radiograph",
            "trg_022_cde_webinar_dentists", "trg_023_competitor_opened_dentist"]

    def test_one_action_per_merchant_highest_urgency_wins(self):
        engine, store = VeraEngine(), load_seed_store()
        actions = tick(engine, store, self.M001)
        self.assertEqual([a["trigger_id"] for a in actions], ["trg_002_compliance_dci_radiograph"])
        deferred = {d["trigger_id"] for d in engine.last_decisions if d["outcome"] == "defer"}
        self.assertEqual(deferred, set(self.M001) - {"trg_002_compliance_dci_radiograph"})

    def test_deferred_triggers_go_out_on_later_ticks_in_priority_order(self):
        engine, store = VeraEngine(), load_seed_store()
        sent = [tick(engine, store, self.M001)[0]["trigger_id"] for _ in range(4)]
        self.assertEqual(sent[0], "trg_002_compliance_dci_radiograph")
        self.assertEqual(sent[1], "trg_001_research_digest_dentists")  # merchant's open ask boosts knowledge
        self.assertEqual(set(sent), set(self.M001))
        self.assertEqual(tick(engine, store, self.M001), [])

    def test_customer_facing_counts_toward_merchant_cap(self):
        actions = tick(VeraEngine(), load_seed_store(), ["trg_003_recall_due_priya", "trg_023_competitor_opened_dentist"])
        self.assertEqual(len(actions), 1)
        self.assertEqual(actions[0]["trigger_id"], "trg_003_recall_due_priya")

    def test_many_merchants_one_each_and_cap(self):
        store = ExpandedDataset.store()
        all_ids = sorted(json.loads(p.read_text())["id"] for p in (ExpandedDataset.path() / "triggers").glob("*.json"))
        actions = tick(VeraEngine(), store, all_ids)
        merchants = [a["merchant_id"] for a in actions]
        self.assertLessEqual(len(actions), 20)
        self.assertEqual(len(merchants), len(set(merchants)))

    def test_supply_alert_outranks_everything_for_its_merchant(self):
        actions = tick(VeraEngine(), load_seed_store(), ["trg_020_summer_demand_shift", "trg_019_chronic_refill_grandfather",
                                                          "trg_018_supply_atorvastatin_recall"])
        self.assertEqual(actions[0]["trigger_id"], "trg_018_supply_atorvastatin_recall")

    def test_ranking_is_deterministic(self):
        store = ExpandedDataset.store()
        ids = sorted(json.loads(p.read_text())["id"] for p in (ExpandedDataset.path() / "triggers").glob("*.json"))
        first = json.dumps(tick(VeraEngine(), store, ids), sort_keys=True)
        second = json.dumps(tick(VeraEngine(), store, list(reversed(ids))), sort_keys=True)
        self.assertEqual(first, second)


# ------------------------------------------------------------ suppression
class SuppressionTest(unittest.TestCase):
    def test_same_trigger_never_sent_twice(self):
        engine, store = VeraEngine(), load_seed_store()
        self.assertEqual(len(tick(engine, store, ["trg_023_competitor_opened_dentist"])), 1)
        self.assertEqual(tick(engine, store, ["trg_023_competitor_opened_dentist"]), [])
        self.assertIn("already sent", engine.last_decisions[0]["reason"])

    def test_suppression_key_scoped_per_recipient(self):
        store = load_seed_store()
        twin = dict(seed_items("triggers_seed.json", "triggers")[0], id="trg_twin",
                    merchant_id="m_002_bharat_dentist_mumbai")          # same category-wide key
        store.put("trigger", "trg_twin", 1, twin)
        engine = VeraEngine()
        self.assertEqual(len(tick(engine, store, ["trg_001_research_digest_dentists"])), 1)
        self.assertEqual(len(tick(engine, store, ["trg_twin"])), 1)

    def test_opted_out_and_snoozed_merchants_are_skipped(self):
        from datetime import datetime, timezone
        engine, store = VeraEngine(), load_seed_store()
        engine.opted_out_merchants.add("m_001_drmeera_dentist_delhi")
        self.assertEqual(tick(engine, store, ["trg_023_competitor_opened_dentist"]), [])
        engine.opted_out_merchants.clear()
        engine.snoozed_until["m_001_drmeera_dentist_delhi"] = datetime(2026, 4, 26, 12, 0, tzinfo=timezone.utc)
        self.assertEqual(tick(engine, store, ["trg_023_competitor_opened_dentist"]), [])
        self.assertEqual(len(tick(engine, store, ["trg_023_competitor_opened_dentist"], now="2026-04-26T12:05:00Z")), 1)

    def test_unknown_or_unloaded_contexts_are_skipped(self):
        store = ContextStore()
        store.put("trigger", "t", 1, {"id": "t", "kind": "perf_dip", "merchant_id": "m_missing"})
        engine = VeraEngine()
        self.assertEqual(tick(engine, store, ["t", "nope"]), [])
        self.assertEqual({d["reason"] for d in engine.last_decisions},
                         {"merchant context not loaded", "unknown trigger"})

    def test_never_repeats_a_body_from_conversation_history(self):
        store = load_seed_store()
        merchant = copy.deepcopy(store.merchant("m_001_drmeera_dentist_delhi"))
        first = single("trg_023_competitor_opened_dentist", store)
        merchant["conversation_history"].append({"from": "vera", "body": first["body"]})
        store.put("merchant", merchant["merchant_id"], 2, merchant)
        again = single("trg_023_competitor_opened_dentist", store)
        self.assertTrue(again is None or again["body"] != first["body"])


# ------------------------------------------------------------ contradictions + grounding
class GroundingTest(unittest.TestCase):
    def test_contradicted_dip_is_never_claimed(self):
        store = ExpandedDataset.store()   # T25: perf_dip, but views +8%, calls +2%
        a = single("trg_031_perf_dip_m_023_sushma_salon_p", store)
        self.assertIsNotNone(a)
        self.assertNotIn("drop", a["body"].lower())
        self.assertIn("calls up 2%", a["body"])

    def test_contradicted_dip_without_alternative_is_restrained(self):
        store = load_seed_store()
        m = copy.deepcopy(store.merchant("m_003_studio11_salon_hyderabad"))   # growing, above peers
        store.put("trigger", "t_fake_dip", 1, {"id": "t_fake_dip", "kind": "perf_dip", "merchant_id": m["merchant_id"],
                                               "payload": {"placeholder": True}, "urgency": 3, "suppression_key": "z"})
        engine = VeraEngine()
        self.assertEqual(tick(engine, store, ["t_fake_dip"]), [])
        self.assertIn("contradicted", engine.last_decisions[0]["reason"])

    def test_every_number_comes_from_the_selected_evidence(self):
        from decision import Ctx, b_perf
        from signals import merchant_view
        from strategies import pack_for
        from composer import _grounding
        store = load_seed_store()
        engine = VeraEngine()
        plan = engine.plan("trg_004_perf_dip_bharat", store, None)
        bank = Validator.scoped(_grounding(plan), plan.category)
        self.assertEqual(bank.check_numbers("calls down 50%, 6 vs 12"), [])
        # 220 (total patients) and 95 (lapsed) exist in Bharat's context but were not selected as evidence.
        self.assertEqual({i.detail for i in bank.check_numbers("220 patients, 95 lapsed")}, {"220", "95"})

    def test_sparse_sweep_every_kind_every_merchant(self):
        """All trigger kinds x all 50 generated merchants with placeholder payloads: clean or restrained."""
        store = ExpandedDataset.store()
        merchants = sorted(json.loads(p.read_text())["merchant_id"] for p in (ExpandedDataset.path() / "merchants").glob("*.json"))
        kinds = sorted(set(FAMILY_OF_KIND) | {"weather_heatwave", "totally_new_kind"})
        sent = skipped = 0
        for mid in merchants:
            for kind in kinds:
                tid = f"sweep_{mid}_{kind}"
                store.put("trigger", tid, 1, {"id": tid, "scope": "merchant", "kind": kind, "merchant_id": mid,
                                              "payload": {"placeholder": True, "metric_or_topic": kind},
                                              "urgency": 2, "suppression_key": tid})
                actions = tick(VeraEngine(), store, [tid])
                if actions:
                    sent += 1
                    assert_clean(self, actions[0])
                else:
                    skipped += 1
        self.assertGreater(sent, skipped)   # restraint is allowed, silence everywhere is not


# ------------------------------------------------------------ canonical pairs + server
class CanonicalAndServerTest(unittest.TestCase):
    def test_all_30_canonical_pairs(self):
        store = ExpandedDataset.store()
        pairs = json.loads((ExpandedDataset.path() / "test_pairs.json").read_text())["pairs"]
        produced = 0
        for p in pairs:
            with self.subTest(test_id=p["test_id"]):
                a = single(p["trigger_id"], store)
                if a is not None:
                    produced += 1
                    assert_clean(self, a)
                    self.assertEqual(a["merchant_id"], p["merchant_id"])
        self.assertEqual(produced, 30)

    def test_http_tick_returns_live_actions(self):
        import threading
        from urllib import request
        from server import VeraApp, create_server
        app = VeraApp(store=load_seed_store())
        srv = create_server("127.0.0.1", 0, app)
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        try:
            req = request.Request(f"http://127.0.0.1:{srv.server_address[1]}/v1/tick", method="POST",
                                  data=json.dumps({"now": NOW, "available_triggers": ["trg_018_supply_atorvastatin_recall"]}).encode(),
                                  headers={"Content-Type": "application/json"})
            body = json.loads(request.urlopen(req, timeout=5).read())
            self.assertEqual(len(body["actions"]), 1)
            assert_clean(self, body["actions"][0])
        finally:
            srv.shutdown()
            srv.server_close()


if __name__ == "__main__":
    unittest.main()
