"""Submission entry point (challenge-brief.md §7.1).

Two uses:

  * ``compose(category, merchant, trigger, customer)``: the pure, offline
    composition function the brief asks for. Returns
    ``{body, cta, send_as, suppression_key, rationale}``. Deterministic: it runs
    the same decision engine as the HTTP server on a private in-memory store.
  * ``python bot.py [--port 8080]``: starts the HTTP server (same as server.py).
"""

from __future__ import annotations

from typing import Optional

from decision import VeraEngine
from state import ContextStore

# The dataset's "today" (week-17 digests, IPL match of 2026-04-26). Only used
# to pick the seasonal beat for month-sensitive categories.
REFERENCE_NOW = "2026-04-26T10:30:00Z"


def compose(category: dict, merchant: dict, trigger: dict, customer: Optional[dict] = None,
            now: str = REFERENCE_NOW) -> dict:
    store = ContextStore()
    if category:
        store.put("category", category.get("slug") or merchant.get("category_slug", "category"), 1, category)
    store.put("merchant", merchant["merchant_id"], 1, merchant)
    if customer:
        store.put("customer", customer["customer_id"], 1, customer)
    store.put("trigger", trigger["id"], 1, trigger)

    engine = VeraEngine()
    actions = engine.tick({"now": now, "available_triggers": [trigger["id"]]}, store)["actions"]
    if actions:
        action = actions[0]
        return {key: action[key] for key in ("body", "cta", "send_as", "suppression_key", "rationale")}

    reason = next((d.get("reason") for d in engine.last_decisions if d.get("trigger_id") == trigger["id"]),
                  "nothing grounded worth sending")
    return {"body": "", "cta": "none", "send_as": "vera",
            "suppression_key": str(trigger.get("suppression_key") or ""),
            "rationale": f"Deliberately not sending: {reason}."}


if __name__ == "__main__":
    from server import main
    main()
