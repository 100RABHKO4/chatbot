import threading
import unittest

from state import ContextStore


def merchant(mid="m_001_drmeera_dentist_delhi", views=2410):
    return {"merchant_id": mid, "category_slug": "dentists", "performance": {"views": views}}


class ContextStoreTest(unittest.TestCase):
    def setUp(self):
        self.store = ContextStore()

    def test_new_context_is_accepted(self):
        result = self.store.put("merchant", "m_001_drmeera_dentist_delhi", 1, merchant())
        self.assertTrue(result.accepted)
        self.assertEqual(self.store.merchant("m_001_drmeera_dentist_delhi")["performance"]["views"], 2410)

    def test_same_version_is_stale_and_does_not_mutate(self):
        self.store.put("merchant", "m_001_drmeera_dentist_delhi", 1, merchant(views=2410))
        result = self.store.put("merchant", "m_001_drmeera_dentist_delhi", 1, merchant(views=9999))
        self.assertFalse(result.accepted)
        self.assertEqual(result.current.version, 1)
        self.assertEqual(self.store.merchant("m_001_drmeera_dentist_delhi")["performance"]["views"], 2410)

    def test_lower_version_is_stale(self):
        self.store.put("merchant", "m_001_drmeera_dentist_delhi", 5, merchant())
        result = self.store.put("merchant", "m_001_drmeera_dentist_delhi", 3, merchant(views=1))
        self.assertFalse(result.accepted)
        self.assertEqual(result.current.version, 5)

    def test_higher_version_replaces(self):
        self.store.put("merchant", "m_001_drmeera_dentist_delhi", 1, merchant(views=2410))
        self.assertTrue(self.store.put("merchant", "m_001_drmeera_dentist_delhi", 2, merchant(views=2580)).accepted)
        self.assertEqual(self.store.merchant("m_001_drmeera_dentist_delhi")["performance"]["views"], 2580)

    def test_short_context_id_resolves_by_payload_id(self):
        self.store.put("merchant", "m_001_drmeera", 1, merchant())
        self.assertIsNotNone(self.store.merchant("m_001_drmeera"))
        self.assertIsNotNone(self.store.merchant("m_001_drmeera_dentist_delhi"))

    def test_category_resolves_by_slug(self):
        self.store.put("category", "dentists", 1, {"slug": "dentists"})
        self.assertEqual(self.store.category("dentists")["slug"], "dentists")

    def test_customers_and_triggers_indexed_by_merchant(self):
        mid = "m_001_drmeera_dentist_delhi"
        self.store.put("merchant", "m_001_drmeera", 1, merchant(mid))
        self.store.put("customer", "c_2", 1, {"customer_id": "c_2", "merchant_id": mid})
        self.store.put("customer", "c_1", 1, {"customer_id": "c_1", "merchant_id": mid})
        self.store.put("customer", "c_x", 1, {"customer_id": "c_x", "merchant_id": "other"})
        self.store.put("trigger", "t_1", 1, {"id": "t_1", "merchant_id": mid})
        self.assertEqual([c["customer_id"] for c in self.store.customers_of(mid)], ["c_1", "c_2"])
        self.assertEqual([c["customer_id"] for c in self.store.customers_of("m_001_drmeera")], ["c_1", "c_2"])
        self.assertEqual([t["id"] for t in self.store.triggers_of(mid)], ["t_1"])

    def test_reindex_when_customer_moves_merchant(self):
        self.store.put("customer", "c_1", 1, {"customer_id": "c_1", "merchant_id": "a"})
        self.store.put("customer", "c_1", 2, {"customer_id": "c_1", "merchant_id": "b"})
        self.assertEqual(self.store.customers_of("a"), [])
        self.assertEqual(len(self.store.customers_of("b")), 1)

    def test_counts_and_clear(self):
        self.store.put("category", "dentists", 1, {"slug": "dentists"})
        self.store.put("trigger", "t", 1, {"id": "t"})
        self.assertEqual(self.store.counts(), {"category": 1, "merchant": 0, "customer": 0, "trigger": 1})
        self.store.clear()
        self.assertEqual(self.store.counts(), {"category": 0, "merchant": 0, "customer": 0, "trigger": 0})

    def test_invalid_scope_raises(self):
        with self.assertRaises(ValueError):
            self.store.put("offer", "x", 1, {})

    def test_unknown_lookups_return_none(self):
        self.assertIsNone(self.store.merchant("nope"))
        self.assertIsNone(self.store.merchant(None))
        self.assertIsNone(self.store.get("offer", "x"))

    def test_concurrent_version_race_keeps_highest(self):
        def push(v):
            self.store.put("merchant", "m", v, {"merchant_id": "m", "v": v})
        threads = [threading.Thread(target=push, args=(v,)) for v in range(1, 201)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        self.assertEqual(self.store.get_record("merchant", "m").version, 200)
        self.assertEqual(self.store.merchant("m")["v"], 200)


if __name__ == "__main__":
    unittest.main()
