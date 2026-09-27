import json
import threading
import unittest
from urllib import error, request

from server import VeraApp, create_server


def call(base, method, path, body=None, raw=None):
    data = raw if raw is not None else (json.dumps(body).encode() if body is not None else None)
    req = request.Request(base + path, data=data, method=method, headers={"Content-Type": "application/json"})
    try:
        with request.urlopen(req, timeout=5) as resp:
            return resp.status, json.loads(resp.read())
    except error.HTTPError as e:
        return e.code, json.loads(e.read())


class ServerTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = VeraApp()
        cls.server = create_server("127.0.0.1", 0, cls.app)
        cls.base = f"http://127.0.0.1:{cls.server.server_address[1]}"
        threading.Thread(target=cls.server.serve_forever, daemon=True).start()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()

    def setUp(self):
        self.app.store.clear()

    def push(self, scope, cid, version, payload):
        return call(self.base, "POST", "/v1/context",
                    {"scope": scope, "context_id": cid, "version": version, "payload": payload,
                     "delivered_at": "2026-04-26T10:00:00Z"})

    def test_healthz_and_metadata(self):
        status, body = call(self.base, "GET", "/v1/healthz")
        self.assertEqual(status, 200)
        self.assertEqual(body["status"], "ok")
        self.assertEqual(body["contexts_loaded"], {"category": 0, "merchant": 0, "customer": 0, "trigger": 0})
        status, meta = call(self.base, "GET", "/v1/metadata")
        self.assertEqual(status, 200)
        for key in ("team_name", "team_members", "model", "approach", "contact_email", "version", "submitted_at"):
            self.assertIn(key, meta)

    def test_context_accept_duplicate_stale_and_replace(self):
        status, body = self.push("merchant", "m_1", 1, {"merchant_id": "m_1"})
        self.assertEqual((status, body["accepted"], body["ack_id"]), (200, True, "ack_m_1_v1"))
        status, body = self.push("merchant", "m_1", 1, {"merchant_id": "m_1"})
        self.assertEqual((status, body), (409, {"accepted": False, "reason": "stale_version", "current_version": 1}))
        status, body = self.push("merchant", "m_1", 0, {"merchant_id": "m_1"})
        self.assertEqual(status, 409)
        status, _ = self.push("merchant", "m_1", 2, {"merchant_id": "m_1"})
        self.assertEqual(status, 200)
        self.assertEqual(call(self.base, "GET", "/v1/healthz")[1]["contexts_loaded"]["merchant"], 1)

    def test_context_validation_errors(self):
        self.assertEqual(self.push("offer", "x", 1, {})[1]["reason"], "invalid_scope")
        for bad in (self.push("merchant", "", 1, {}), self.push("merchant", "m", "1", {}),
                    self.push("merchant", "m", True, {}), self.push("merchant", "m", 1, [])):
            self.assertEqual(bad[0], 400)
        status, body = call(self.base, "POST", "/v1/context", raw=b"{not json")
        self.assertEqual((status, body["reason"]), (400, "malformed"))
        status, _ = call(self.base, "POST", "/v1/context", body=[1, 2])
        self.assertEqual(status, 400)

    def test_tick_is_always_valid(self):
        self.assertEqual(call(self.base, "POST", "/v1/tick", {"now": "2026-04-26T10:30:00Z",
                                                               "available_triggers": ["t1"]}), (200, {"actions": []}))
        self.assertEqual(call(self.base, "POST", "/v1/tick", raw=b"garbage"), (200, {"actions": []}))
        self.assertEqual(call(self.base, "POST", "/v1/tick", {"available_triggers": "nope"}), (200, {"actions": []}))

    def test_reply_is_always_valid(self):
        status, body = call(self.base, "POST", "/v1/reply", {"conversation_id": "c", "merchant_id": "m",
                                                             "from_role": "merchant", "message": "hi",
                                                             "received_at": "2026-04-26T10:40:00Z", "turn_number": 2})
        self.assertEqual(status, 200)
        self.assertIn(body["action"], ("send", "wait", "end"))
        status, body = call(self.base, "POST", "/v1/reply", raw=b"\xff\xfe")
        self.assertEqual((status, body["action"]), (200, "wait"))

    def test_routing_errors(self):
        self.assertEqual(call(self.base, "GET", "/v1/nope")[0], 404)
        self.assertEqual(call(self.base, "GET", "/v1/tick")[0], 405)
        self.assertEqual(call(self.base, "POST", "/v1/healthz", {})[0], 405)
        self.assertEqual(call(self.base, "GET", "/v1/healthz/")[0], 200)

    def test_teardown_wipes_state(self):
        self.push("category", "dentists", 1, {"slug": "dentists"})
        self.assertEqual(call(self.base, "POST", "/v1/teardown", {}), (200, {"ok": True}))
        self.assertEqual(call(self.base, "GET", "/v1/healthz")[1]["contexts_loaded"]["category"], 0)


class EngineGuardTest(unittest.TestCase):
    class BadEngine:
        def tick(self, request, store):
            return {"actions": [{"body": ""}, "junk", {"body": "ok body", **{f: None for f in (
                "conversation_id", "merchant_id", "customer_id", "send_as", "trigger_id", "template_name",
                "template_params", "cta", "suppression_key", "rationale")}}]}

        def reply(self, request, store):
            raise RuntimeError("boom")

        def reset(self):
            pass

    def test_malformed_actions_dropped_and_crashes_contained(self):
        app = VeraApp(engine=self.BadEngine())
        status, body = app.tick({"available_triggers": []})
        self.assertEqual(len(body["actions"]), 1)
        status, body = app.reply({"conversation_id": "c", "message": "yes"})
        self.assertEqual((status, body["action"]), (200, "wait"))


if __name__ == "__main__":
    unittest.main()
