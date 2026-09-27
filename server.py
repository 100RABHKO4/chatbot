"""Standard-library HTTP server exposing the Vera judge contract.

Endpoints (challenge-testing-brief.md §2):
  GET  /v1/healthz   GET  /v1/metadata
  POST /v1/context   POST /v1/tick   POST /v1/reply   POST /v1/teardown

Design:
  * ``VeraApp`` holds all behaviour and is callable without HTTP (tests use it).
  * ``make_handler`` adapts it to ``http.server``; the HTTP layer only parses,
    routes and serialises.
  * Decision-making lives behind the ``Engine`` interface: ``decision.VeraEngine``
    by default; ``NullEngine`` is a no-op stand-in for tests.
  * tick/reply never return an error status: a malformed request or an engine
    failure still yields a valid, conservative JSON answer, because the judge
    scores those endpoints and penalises malformed output.

Run: python server.py [--host 0.0.0.0] [--port 8080]   (PORT env also honoured)
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys
import time
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Optional, Protocol

from decision import VeraEngine
from state import SCOPES, ContextStore, utc_now_iso

log = logging.getLogger("vera.server")

MAX_BODY_BYTES = 2 * 1024 * 1024  # judge caps context payloads at 500 KB
MAX_ACTIONS_PER_TICK = 20         # challenge-testing-brief.md §5
DEFAULT_WAIT_SECONDS = 1800

REQUIRED_ACTION_FIELDS = (
    "conversation_id", "merchant_id", "customer_id", "send_as", "trigger_id",
    "template_name", "template_params", "body", "cta", "suppression_key", "rationale",
)
REPLY_ACTIONS = ("send", "wait", "end")

Response = tuple[int, dict]


class Engine(Protocol):
    def tick(self, request: dict, store: ContextStore) -> dict: ...
    def reply(self, request: dict, store: ContextStore) -> dict: ...
    def reset(self) -> None: ...


class NullEngine:
    """Placeholder until the decision layer lands: never sends, never crashes."""

    def tick(self, request: dict, store: ContextStore) -> dict:
        return {"actions": []}

    def reply(self, request: dict, store: ContextStore) -> dict:
        return {"action": "wait", "wait_seconds": DEFAULT_WAIT_SECONDS,
                "rationale": "Reply engine not configured; holding without sending."}

    def reset(self) -> None:
        pass


class VeraApp:
    def __init__(self, engine: Optional[Engine] = None, store: Optional[ContextStore] = None) -> None:
        self.store = store or ContextStore()
        self.engine: Engine = engine or VeraEngine()
        self.started = time.monotonic()

    # ---------------------------------------------------------------- GET
    def healthz(self) -> Response:
        return HTTPStatus.OK, {
            "status": "ok",
            "uptime_seconds": int(time.monotonic() - self.started),
            "contexts_loaded": self.store.counts(),
        }

    def metadata(self) -> Response:
        members = [m.strip() for m in os.environ.get("VERA_TEAM_MEMBERS", "").split(",") if m.strip()]
        return HTTPStatus.OK, {
            "team_name": os.environ.get("VERA_TEAM_NAME", "Vera Deterministic"),
            "team_members": members,
            "model": "deterministic-rules-v1 (no LLM)",
            "approach": "Decision-first rule engine: signal ranking, category strategy packs, "
                        "grounded composition with a fact-bank validator",
            "contact_email": os.environ.get("VERA_CONTACT_EMAIL", ""),
            "version": "0.1.0",
            "submitted_at": os.environ.get("VERA_SUBMITTED_AT", "2026-04-26T08:00:00Z"),
        }

    # --------------------------------------------------------------- POST
    def context(self, body: Any) -> Response:
        error = _validate_context(body)
        if error is not None:
            reason, details = error
            return HTTPStatus.BAD_REQUEST, {"accepted": False, "reason": reason, "details": details}

        result = self.store.put(body["scope"], body["context_id"], body["version"], body["payload"])
        if not result.accepted:
            return HTTPStatus.CONFLICT, {"accepted": False, "reason": "stale_version",
                                         "current_version": result.current.version}
        return HTTPStatus.OK, {"accepted": True,
                               "ack_id": f"ack_{body['context_id']}_v{body['version']}",
                               "stored_at": result.current.stored_at}

    def tick(self, body: Any) -> Response:
        if not isinstance(body, dict):
            return HTTPStatus.OK, {"actions": []}
        request = {
            "now": body.get("now") if isinstance(body.get("now"), str) else utc_now_iso(),
            "available_triggers": [t for t in body.get("available_triggers") or []
                                   if isinstance(t, str)] if isinstance(body.get("available_triggers"), list) else [],
        }
        try:
            result = self.engine.tick(request, self.store)
        except Exception:
            log.exception("engine.tick failed")
            return HTTPStatus.OK, {"actions": []}
        return HTTPStatus.OK, {"actions": _sanitize_actions(result)}

    def reply(self, body: Any) -> Response:
        if not isinstance(body, dict) or not isinstance(body.get("conversation_id"), str) \
                or not isinstance(body.get("message"), str):
            return HTTPStatus.OK, _safe_wait("Malformed reply request; holding.")
        request = {
            "conversation_id": body["conversation_id"],
            "merchant_id": body.get("merchant_id") if isinstance(body.get("merchant_id"), str) else None,
            "customer_id": body.get("customer_id") if isinstance(body.get("customer_id"), str) else None,
            "from_role": body.get("from_role") if body.get("from_role") in ("merchant", "customer") else "merchant",
            "message": body["message"],
            "received_at": body.get("received_at") if isinstance(body.get("received_at"), str) else utc_now_iso(),
            "turn_number": body.get("turn_number") if _is_int(body.get("turn_number")) else None,
        }
        try:
            result = self.engine.reply(request, self.store)
        except Exception:
            log.exception("engine.reply failed")
            return HTTPStatus.OK, _safe_wait("Internal error; holding instead of sending.")
        return HTTPStatus.OK, _sanitize_reply(result)

    def teardown(self) -> Response:
        self.store.clear()
        self.engine.reset()
        return HTTPStatus.OK, {"ok": True}


# ------------------------------------------------------------ validation
def _is_int(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def _validate_context(body: Any) -> Optional[tuple[str, str]]:
    if not isinstance(body, dict):
        return "malformed", "request body must be a JSON object"
    if body.get("scope") not in SCOPES:
        return "invalid_scope", f"scope must be one of {list(SCOPES)}"
    if not isinstance(body.get("context_id"), str) or not body["context_id"].strip():
        return "malformed", "context_id must be a non-empty string"
    if not _is_int(body.get("version")) or body["version"] < 0:
        return "malformed", "version must be a non-negative integer"
    if not isinstance(body.get("payload"), dict):
        return "malformed", "payload must be a JSON object"
    return None


def _safe_wait(rationale: str) -> dict:
    return {"action": "wait", "wait_seconds": DEFAULT_WAIT_SECONDS, "rationale": rationale}


def _sanitize_actions(result: Any) -> list[dict]:
    actions = result.get("actions") if isinstance(result, dict) else None
    if not isinstance(actions, list):
        return []
    clean = []
    for action in actions:
        if not isinstance(action, dict):
            continue
        missing = [f for f in REQUIRED_ACTION_FIELDS if f not in action]
        if missing or not isinstance(action.get("body"), str) or not action["body"].strip():
            log.error("dropping malformed action %s (missing=%s)", action.get("trigger_id"), missing)
            continue
        clean.append(action)
    return clean[:MAX_ACTIONS_PER_TICK]


def _sanitize_reply(result: Any) -> dict:
    if not isinstance(result, dict) or result.get("action") not in REPLY_ACTIONS:
        return _safe_wait("Engine returned an invalid reply; holding.")
    if result["action"] == "send" and (not isinstance(result.get("body"), str) or not result["body"].strip()):
        return _safe_wait("Engine produced an empty body; holding instead of sending.")
    if result["action"] == "wait" and not _is_int(result.get("wait_seconds")):
        result = {**result, "wait_seconds": DEFAULT_WAIT_SECONDS}
    result.setdefault("rationale", "")
    return result


# ------------------------------------------------------------------ HTTP
GET_ROUTES = {"/v1/healthz": "healthz", "/v1/metadata": "metadata"}
POST_ROUTES = {"/v1/context": "context", "/v1/tick": "tick", "/v1/reply": "reply",
               "/v1/teardown": "teardown"}
# Fallback bodies when a POST cannot be parsed at all.
UNPARSEABLE = {
    "context": (HTTPStatus.BAD_REQUEST, {"accepted": False, "reason": "malformed", "details": "invalid JSON"}),
    "tick": (HTTPStatus.OK, {"actions": []}),
    "reply": (HTTPStatus.OK, _safe_wait("Unparseable reply request; holding.")),
    "teardown": None,
}


def make_handler(app: VeraApp) -> type[BaseHTTPRequestHandler]:
    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"
        server_version = "VeraBot/0.1"

        def do_GET(self) -> None:
            route = GET_ROUTES.get(self._path())
            if route is None:
                self._not_found_or_not_allowed(POST_ROUTES)
                return
            self._send(*getattr(app, route)())

        def do_POST(self) -> None:
            route = POST_ROUTES.get(self._path())
            if route is None:
                self._not_found_or_not_allowed(GET_ROUTES)
                return
            ok, body = self._read_json()
            if not ok:
                fallback = UNPARSEABLE[route]
                if fallback is not None:
                    self._send(*fallback)
                    return
                body = {}
            if body is _TOO_LARGE:
                self._send(HTTPStatus.REQUEST_ENTITY_TOO_LARGE,
                           {"accepted": False, "reason": "payload_too_large",
                            "details": f"body exceeds {MAX_BODY_BYTES} bytes"})
                return
            handler = getattr(app, route)
            self._send(*(handler() if route == "teardown" else handler(body)))

        # -------------------------------------------------------- helpers
        def _path(self) -> str:
            return self.path.split("?", 1)[0].rstrip("/") or "/"

        def _not_found_or_not_allowed(self, other_routes: dict) -> None:
            if self._path() in other_routes:
                self._send(HTTPStatus.METHOD_NOT_ALLOWED, {"error": "method_not_allowed"})
            else:
                self._send(HTTPStatus.NOT_FOUND, {"error": "not_found", "path": self._path()})

        def _read_json(self) -> tuple[bool, Any]:
            try:
                length = int(self.headers.get("Content-Length") or 0)
            except ValueError:
                return False, None
            if length > MAX_BODY_BYTES:
                self.rfile.read(length)  # drain so the connection stays usable
                return True, _TOO_LARGE
            raw = self.rfile.read(length) if length > 0 else b""
            try:
                return True, json.loads(raw.decode("utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError):
                return False, None

        def _send(self, status: int, payload: dict) -> None:
            data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def log_message(self, fmt: str, *args: Any) -> None:
            log.debug("%s - %s", self.address_string(), fmt % args)

    return Handler


_TOO_LARGE = object()


class _Server(ThreadingHTTPServer):
    # Default listen backlog is 5; the judge sends up to 10 req/s, and a full
    # backlog costs a ~1s SYN retry on the client side.
    request_queue_size = 128


def create_server(host: str = "0.0.0.0", port: int = 8080, app: Optional[VeraApp] = None) -> ThreadingHTTPServer:
    server = _Server((host, port), make_handler(app or VeraApp()))
    server.daemon_threads = True
    return server


def main(argv: Optional[list[str]] = None) -> None:
    parser = argparse.ArgumentParser(description="Vera bot HTTP server")
    parser.add_argument("--host", default=os.environ.get("HOST", "0.0.0.0"))
    parser.add_argument("--port", type=int, default=int(os.environ.get("PORT", "8080")))
    parser.add_argument("--log-level", default=os.environ.get("LOG_LEVEL", "INFO"))
    args = parser.parse_args(argv)

    logging.basicConfig(level=args.log_level.upper(), stream=sys.stderr,
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    server = create_server(args.host, args.port)
    log.info("Vera bot listening on http://%s:%d", args.host, args.port)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
