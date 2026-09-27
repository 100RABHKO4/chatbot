"""Offline multi-turn handler required by challenge-brief.md §7.4.

    respond(state, merchant_message) -> dict

``state`` is a plain dict that the caller threads through successive calls:

    {
      "conversation_id": "conv_...",            # optional; generated if absent
      "merchant": {...MerchantContext...},      # required
      "category": {...CategoryContext...},      # optional
      "customer": {...CustomerContext...},      # optional (customer-facing threads)
      "trigger":  {...TriggerContext...},       # optional: opens the thread with Vera's first message
      "turns":    [{"from": "merchant", "body": "..."}, ...]   # managed by respond()
    }

It replays the thread through the same VeraEngine the HTTP server uses, so
offline and live behaviour cannot drift apart. The return value is the reply
action (send / wait / end) plus ``state`` with the new turns appended.
Deterministic: identical state + message -> identical output.
"""

from __future__ import annotations

import copy
from typing import Any, Optional

from decision import VeraEngine
from state import ContextStore

NOW = "2026-04-26T10:30:00Z"


def _rebuild(state: dict) -> tuple[VeraEngine, ContextStore, str, Optional[dict]]:
    store, engine = ContextStore(), VeraEngine()
    merchant = state["merchant"]
    store.put("merchant", merchant["merchant_id"], 1, merchant)
    if state.get("category"):
        store.put("category", state["category"].get("slug", merchant.get("category_slug", "")), 1, state["category"])
    if state.get("customer"):
        store.put("customer", state["customer"]["customer_id"], 1, state["customer"])
    opening = None
    conv_id = state.get("conversation_id")
    trigger = state.get("trigger")
    if trigger:
        store.put("trigger", trigger["id"], 1, trigger)
        actions = engine.tick({"now": NOW, "available_triggers": [trigger["id"]]}, store)["actions"]
        if actions:
            opening = actions[0]
            if conv_id and conv_id != opening["conversation_id"]:
                engine.conversations[conv_id] = engine.conversations.pop(opening["conversation_id"])
                engine.last_conversation_by_merchant[merchant["merchant_id"]] = conv_id
            conv_id = conv_id or opening["conversation_id"]
    return engine, store, conv_id or f"conv_offline_{merchant['merchant_id']}", opening


def respond(state: dict, merchant_message: str) -> dict:
    state = copy.deepcopy(state)
    engine, store, conv_id, opening = _rebuild(state)
    merchant_id = state["merchant"]["merchant_id"]
    role = "customer" if state.get("customer") and state.get("from_role") == "customer" else "merchant"

    def call(message: str, turn_number: int) -> dict[str, Any]:
        return engine.reply({"conversation_id": conv_id, "merchant_id": merchant_id,
                             "customer_id": (state.get("customer") or {}).get("customer_id"),
                             "from_role": role, "message": message, "received_at": NOW,
                             "turn_number": turn_number}, store)

    turns = state.setdefault("turns", [])
    inbound = [t["body"] for t in turns if t.get("from") in ("merchant", "customer")]
    for i, message in enumerate(inbound):          # replay history to rebuild counters and state
        call(message, i + 2)
    result = call(merchant_message, len(inbound) + 2)

    if opening and not any(t.get("from") == "vera" for t in turns):
        turns.insert(0, {"from": "vera", "body": opening["body"]})
    turns.append({"from": role, "body": merchant_message})
    if result["action"] == "send":
        turns.append({"from": "vera", "body": result["body"]})
    state["conversation_id"] = conv_id
    return {**result, "state": state}
