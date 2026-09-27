"""Signal extraction, gating and scoped facts.

Everything here is pure and deterministic. The decision layer asks three
questions of this module:

  * What is true about this merchant/customer?   -> MerchantView / CustomerView
  * May Vera message this recipient at all?      -> merchant_gate / customer_gate
  * Is the trigger's claim backed by the data?    -> verify_perf_claim

A ``Fact`` is one piece of evidence *as it will appear in the message* plus
the raw values that ground it. The validator only ever sees the facts chosen
for a decision (the scoped fact bank), never the whole context.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Any, Iterable, Optional

MONTHS = ("Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec")
METRIC_WORDS = {"views": "profile views", "calls": "calls", "ctr": "click-through rate",
                "directions": "direction requests", "leads": "leads", "review_count": "reviews"}
SIGNIFICANT_DELTA = 0.10     # |7-day change| worth talking about
MEANINGFUL_PEER_GAP = 0.10   # relative gap vs peer average worth talking about


# ---------------------------------------------------------------- formatting
def fmt_num(value: float) -> str:
    """Indian digit grouping: 2410 -> 2,410; 125000 -> 1,25,000; 2.5 -> 2.5."""
    if value != int(value):
        return f"{value:.1f}".rstrip("0").rstrip(".")
    digits = str(abs(int(value)))
    if len(digits) > 3:
        head, tail = digits[:-3], digits[-3:]
        groups = []
        while len(head) > 2:
            groups.insert(0, head[-2:])
            head = head[:-2]
        digits = ",".join(([head] if head else []) + groups + [tail])
    return ("-" if value < 0 else "") + digits


def fmt_pct(fraction: float) -> str:
    """0.021 -> '2.1%', -0.5 -> '50%' (sign is carried by the wording)."""
    pct = round(abs(fraction) * 100, 1)
    return f"{int(pct) if pct == int(pct) else pct}%"


def fmt_money(value: float) -> str:
    return "₹" + fmt_num(value)


def parse_iso(value: Any) -> Optional[datetime]:
    if not isinstance(value, str) or not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        try:
            return datetime.combine(date.fromisoformat(value[:10]), datetime.min.time())
        except ValueError:
            return None


def fmt_date(value: Any) -> Optional[str]:
    """'2026-11-12' -> '12 Nov'."""
    parsed = parse_iso(value)
    return f"{parsed.day} {MONTHS[parsed.month - 1]}" if parsed else None


_TOKEN_FIXES = {"ctr": "CTR", "gbp": "Google profile", "ors": "ORS", "rx": "Rx", "pt": "PT",
                "rct": "RCT", "ipl": "IPL", "yoy": "YoY", "otc": "OTC"}


def humanize(token: Any) -> str:
    """'kids_yoga_summer_camp' -> 'kids yoga summer camp'; 'ORS_demand_+40' -> 'ORS demand +40%'."""
    if not isinstance(token, str):
        return ""
    text = re.sub(r"_([+-]\d+)$", r" \1%", token.strip())
    words = [w for w in re.split(r"[_\s]+", text) if w]
    return " ".join(_TOKEN_FIXES.get(w.lower(), w) for w in words)


def first_name(raw: Any) -> str:
    """Normalise owner/customer names: 'Dr. Sameer' -> 'Sameer'."""
    if not isinstance(raw, str):
        return ""
    name = re.sub(r"^\s*(dr\.?|doctor)\s+", "", raw.strip(), flags=re.I)
    return name.split("(")[0].strip()


_ABBREVIATIONS = {"dr", "mr", "mrs", "ms", "no", "vs", "p", "pp", "st", "mfr", "approx", "e.g", "i.e", "etc"}


def first_sentence(text: Any) -> str:
    """First sentence without the trailing period; 'Speaker: Dr. R. Mehta. Covers…' -> 'Speaker: Dr. R. Mehta'."""
    if not isinstance(text, str):
        return ""
    text = text.strip()
    for m in re.finditer(r"[.!?](?=\s+|$)", text):
        before = re.search(r"([A-Za-z.]+)$", text[:m.start()])
        word = before.group(1).lower() if before else ""
        if m.group(0) == "." and (word in _ABBREVIATIONS or (len(word) == 1 and word.isalpha())):
            continue
        return text[:m.start() + (0 if m.group(0) == "." else 1)]
    return text.rstrip(".")


# --------------------------------------------------------------------- facts
@dataclass(frozen=True)
class Fact:
    key: str
    text: str                      # exactly as rendered in the body
    values: tuple = ()             # raw numbers/strings that ground ``text``
    source: str = ""               # where it came from (for the rationale)


@dataclass
class Evidence:
    facts: dict[str, Fact] = field(default_factory=dict)

    def add(self, fact: Optional[Fact]) -> Optional[Fact]:
        if fact is not None:
            self.facts[fact.key] = fact
        return fact

    def get(self, key: str) -> Optional[Fact]:
        return self.facts.get(key)

    def text(self, key: str, default: str = "") -> str:
        fact = self.facts.get(key)
        return fact.text if fact else default

    def grounding(self) -> list[Any]:
        """Everything the validator may accept: rendered text plus raw values."""
        out: list[Any] = []
        for fact in self.facts.values():
            out.append(fact.text)
            out.extend(fact.values)
        return out

    def sources(self) -> list[str]:
        return sorted({f.source for f in self.facts.values() if f.source})


# ------------------------------------------------------------ merchant view
@dataclass
class MerchantView:
    raw: dict
    merchant_id: str
    category: str
    name: str
    owner: str
    locality: str
    city: str
    languages: tuple[str, ...]
    verified: Optional[bool]
    perf: dict
    delta: dict
    active_offers: list[dict]
    expired_offers: list[dict]
    subscription: dict
    aggregate: dict
    themes: list[dict]
    history: list[dict]
    signals: dict[str, Optional[str]]

    @property
    def speaks_hindi(self) -> bool:
        return "hi" in self.languages

    def metric(self, name: str) -> Optional[float]:
        value = self.perf.get(name)
        return float(value) if _is_num(value) else None

    def delta_of(self, metric: str) -> Optional[float]:
        value = self.delta.get(f"{metric}_pct")
        return float(value) if _is_num(value) else None

    def open_intent(self) -> Optional[dict]:
        """Latest merchant turn that expressed intent and was not followed by Vera."""
        for turn in reversed(self.history):
            if turn.get("from") == "vera":
                return None
            if turn.get("from") == "merchant" and str(turn.get("engagement", "")).startswith("intent"):
                return turn
        return None

    def previous_bodies(self) -> list[str]:
        return [t["body"] for t in self.history if t.get("from") == "vera" and isinstance(t.get("body"), str)]


def merchant_view(merchant: dict) -> MerchantView:
    identity = _dict(merchant.get("identity"))
    perf = _dict(merchant.get("performance"))
    offers = [o for o in merchant.get("offers") or [] if isinstance(o, dict) and isinstance(o.get("title"), str)]
    return MerchantView(
        raw=merchant,
        merchant_id=str(merchant.get("merchant_id") or ""),
        category=str(merchant.get("category_slug") or ""),
        name=str(identity.get("name") or "").strip(),
        owner=first_name(identity.get("owner_first_name")),
        locality=str(identity.get("locality") or "").strip(),
        city=str(identity.get("city") or "").strip(),
        languages=tuple(l for l in identity.get("languages") or [] if isinstance(l, str)),
        verified=identity.get("verified") if isinstance(identity.get("verified"), bool) else None,
        perf=perf,
        delta=_dict(perf.get("delta_7d")),
        active_offers=[o for o in offers if o.get("status") == "active"],
        expired_offers=[o for o in offers if o.get("status") == "expired"],
        subscription=_dict(merchant.get("subscription")),
        aggregate=_dict(merchant.get("customer_aggregate")),
        themes=[t for t in merchant.get("review_themes") or [] if isinstance(t, dict)],
        history=[t for t in merchant.get("conversation_history") or [] if isinstance(t, dict)],
        signals=_parse_signals(merchant.get("signals")),
    )


def _parse_signals(raw: Any) -> dict[str, Optional[str]]:
    out: dict[str, Optional[str]] = {}
    for item in raw if isinstance(raw, list) else []:
        if isinstance(item, str) and item:
            name, _, value = item.partition(":")
            out[name] = value or None
    return out


# ---------------------------------------------------------- merchant facts
def metric_fact(mv: MerchantView, metric: str, key: Optional[str] = None) -> Optional[Fact]:
    value = mv.metric(metric)
    if value is None:
        return None
    window = mv.perf.get("window_days")
    rendered = fmt_pct(value) if metric == "ctr" else fmt_num(value)
    suffix = f" in the last {int(window)} days" if _is_num(window) and metric != "ctr" else ""
    return Fact(key or metric, f"{rendered} {METRIC_WORDS.get(metric, metric)}{suffix}",
                (value, window) if suffix else (value,), f"merchant.performance.{metric}")


def delta_fact(mv: MerchantView, metric: str) -> Optional[Fact]:
    change = mv.delta_of(metric)
    if change is None:
        return None
    direction = "up" if change > 0 else "down" if change < 0 else "flat"
    text = f"{METRIC_WORDS.get(metric, metric)} {direction} {fmt_pct(change)} this week" if change else \
        f"{METRIC_WORDS.get(metric, metric)} flat this week"
    return Fact(f"delta_{metric}", text, (change,), f"merchant.performance.delta_7d.{metric}_pct")


PEER_KEYS = {"ctr": "avg_ctr", "calls": "avg_calls_30d", "views": "avg_views_30d",
             "directions": "avg_directions_30d"}


def peer_gap(mv: MerchantView, category: Optional[dict], metric: str) -> Optional[tuple[float, float, float]]:
    """(merchant value, peer value, relative gap) or None when not comparable."""
    peers = _dict((category or {}).get("peer_stats"))
    peer = peers.get(PEER_KEYS.get(metric, ""))
    mine = mv.metric(metric)
    if mine is None or not _is_num(peer) or peer <= 0:
        return None
    return mine, float(peer), (mine - float(peer)) / float(peer)


def peer_fact(mv: MerchantView, category: Optional[dict], metric: str) -> Optional[Fact]:
    gap = peer_gap(mv, category, metric)
    if gap is None or abs(gap[2]) < MEANINGFUL_PEER_GAP:
        return None
    mine, peer, rel = gap
    peers_label = f"similar {mv.category}" if mv.category else "similar businesses"
    if metric == "ctr":
        text = f"your click-through rate is {fmt_pct(mine)} vs a {fmt_pct(peer)} average for {peers_label}"
    else:
        text = f"you're at {fmt_num(mine)} {METRIC_WORDS.get(metric, metric)} vs a {fmt_num(peer)} average for {peers_label}"
    return Fact(f"peer_{metric}", text, (mine, peer), f"category.peer_stats.{PEER_KEYS[metric]}")


def best_peer_fact(mv: MerchantView, category: Optional[dict], want: str) -> Optional[Fact]:
    """Largest gap in the wanted direction ('below' or 'above') across metrics."""
    best, best_gap = None, 0.0
    for metric in ("calls", "ctr", "views"):
        gap = peer_gap(mv, category, metric)
        if gap is None:
            continue
        rel = gap[2]
        if (want == "below" and rel <= -MEANINGFUL_PEER_GAP and rel < best_gap) or \
                (want == "above" and rel >= MEANINGFUL_PEER_GAP and rel > best_gap):
            best, best_gap = metric, rel
    return peer_fact(mv, category, best) if best else None


def offer_fact(mv: MerchantView, key: str = "offer") -> Optional[Fact]:
    """Prefer service@price offers (brief §3.3) among the merchant's active offers."""
    if not mv.active_offers:
        return None
    ranked = sorted(mv.active_offers, key=lambda o: (0 if "₹" in o["title"] or "@" in o["title"] else 1,
                                                      str(o.get("id", ""))))
    title = ranked[0]["title"].strip()
    return Fact(key, title, (title,), "merchant.offers")


def signal_days(mv: MerchantView, name: str) -> Optional[int]:
    value = mv.signals.get(name)
    match = re.match(r"^(\d+)d$", value or "")
    return int(match.group(1)) if match else None


# ------------------------------------------------------------ category facts
def digest_item(category: Optional[dict], item_id: Any = None, kinds: Iterable[str] = ()) -> Optional[dict]:
    items = [d for d in (category or {}).get("digest") or [] if isinstance(d, dict)]
    if isinstance(item_id, str):
        for item in items:
            if item.get("id") == item_id:
                return item
    wanted = tuple(kinds)
    for item in sorted(items, key=lambda d: str(d.get("id", ""))):
        if item.get("kind") in wanted:
            return item
    return None


def seasonal_beat(category: Optional[dict], month: Optional[int]) -> Optional[dict]:
    if not month:
        return None
    for beat in (category or {}).get("seasonal_beats") or []:
        if isinstance(beat, dict) and month in _months_in(str(beat.get("month_range", ""))):
            return beat
    return None


def _months_in(spec: str) -> set[int]:
    names = [m.lower() for m in MONTHS]
    found = [names.index(tok.lower()[:3]) + 1 for tok in re.findall(r"[A-Za-z]{3,}", spec)
             if tok.lower()[:3] in names]
    if len(found) == 2:
        start, end = found
        return {((start - 1 + i) % 12) + 1 for i in range(((end - start) % 12) + 1)}
    return set(found)


# ------------------------------------------------------------ customer view
@dataclass
class CustomerView:
    raw: dict
    customer_id: str
    merchant_id: str
    name: str
    addressee: str                 # who the message greets (parent/son channels)
    via: Optional[str]             # "parent" / "son" / None
    language: str                  # "en" | "hi" | "hi-en"
    state: str
    senior: bool
    last_visit: Optional[str]
    visits: Optional[int]
    services: list[str]
    preferences: dict
    consent_scope: tuple[str, ...]
    opted_in: bool
    reminder_opt_in: Optional[bool]


def customer_view(customer: dict) -> CustomerView:
    identity = _dict(customer.get("identity"))
    rel = _dict(customer.get("relationship"))
    prefs = _dict(customer.get("preferences"))
    consent = _dict(customer.get("consent"))
    raw_name = str(identity.get("name") or "")
    parent = re.search(r"\(parent:\s*([^)]+)\)", raw_name)
    channel = str(prefs.get("channel") or "")
    via = "parent" if parent or "parent" in channel else "son" if "son" in channel else None
    name = "" if raw_name.startswith("(") else first_name(raw_name)
    lang_raw = str(identity.get("language_pref") or "en").lower()
    language = "hi" if lang_raw == "hi" else "hi-en" if lang_raw.startswith("hi") else "en"
    return CustomerView(
        raw=customer,
        customer_id=str(customer.get("customer_id") or ""),
        merchant_id=str(customer.get("merchant_id") or ""),
        name=name,
        addressee=parent.group(1).strip() if parent else name,
        via=via,
        language=language,
        state=str(customer.get("state") or ""),
        senior=bool(identity.get("senior_citizen")),
        last_visit=rel.get("last_visit") if isinstance(rel.get("last_visit"), str) else None,
        visits=rel.get("visits_total") if _is_num(rel.get("visits_total")) else None,
        services=[s for s in rel.get("services_received") or [] if isinstance(s, str) and s != "..."],
        preferences=prefs,
        consent_scope=tuple(s for s in consent.get("scope") or [] if isinstance(s, str)),
        opted_in=bool(consent.get("opted_in_at")),
        reminder_opt_in=prefs.get("reminder_opt_in") if isinstance(prefs.get("reminder_opt_in"), bool) else None,
    )


# ------------------------------------------------------------------ gating
CONSENT_SCOPES = {
    "recall_due": {"recall_reminders", "appointment_reminders"},
    "appointment_tomorrow": {"appointment_reminders"},
    "chronic_refill_due": {"refill_reminders", "delivery_notifications"},
    "customer_lapsed_soft": {"winback_offers", "promotional_offers", "renewal_reminders", "recall_reminders"},
    "customer_lapsed_hard": {"winback_offers", "promotional_offers", "renewal_reminders", "recall_reminders"},
    "trial_followup": {"program_updates", "kids_program_updates", "appointment_reminders"},
    "wedding_package_followup": {"bridal_package_followup", "appointment_reminders"},
}
TRANSACTIONAL = {"recall_due", "appointment_tomorrow", "chronic_refill_due", "trial_followup"}


@dataclass(frozen=True)
class Gate:
    ok: bool
    reason: str


def customer_gate(cv: CustomerView, kind: str, merchant_id: str, opted_out: set[str]) -> Gate:
    if cv.customer_id in opted_out:
        return Gate(False, "customer opted out")
    if cv.merchant_id and merchant_id and cv.merchant_id != merchant_id:
        return Gate(False, "customer belongs to a different merchant")
    if not cv.opted_in or not cv.consent_scope:
        return Gate(False, "no recorded consent")
    if str(cv.preferences.get("channel", "")).startswith("none"):
        return Gate(False, "no reachable channel")
    wanted = CONSENT_SCOPES.get(kind, {"promotional_offers", "appointment_reminders"})
    if wanted & set(cv.consent_scope):
        return Gate(True, "consent scope matches")
    if kind in TRANSACTIONAL and cv.reminder_opt_in:
        return Gate(True, "reminder opt-in covers a service reminder")
    return Gate(False, "consent scope does not cover this message")


def merchant_gate(merchant_id: str, now: Optional[datetime], opted_out: set[str],
                  snoozed_until: dict[str, datetime]) -> Gate:
    if merchant_id in opted_out:
        return Gate(False, "merchant opted out")
    until = snoozed_until.get(merchant_id)
    if until is not None and now is not None and _aware(now) < _aware(until):
        return Gate(False, "merchant asked for time")
    return Gate(True, "ok")


# ------------------------------------------------------------ contradiction
@dataclass(frozen=True)
class PerfClaim:
    status: str                    # "verified" | "contradicted" | "unverifiable"
    metric: Optional[str]
    change: Optional[float]        # the change Vera may state (merchant data wins)


def verify_perf_claim(kind: str, payload: dict, mv: MerchantView) -> PerfClaim:
    """Check a perf trigger against the merchant's own 7-day deltas.

    Merchant data is the verifiable source (the judge sees it), so it wins:
    a dip claim with no negative merchant delta is ``contradicted``.
    """
    want_down = kind in ("perf_dip", "seasonal_perf_dip")
    metric = payload.get("metric") if isinstance(payload.get("metric"), str) else None
    claimed = payload.get("delta_pct") if _is_num(payload.get("delta_pct")) else None

    if metric and mv.delta_of(metric) is not None:
        actual = mv.delta_of(metric)
        agrees = actual <= -SIGNIFICANT_DELTA if want_down else actual >= SIGNIFICANT_DELTA
        return PerfClaim("verified" if agrees else "contradicted", metric, actual if agrees else None)

    # No comparable merchant delta: take the strongest delta in the claimed direction.
    candidates = [(m, mv.delta_of(m)) for m in ("calls", "views", "ctr") if mv.delta_of(m) is not None]
    if candidates:
        pick = min(candidates, key=lambda c: c[1]) if want_down else max(candidates, key=lambda c: c[1])
        agrees = pick[1] <= -SIGNIFICANT_DELTA if want_down else pick[1] >= SIGNIFICANT_DELTA
        return PerfClaim("verified" if agrees else "contradicted", pick[0], pick[1] if agrees else None)
    if metric and claimed is not None:
        return PerfClaim("unverifiable", metric, claimed)
    return PerfClaim("unverifiable", None, None)


# ------------------------------------------------------------------ helpers
def _dict(value: Any) -> dict:
    return value if isinstance(value, dict) else {}


def _is_num(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def _aware(value: datetime) -> datetime:
    from datetime import timezone
    return value if value.tzinfo else value.replace(tzinfo=timezone.utc)


is_num = _is_num
as_dict = _dict
aware = _aware
