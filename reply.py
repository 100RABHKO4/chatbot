"""Reply engine: classify the inbound turn, decide send / wait / end, validate.

Intent order (first match wins). The order resolves overlaps deliberately:
  1 opt_out     "STOP", "unsubscribe", "don't message me"          -> end + suppress merchant
  2 hostile     abuse without an explicit stop                    -> apologise once; repeat -> end
  3 auto_reply  canned WhatsApp Business text or verbatim repeat  -> send once, wait, end
  4 defer       "busy", "call me tomorrow", "ping me later"         -> wait (snooze)
  5 decline     "no", "not now", "don't want this"                 -> acknowledge, drop proposal
  6 accept      "yes", "go ahead", "let's do it", "approved"        -> execute, no questions
  7 off_topic   GST, loans, tax, ...                                 -> decline politely, redirect
  8 question    "?", what/how/kitna, "tell me more"                  -> grounded answer + re-offer
  9 unclear     anything else                                        -> one binary nudge, then wait

Referent: "yes"/"do it" binds to the pending proposal of this conversation,
else the merchant's latest Vera conversation, else the last Vera offer the
merchant accepted in their conversation_history, else a generic next step.

Auto-replies and hostility are counted per conversation AND per merchant, so
a judge that rotates conversation ids is still detected.
"""

from __future__ import annotations

import hashlib
import re
from datetime import datetime, timedelta, timezone
from typing import Any, Optional

from signals import (MerchantView, customer_view, first_sentence, fmt_num, fmt_pct, merchant_view,
                     parse_iso, METRIC_WORDS)
from state import ContextStore
from strategies import pack_for
from strategies.base import CategoryPack
from validator import Validator

# ------------------------------------------------------------------ lexicons
_OPT_OUT = re.compile(
    r"\b(stop|unsubscribe|opt[\s-]?out|remove me|leave me alone|no more (messages|msgs)|"
    r"(don'?t|do not|dont|never) (message|text|contact|send|ping|call|bother|disturb)\w*|"
    r"band karo|mat bhejo|message mat|msg mat|mujhe mat)\b", re.I)
_HOSTILE = re.compile(
    r"\b(useless|spam\w*|stupid|idiot\w*|nonsense|rubbish|bakwa+s|fraud|scam\w*|cheat\w*|irritat\w*|"
    r"annoying|bothering|harass\w*|shut up|waste of (my )?time|get lost|pagal|bloody|damn|f+u+c+k\w*|wtf|"
    r"bullshit|go to hell|chup)\b", re.I)
_AUTO = re.compile(
    r"(thank(s| you) for (contacting|reaching|your message|messaging|writing)|"
    r"(will|shall) (get back|respond|reply|revert)( to you)? (shortly|soon|as soon)|our team will|"
    r"automated (assistant|reply|response|message)|auto[- ]?reply|out of (the )?office|"
    r"currently (unavailable|away|closed)|outside (our )?(business|working) hours|"
    r"we have received your (message|query)|this is an automated|"
    r"jaankari ke liye|team tak pahuncha|aapka sandesh)", re.I)
_DEFER = re.compile(
    r"\b(busy|later|tomorrow|tmrw|kal|baad (me|mein|main)|next week|call me|ping me|remind me|"
    r"in (a|an|\d+) (min|mins|minute|minutes|hour|hours|hr|hrs|day|days)|after \d+|tonight|this evening|"
    r"in a meeting|driving|not right now|right now busy|abhi busy|free hoke|some other time)\b", re.I)
_DECLINE = re.compile(
    r"\b(no(?! (problem|worries|issue))|nope|nah|nahi|nahin|not interested|don'?t want|do not want|"
    r"not now|not needed|no need|skip( it)?|leave it|rehne do|mat karo|not required|cancel)\b", re.I)
_ACCEPT = re.compile(
    r"\b(yes|yeah|yep|yup|haan|han|ji haan|ok|okay|okk+|sure|go ahead|go for it|do it|let'?s do it|lets do|"
    r"proceed|approved?|approve it|confirm(ed)?|send (it|me|the|over)|please do|karo|kar do|chalega|"
    r"theek hai|thik hai|start( it)?|book it|sounds good|perfect|want to (join|start)|judna|judrna|jodna|"
    r"interested|draft it|publish)\b", re.I)
_OFF_TOPIC = re.compile(
    r"\b(gst|income tax|itr|tax (filing|return)|loan|insurance|visa|passport|stock market|share price|crypto|"
    r"cricket score|election|politics|recipe|movie|bank account|credit card|electricity bill|aadhaar|pan card)\b", re.I)
_QUESTION = re.compile(
    r"(\?|^\s*(what|which|how|why|when|where|who|kya|kitna|kitne|kaun|kab|kaise|kyun|can|could|is|are|does|do)\b|"
    r"tell me more|more (info|details)|details|explain|samjhao|batao)", re.I)
_THANKS_ONLY = re.compile(r"^\s*(?:(?:thanks|thank you|thx|ty|shukriya|dhanyavaad|🙏|👍)[\s.!]*)+$", re.I)
_HINGLISH = re.compile(r"\b(hai|haan|nahi|karo|kar|bhej\w*|abhi|kal|baad|mujhe|aap|theek|thik|kya|chahiye|ji|"
                       r"accha|acha|kitna|batao)\b|[ऀ-ॿ]", re.I)
_SLOT_CHOICE = re.compile(r"^\s*([1-9])\s*[.!)]?\s*$")

AUTO_REPEAT_MIN_CHARS = 25
QUALIFYING = ("would you", "do you", "can you tell", "what if", "how about")

AGGREGATE_PHRASES = {
    "lapsed_180d_plus": "{n} {noun} lapsed for 180+ days",
    "lapsed_90d_plus": "{n} {noun} lapsed for 90+ days",
    "total_unique_ytd": "{n} unique {noun} so far this year",
    "high_risk_adult_count": "{n} high-risk adult {noun}",
    "total_active_members": "{n} active members",
    "chronic_rx_count": "{n} chronic-Rx customers",
    "delivery_orders_30d": "{n} delivery orders in the last 30 days",
    "dine_in_orders_30d": "{n} dine-in orders in the last 30 days",
}


def classify(message: str, conv: dict, mstate: dict) -> str:
    text = message.strip()
    if not text:
        return "unclear"
    if _THANKS_ONLY.match(text):
        return "thanks"
    if not re.search(r"[A-Za-z0-9\u0900-\u097F]", text):
        return "unclear"             # emoji / punctuation only
    if _OPT_OUT.search(text):
        return "opt_out"
    if _HOSTILE.search(text):
        return "hostile"
    if _is_auto_reply(text, conv, mstate):
        return "auto_reply"
    if _DEFER.search(text):
        return "defer"
    if _DECLINE.search(text):
        return "decline"
    if _ACCEPT.search(text):
        return "accept"
    if _OFF_TOPIC.search(text):
        return "off_topic"
    if _QUESTION.search(text):
        return "question"
    return "unclear"


def _norm(text: str) -> str:
    return " ".join(text.lower().split())


def _is_auto_reply(text: str, conv: dict, mstate: dict) -> bool:
    if _AUTO.search(text):
        return True
    if len(text) < AUTO_REPEAT_MIN_CHARS:
        return False
    norm = _norm(text)
    seen = [t["body"] for t in conv.get("turns", []) if t.get("from") != "vera"] + mstate.get("recent_inbound", [])
    return any(_norm(s) == norm for s in seen)


# ------------------------------------------------------------------ helpers
def _now(request: dict) -> datetime:
    parsed = parse_iso(request.get("received_at"))
    if parsed is None:
        return datetime.now(timezone.utc)
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def _pick(seed: str, options: list[str]) -> str:
    return options[int(hashlib.sha256(seed.encode()).hexdigest(), 16) % len(options)]


def _wait(seconds: int, why: str) -> dict:
    return {"action": "wait", "wait_seconds": int(seconds), "rationale": why}


def _end(why: str) -> dict:
    return {"action": "end", "rationale": why}


def _defer_seconds(text: str) -> int:
    lower = text.lower()
    match = re.search(r"\b(?:in|after)\s+(a|an|\d+)\s*(min|mins|minute|minutes|hour|hours|hr|hrs|day|days)\b", lower)
    if match:
        qty = 1 if match.group(1) in ("a", "an") else int(match.group(1))
        unit = match.group(2)
        return qty * (60 if unit.startswith("min") else 86400 if unit.startswith("day") else 3600)
    if "next week" in lower:
        return 7 * 86400
    if re.search(r"\b(tomorrow|tmrw|kal)\b", lower):
        return 86400
    if re.search(r"\b(tonight|this evening)\b", lower):
        return 6 * 3600
    if re.search(r"\b(meeting|driving|right now|abhi)\b", lower):
        return 1800
    return 3 * 3600


# ------------------------------------------------------------------ context
class Turn:
    """Everything one reply decision needs, resolved once."""

    def __init__(self, engine, request: dict, store: ContextStore) -> None:
        self.engine, self.request, self.store = engine, request, store
        self.conv_id: str = request["conversation_id"]
        conv = engine.conversations.get(self.conv_id)
        merchant_id = request.get("merchant_id") or (conv or {}).get("merchant_id")
        merchant = store.merchant(merchant_id) if merchant_id else None
        self.mv: Optional[MerchantView] = merchant_view(merchant) if merchant else None
        self.merchant_id: str = (self.mv.merchant_id if self.mv else None) or merchant_id or "unknown"
        if conv is None:
            conv = {"merchant_id": self.merchant_id, "customer_id": request.get("customer_id"), "send_as": "vera",
                    "bodies": [], "turns": [], "status": "open", "proposal": None, "adopted": True}
            engine.conversations[self.conv_id] = conv
        self.conv = conv
        self.mstate = engine.merchant_state.setdefault(self.merchant_id, {
            "auto_streak": 0, "hostile_count": 0, "turns": 0, "recent_inbound": []})
        self.category = store.category(self.mv.category) if self.mv else None
        self.pack: CategoryPack = pack_for(self.mv.category if self.mv else None)
        self.message: str = request["message"]
        self.now = _now(request)
        self.hinglish = bool(_HINGLISH.search(self.message)) or \
            (self.conv.get("send_as") == "merchant_on_behalf" and self._customer_lang() in ("hi", "hi-en"))
        self.proposal = self._resolve_proposal()

    def _customer_lang(self) -> str:
        customer = self.store.customer(self.conv.get("customer_id") or self.request.get("customer_id"))
        return customer_view(customer).language if customer else "en"

    # "yes" binds to the most specific open proposal available.
    def _resolve_proposal(self) -> Optional[dict]:
        if self.conv.get("proposal"):
            return self.conv["proposal"]
        last_id = self.engine.last_conversation_by_merchant.get(self.merchant_id)
        last = self.engine.conversations.get(last_id or "")
        if last and last.get("proposal") and last.get("status") != "ended":
            return last["proposal"]
        return self._history_proposal()

    def _history_proposal(self) -> Optional[dict]:
        if not self.mv:
            return None
        history = self.mv.history
        for i in range(len(history) - 1, -1, -1):
            turn = history[i]
            body = turn.get("body") if turn.get("from") == "vera" else None
            offer = re.search(r"Want me to (.+?)\?", body or "")
            if not offer:
                continue
            follow = history[i + 1] if i + 1 < len(history) else None
            ask = follow.get("body") if follow and follow.get("from") == "merchant" else None
            deliverable = offer.group(1).strip()
            if ask:
                focus = re.search(r"focus on (.+)$", ask, re.I)
                if focus:
                    deliverable += f", focused on {focus.group(1).rstrip('.!')}"
            return {"action": "history_offer", "family": "history", "kind": "conversation_history",
                    "deliverable": deliverable, "reason": "your earlier request", "facts": {},
                    "grounding": [body] + ([ask] if ask else []), "cta": "binary_yes_no", "slots": []}
        return None

    # ------------------------------------------------------------ output
    @property
    def who(self) -> str:
        if not self.mv:
            return ""
        return self.pack.salute(self.mv.owner) if self.mv.owner else self.mv.name

    def deliverable(self) -> str:
        if self.proposal:
            return self.proposal["deliverable"]
        return self.pack.deliverable("benchmark_gap")

    def grounding(self, *extra: Any) -> list[Any]:
        base = list((self.proposal or {}).get("grounding") or [])
        if self.mv:
            base += [self.mv.name, self.mv.owner, self.mv.locality, self.mv.city]
        return base + [e for e in extra if e is not None]

    def send(self, bodies: list[str], cta: str, why: str, grounding: list[Any] = (), qualifying_ok: bool = False,
             no_questions: bool = False) -> dict:
        """Validate candidate bodies in order; first clean one is sent, else hold."""
        previous = self.conv.get("bodies", []) + self.engine.bodies_by_merchant.get(self.merchant_id, [])
        validator = Validator.scoped(self.grounding(*grounding), self.category, self.pack.extra_taboos)
        failures = []
        for body in bodies:
            result = validator.validate(body, intent_mode=not qualifying_ok, previous_bodies=previous)
            if no_questions and "?" in body:
                failures.append(["question_mark"])
                continue
            if result.ok:
                self.conv.setdefault("bodies", []).append(body)
                self.conv.setdefault("turns", []).append({"from": "vera", "body": body})
                self.engine.bodies_by_merchant.setdefault(self.merchant_id, []).append(body)
                return {"action": "send", "body": body, "cta": cta, "rationale": why}
            failures.append(result.codes())
        return _wait(1800, f"{why} — held: no candidate passed validation {failures}")


# ------------------------------------------------------------------ policies
def handle_reply(engine, request: dict, store: ContextStore) -> dict:
    cache_key = (request["conversation_id"], request.get("turn_number"), request["message"]) \
        if request.get("turn_number") is not None else None
    if cache_key and cache_key in engine.reply_cache:
        return dict(engine.reply_cache[cache_key])  # duplicate delivery: same answer, no state change

    turn = Turn(engine, request, store)
    conv, mstate = turn.conv, turn.mstate
    if turn.conv.get("send_as") == "merchant_on_behalf" or request.get("from_role") == "customer":
        response = _customer_reply(turn)
    else:
        intent = classify(turn.message, conv, mstate)
        response = _merchant_reply(turn, intent)
        conv["last_intent"] = intent

    conv.setdefault("turns", []).insert(len(conv.get("turns", [])) - (1 if response["action"] == "send" else 0),
                                        {"from": request.get("from_role", "merchant"), "body": turn.message,
                                         "intent": conv.get("last_intent")})
    mstate["turns"] = mstate.get("turns", 0) + 1
    mstate["recent_inbound"] = (mstate.get("recent_inbound", []) + [turn.message])[-5:]
    if response["action"] == "end":
        conv["status"] = "ended"
    elif response["action"] == "wait":
        conv["status"] = "waiting"
    if cache_key:
        engine.reply_cache[cache_key] = dict(response)
    return response


def _merchant_reply(turn: Turn, intent: str) -> dict:
    conv, mstate, engine = turn.conv, turn.mstate, turn.engine
    if intent != "auto_reply":
        mstate["auto_streak"] = 0
        conv["auto_streak"] = 0

    # A closed conversation stays closed, except that a genuine human reply
    # after an auto-reply exit re-opens it (the owner is finally here).
    if conv.get("status") == "ended":
        if conv.get("ended_reason") in ("opt_out", "hostile") or intent in ("opt_out", "hostile", "auto_reply"):
            return _end("Conversation already closed; not re-engaging.")
        conv["status"] = "open"

    if intent == "opt_out":
        engine.opted_out_merchants.add(turn.merchant_id)
        conv["ended_reason"] = "opt_out"
        return _end("Merchant asked to stop. Closing this conversation and suppressing all proactive "
                    "messages to this merchant.")

    if intent == "hostile":
        mstate["hostile_count"] = mstate.get("hostile_count", 0) + 1
        if mstate["hostile_count"] >= 2:
            conv["ended_reason"] = "hostile"
            engine.snoozed_until[turn.merchant_id] = turn.now + timedelta(days=7)
            return _end("Repeated frustration from the merchant; exiting gracefully and pausing outreach for 7 days.")
        name = f", {turn.who}" if turn.who else ""
        return turn.send([
            f"Sorry{name} — that's fair feedback, and I won't push. I'll only message when there's something "
            f"specific for your {turn.pack.business_noun}. Reply STOP anytime and I won't message again.",
            f"Apologies{name} — point taken. I'll keep it to what's genuinely useful. Reply STOP and I won't message again.",
        ], "none", "Merchant frustrated (no explicit stop): one apology with a clear opt-out path, no argument.")

    if intent == "auto_reply":
        mstate["auto_streak"] = mstate.get("auto_streak", 0) + 1
        conv["auto_streak"] = conv.get("auto_streak", 0) + 1
        streak = max(mstate["auto_streak"], conv["auto_streak"])
        if streak >= 3:
            conv["ended_reason"] = "auto_reply"
            engine.snoozed_until[turn.merchant_id] = turn.now + timedelta(hours=24)
            return _end(f"Auto-reply received {streak} times in a row; no human on this number. Closing and "
                        "pausing proactive sends for 24h.")
        if streak == 2:
            return _wait(86400, "Same canned auto-reply again; the owner isn't at the phone. Waiting 24h before retrying.")
        owner = turn.who or "the owner"
        return turn.send([
            f"Looks like an automated reply — no problem. When {owner} sees this, just reply YES and I'll "
            f"{turn.deliverable()}.",
            f"That reads like an auto-reply. Whenever {owner} is free: reply YES and I'll {turn.deliverable()}.",
        ], "binary_yes_no", "Detected a WhatsApp Business auto-reply; one short note flagged for the owner.")

    if intent == "defer":
        seconds = _defer_seconds(turn.message)
        engine.snoozed_until[turn.merchant_id] = turn.now + timedelta(seconds=seconds)
        return _wait(seconds, f"Merchant asked for time; snoozing this merchant for {seconds // 60} minutes.")

    if intent == "decline":
        topic = (turn.proposal or {}).get("deliverable")
        conv["proposal"] = None
        engine.snoozed_until[turn.merchant_id] = turn.now + timedelta(hours=24)
        if conv.get("last_intent") == "decline":
            return _end("Second decline in a row; closing politely.")
        lead = "Bilkul, no problem" if turn.hinglish else "No problem"
        dropped = " — I've dropped that idea." if topic else "."
        return turn.send([
            f"{lead}{dropped} I'll only come back when there's something new worth your time.",
            "Understood — parking this. I'll only reach out again with something new.",
        ], "none", "Merchant declined; proposal cleared, no counter-pitch, 24h snooze on proactive sends.")

    if intent == "accept":
        return _accept(turn)

    if intent == "off_topic":
        subject = _OFF_TOPIC.search(turn.message).group(0)
        subject = subject.upper() if len(subject) <= 3 else subject
        return turn.send([
            f"{subject} is outside what I can help with — your CA or the right provider is best for that. "
            f"Coming back to our thread: reply YES and I'll {turn.deliverable()}.",
            f"I can't help with {subject}, sorry. On your listing though: reply YES and I'll {turn.deliverable()}.",
        ], "binary_yes_no", "Out-of-scope request declined politely; redirected to the open proposal.")

    if intent == "question":
        return _answer(turn)

    if intent == "thanks":
        if conv.get("accepted"):
            return _end("Merchant closed with thanks after the work was accepted.")
        return turn.send([f"Anytime! Reply YES whenever you're ready and I'll {turn.deliverable()}.",
                          f"Happy to help. Just reply YES and I'll {turn.deliverable()}."],
                         "binary_yes_no", "Polite thanks with a pending proposal: one light re-offer.")

    # unclear
    if conv.get("last_intent") == "unclear":
        return _wait(3 * 3600, "Two unclear replies in a row; backing off instead of guessing.")
    return turn.send([f"Just to be sure — reply YES and I'll {turn.deliverable()}, or STOP if this isn't useful.",
                      f"Quick check: reply YES and I'll {turn.deliverable()}, or STOP to skip it."],
                     "binary_yes_no", "Unclear reply: one binary nudge tied to the pending proposal.")


def _accept(turn: Turn) -> dict:
    conv, proposal = turn.conv, turn.proposal
    if conv.get("accepted"):
        return turn.send(["Already on it — the draft lands here shortly. Reply CONFIRM once it looks right and it goes live.",
                          "Noted — work is in progress; I'll post the draft here as soon as it's ready."],
                         "binary_confirm_cancel", "Repeat acceptance: confirm work is underway, no new questions.",
                         no_questions=True)
    conv["accepted"] = True
    deliverable = turn.deliverable()
    opener = _pick(turn.conv_id, ["Done", "On it", "Great"])
    if turn.hinglish:
        opener = "Ho gaya"
    action = (proposal or {}).get("action")
    facts = (proposal or {}).get("facts") or {}
    draft = ""
    if action == "share_knowledge" and facts.get("finding"):
        draft = (f" Draft for {turn.pack.customer_noun}: “{first_sentence(facts['finding'])}. "
                 f"Ask us at your next {turn.pack.visit_noun} whether this applies to you.”")
    elif action == "milestone_push" and turn.mv:
        draft = (f" Draft: “Thank you for choosing {turn.mv.name}! If we made your {turn.pack.visit_noun} better, "
                 f"a quick Google review helps others find us.”")
    if action == "customer_outreach_approval":
        body = f"{opener} — sending the reminder now from your number. I'll share any replies here."
        cta = "none"
    else:
        body = (f"{opener} — starting now: I'll {deliverable}. You'll see it here for a final look.{draft} "
                f"Reply CONFIRM once it looks right and it goes live.")
        cta = "binary_confirm_cancel"
    alt = f"Confirmed — I'll {deliverable} right away and post the draft here. Next step is yours: reply CONFIRM to publish."
    response = turn.send([body, alt], cta, "Merchant committed: switching to action mode on the pending proposal "
                                           f"({(proposal or {}).get('reason', 'generic next step')}); no qualifying questions.",
                         no_questions=True)
    return response


def _answer(turn: Turn) -> dict:
    lower = turn.message.lower()
    mv, pack = turn.mv, turn.pack
    lines: list[str] = []
    grounding: list[Any] = []
    if mv and re.search(r"offer|price|cost|rate|kitna|₹|discount|deal", lower):
        if mv.active_offers:
            titles = [o["title"] for o in mv.active_offers]
            lines.append("Your live offer" + ("s are " if len(titles) > 1 else " is ") + " and ".join(titles) + ".")
            grounding += titles
        else:
            lines.append("There's no active offer on your listing right now.")
    if mv and re.search(r"lapsed|customer|patient|client|member|guest|how many|kitne", lower):
        for key, template in AGGREGATE_PHRASES.items():
            value = mv.aggregate.get(key)
            if isinstance(value, (int, float)) and not isinstance(value, bool):
                lines.append(template.format(n=fmt_num(value), noun=pack.customer_noun).capitalize() + ".")
                grounding.append(value)
                grounding += [int(d) for d in re.findall(r"\d+", key)]   # "lapsed_180d_plus" -> 180
                if len(lines) >= 3:
                    break
    if mv and re.search(r"call|view|ctr|click|performance|numbers|traffic", lower):
        for metric in ("views", "calls", "ctr"):
            value = mv.metric(metric)
            if value is not None:
                shown = fmt_pct(value) if metric == "ctr" else fmt_num(value)
                lines.append(f"{METRIC_WORDS[metric].capitalize()}: {shown}.")
                grounding.append(value)
    facts = (turn.proposal or {}).get("facts") or {}
    if re.search(r"source|study|research|where|kahan|which", lower) and facts.get("source"):
        lines.append(f"Source: {facts['source']}.")
    if not lines and facts:
        detail = next((facts[k] for k in ("finding", "delta", "gap", "headline", "what", "title") if facts.get(k)), None)
        if detail:
            lines.append(f"What I'm working from: {first_sentence(detail)}.")
    if not lines:
        lines.append("I don't have that detail in front of me, so I won't guess.")
    answer = " ".join(lines[:3])
    return turn.send([f"{answer} Reply YES and I'll {turn.deliverable()}.",
                      f"{answer} If useful, reply YES and I'll {turn.deliverable()}."],
                     "binary_yes_no", "Answered from grounded context only, then re-offered the single pending action.",
                     grounding=grounding)


def _customer_reply(turn: Turn) -> dict:
    """Replies from the merchant's customer (send_as merchant_on_behalf conversations)."""
    text, conv, engine = turn.message, turn.conv, turn.engine
    customer_id = conv.get("customer_id") or turn.request.get("customer_id")
    name = turn.mv.name if turn.mv else "our team"
    if _OPT_OUT.search(text):
        if customer_id:
            engine.opted_out_customers.add(customer_id)
        conv["ended_reason"] = "opt_out"
        return _end("Customer opted out; suppressing further messages to this customer.")
    if _AUTO.search(text):
        return _wait(86400, "Customer-side auto-reply; waiting for a human.")
    slots = (turn.proposal or {}).get("slots") or []
    choice = _SLOT_CHOICE.match(text)
    if choice and slots and int(choice.group(1)) <= len(slots):
        slot = slots[int(choice.group(1)) - 1]
        return turn.send([f"Confirmed — {slot} is yours. {name} will see you then; reply here if anything changes.",
                          f"Done — booked for {slot}. Reply here if you need to change it."],
                         "none", f"Customer picked slot {choice.group(1)}; confirming exactly the offered label.",
                         grounding=[slot, int(choice.group(1))])
    if _DECLINE.search(text) or _DEFER.search(text):
        return turn.send(["No problem at all — just message us here whenever it suits you.",
                          "Sure — reply here anytime and we'll set it up."], "none",
                         "Customer not ready; no pressure, conversation left open.")
    if _ACCEPT.search(text) or re.search(r"\bconfirm\b", text, re.I):
        verb = "dispatch" if turn.pack.slug == "pharmacies" else "confirm"
        return turn.send([f"Thank you! We'll {verb} it and the {name} team will keep everything ready.",
                          f"Noted, thank you — {name} will take it from here."], "none",
                         "Customer accepted; confirming without adding new offers or claims.")
    return turn.send([f"Thanks for your message — the {name} team will get back to you on this shortly.",
                      f"Got it — someone from {name} will reply to you here soon."], "none",
                     "Customer asked something outside the reminder; handing to the merchant's team.")
