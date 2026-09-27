"""Decision layer: trigger -> plan -> rank -> one action per merchant per tick.

Pipeline per available trigger:
    resolve contexts -> gate (merchant, suppression, consent)
    -> family builder picks the evidence and the action (or declines)
    -> priority score
Then per merchant: highest priority plan whose composition validates wins;
the rest are deferred (not suppressed) and may go out on a later tick.

Priority (RESEARCH.md §5.3):
    urgency*10 (documented ranker)
    + time sensitivity   (same-day event, hard deadline, stock run-out)
    + impact             (large verified metric move, compliance, revenue at risk)
    + merchant goal      (merchant explicitly asked for this)
    - penalties          (contradicted claim pivoted, unverifiable placeholder, irrelevant festival)
Ties: earliest expires_at, then trigger id. No randomness anywhere.
"""

from __future__ import annotations

import hashlib
import logging
import re
import threading
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Callable, Optional, Union

from composer import Composed, Plan, _grounding, compose, humanize_service
from signals import (Evidence, Fact, MerchantView, best_peer_fact, customer_gate, customer_view, first_sentence,
                     delta_fact, digest_item, fmt_date, fmt_money, fmt_num, fmt_pct, humanize, is_num,
                     merchant_gate, merchant_view, metric_fact, offer_fact, parse_iso, seasonal_beat,
                     verify_perf_claim, METRIC_WORDS, SIGNIFICANT_DELTA, as_dict, aware)
from state import ContextStore
from strategies import pack_for
from strategies.base import CategoryPack

log = logging.getLogger("vera.decision")
MAX_ACTIONS_PER_TICK = 20

FAMILY_OF_KIND = {
    "research_digest": "knowledge", "research_digest_release": "knowledge",
    "category_research_digest_release": "knowledge", "cde_opportunity": "knowledge",
    "category_trend_movement": "knowledge",
    "regulation_change": "compliance", "supply_alert": "compliance",
    "perf_dip": "perf", "seasonal_perf_dip": "perf", "perf_spike": "perf",
    "milestone_reached": "milestone", "review_theme_emerged": "reviews",
    "competitor_opened": "competitor",
    "festival_upcoming": "seasonal", "category_seasonal": "seasonal",
    "ipl_match_today": "event",
    "renewal_due": "lifecycle", "winback_eligible": "lifecycle", "dormant_with_vera": "lifecycle",
    "gbp_unverified": "lifecycle",
    "curious_ask_due": "curious", "active_planning_intent": "planning",
    "recall_due": "customer", "appointment_tomorrow": "customer", "chronic_refill_due": "customer",
    "customer_lapsed_soft": "customer", "customer_lapsed_hard": "customer", "trial_followup": "customer",
    "wedding_package_followup": "customer", "unplanned_slot_open": "customer",
}


@dataclass
class Ctx:
    trigger_id: str
    trigger: dict
    kind: str
    payload: dict
    mv: MerchantView
    category: Optional[dict]
    pack: CategoryPack
    customer: Optional[dict]
    now: Optional[datetime]
    engine: "VeraEngine"


@dataclass(frozen=True)
class Skip:
    reason: str


Built = Union[Plan, Skip]


# ------------------------------------------------------------ plan helpers
def _plan(c: Ctx, family: str, action: str, mode: str, reason: str, ev: Evidence,
          cta_type: str = "binary_yes_no", send_as: str = "vera", cv=None) -> Plan:
    return Plan(trigger_id=c.trigger_id, kind=c.kind, family=family, action=action, mode=mode,
                send_as=send_as, cta_type=cta_type, reason=reason, evidence=ev, pack=c.pack, mv=c.mv,
                trigger=c.trigger, category=c.category, cv=cv)


def _num(value: Any) -> Optional[float]:
    return float(value) if is_num(value) else None


def _placeholder(c: Ctx) -> bool:
    return bool(c.payload.get("placeholder")) or not c.payload


def _metric_word(metric: str) -> str:
    return METRIC_WORDS.get(metric, humanize(metric))


def _change_fact(metric: str, change: float, key: str = "delta") -> Fact:
    direction = "up" if change > 0 else "down"
    return Fact(key, f"{_metric_word(metric)} {direction} {fmt_pct(change)} this week", (change,),
                "merchant.performance.delta_7d")


def _cohort_fact(mv: MerchantView, segment: Any, pack: CategoryPack) -> Optional[Fact]:
    if not isinstance(segment, str) or not segment:
        return None
    stem = segment.rstrip("s")
    for key, value in sorted(mv.aggregate.items()):
        if is_num(value) and key.startswith(stem):
            return Fact("cohort", f"your {fmt_num(value)} {humanize(segment).lower().replace('adults', 'adult')} "
                                  f"{pack.customer_noun}", (value,), f"merchant.customer_aggregate.{key}")
    return Fact("cohort", f"{humanize(segment).lower()} in your {pack.business_noun}", (), "digest.patient_segment")


def _split_camel(text: str) -> str:
    return re.sub(r"(?<=[a-z])(?=[A-Z])", " ", text)


# ------------------------------------------------------------ family builders
def b_knowledge(c: Ctx) -> Built:
    kinds = {"cde_opportunity": ("cde",), "category_trend_movement": ("trend",)}.get(c.kind, ("research",))
    item = digest_item(c.category, c.payload.get("top_item_id") or c.payload.get("digest_item_id"), kinds)
    if not item or not item.get("title"):
        return b_generic(c, "no matching digest item in the category context")
    ev = Evidence()
    ev.add(Fact("title", str(item["title"]).rstrip("."), (item["title"],), f"digest.{item.get('id')}"))
    ev.add(Fact("source", str(item.get("source") or "This week's digest"), (item.get("source", ""),), "digest.source"))
    finding = first_sentence(item.get("summary"))
    if is_num(item.get("trial_n")) and finding:
        finding = f"{fmt_num(item['trial_n'])}-patient trial — {finding[0].lower() + finding[1:]}"
    if finding:
        ev.add(Fact("finding", finding, (item.get("summary", ""), item.get("trial_n")), "digest.summary"))
    if c.kind == "cde_opportunity":
        when = fmt_date(item.get("date"))
        parts = [f"on {when}" if when else "",
                 f"{fmt_num(c.payload['credits'])} CDE credits" if is_num(c.payload.get("credits")) else "",
                 first_sentence(item.get("actionable"))]
        ev.add(Fact("event_detail", ", ".join(p for p in parts if p),
                    (item.get("date", ""), c.payload.get("credits"), item.get("actionable", "")), "digest.cde"))
    else:
        ev.add(_cohort_fact(c.mv, item.get("patient_segment"), c.pack))
    action = "event_register" if c.kind == "cde_opportunity" else "share_knowledge"
    return _plan(c, "knowledge", action, c.kind, f"new {item.get('kind', 'research')} item: {item['title']}", ev)


def b_compliance(c: Ctx) -> Built:
    ev = Evidence()
    if c.kind == "supply_alert":
        item = digest_item(c.category, c.payload.get("alert_id"), ("alert", "supply"))
        molecule, batches = c.payload.get("molecule"), c.payload.get("affected_batches")
        if not isinstance(molecule, str) or not isinstance(batches, list) or not batches:
            return b_generic(c, "supply alert without molecule/batch details")
        ev.add(Fact("molecule", molecule, (molecule,), "trigger.payload.molecule"))
        ev.add(Fact("batches", " and ".join(str(b) for b in batches), tuple(str(b) for b in batches), "trigger.payload"))
        if isinstance(c.payload.get("manufacturer"), str):
            ev.add(Fact("manufacturer", _split_camel(c.payload["manufacturer"]), (c.payload["manufacturer"],), "trigger.payload"))
        if item:
            ev.add(Fact("source", str(item.get("source", "")), (item.get("source", ""),), "digest.source"))
            risk = next((s for s in re.split(r"(?<=[.!?])\s+", str(item.get("summary", ""))) if "risk" in s.lower()),
                        first_sentence(item.get("summary")))
            ev.add(Fact("risk", risk.rstrip("."), (item.get("summary", ""),), "digest.summary"))
        rx = _num(c.mv.aggregate.get("chronic_rx_count"))
        if rx:
            ev.add(Fact("rx_base", f"{fmt_num(rx)} chronic-Rx customers", (rx,), "merchant.customer_aggregate.chronic_rx_count"))
        return _plan(c, "compliance", "compliance_alert", "supply", f"batch recall on {molecule}", ev)

    item = digest_item(c.category, c.payload.get("top_item_id"), ("compliance",))
    if not item:
        return b_generic(c, "regulation trigger without a compliance digest item")
    ev.add(Fact("title", str(item["title"]).rstrip("."), (item["title"],), f"digest.{item.get('id')}"))
    ev.add(Fact("source", str(item.get("source", "")), (item.get("source", ""),), "digest.source"))
    ev.add(Fact("finding", first_sentence(item.get("summary")), (item.get("summary", ""),), "digest.summary"))
    deadline = fmt_date(c.payload.get("deadline_iso"))
    action_line = first_sentence(item.get("actionable"))
    if deadline:
        action_line = f"Deadline {deadline}" + (f" — {action_line[0].lower() + action_line[1:]}" if action_line else "")
    ev.add(Fact("action_line", action_line, (c.payload.get("deadline_iso", ""), item.get("actionable", "")), "digest.actionable"))
    return _plan(c, "compliance", "compliance_alert", "regulation", f"compliance change: {item['title']}", ev)


def b_perf(c: Ctx) -> Built:
    claim = verify_perf_claim(c.kind, c.payload, c.mv)
    ev = Evidence()
    seasonal = c.kind == "seasonal_perf_dip" or bool(c.payload.get("is_expected_seasonal"))
    if c.kind in ("perf_dip", "seasonal_perf_dip") and claim.status == "verified" \
            or (seasonal and claim.status == "unverifiable" and claim.change is not None):
        ev.add(_change_fact(claim.metric, claim.change))
        if seasonal:
            beat = seasonal_beat(c.category, c.now.month if c.now else None)
            ev.add(Fact("season", beat["month_range"] if beat else humanize(c.payload.get("season_note", "this season")),
                        (beat or {}).get("month_range", ""), "category.seasonal_beats"))
            if beat:
                ev.add(Fact("season_note", beat["note"], (beat["note"],), "category.seasonal_beats"))
            base = _num(c.mv.aggregate.get("total_active_members")) or _num(c.mv.aggregate.get("total_unique_ytd"))
            if base:
                ev.add(Fact("base", f"{fmt_num(base)} active {c.pack.customer_noun}", (base,), "merchant.customer_aggregate"))
            return _plan(c, "perf", "fix_dip", "seasonal", f"expected seasonal dip in {claim.metric}", ev)
        baseline = _num(c.payload.get("vs_baseline"))
        if baseline and claim.metric == c.payload.get("metric"):
            now_value = round(baseline * (1 + claim.change))
            ev.add(Fact("baseline", f"{fmt_num(now_value)} vs a usual {fmt_num(baseline)}", (baseline, now_value), "trigger.payload.vs_baseline"))
        peer = best_peer_fact(c.mv, c.category, "below")
        if peer is not None:
            ev.add(Fact("context", peer.text, peer.values, peer.source))
        return _plan(c, "perf", "fix_dip", "dip", f"verified {claim.metric} drop", ev)

    if c.kind == "perf_spike" and claim.status == "verified":
        ev.add(_change_fact(claim.metric, claim.change))
        if isinstance(c.payload.get("likely_driver"), str):
            ev.add(Fact("driver", f"your {humanize(c.payload['likely_driver']).lower()}", (), "trigger.payload.likely_driver"))
        ev.add(metric_fact(c.mv, claim.metric, "context") if claim.metric != "ctr" else None)
        return _plan(c, "perf", "ride_spike", "spike", f"verified {claim.metric} rise", ev)

    # Claim contradicted or unverifiable: never repeat it; pivot to a verified benchmark or stay quiet.
    want = "above" if c.kind == "perf_spike" else "below"
    gap = best_peer_fact(c.mv, c.category, want)
    if gap is None and want == "above" and claim.status == "contradicted":
        want, gap = "below", best_peer_fact(c.mv, c.category, "below")
    if gap is None:
        return Skip(f"{c.kind} claim {claim.status} by merchant data and no honest alternative")
    ev.add(Fact("gap", gap.text, gap.values, gap.source))
    if want == "below" and claim.status == "contradicted":
        metric = c.payload.get("metric") if isinstance(c.payload.get("metric"), str) else (claim.metric or "calls")
        actual = delta_fact(c.mv, metric) or delta_fact(c.mv, "calls")
        if actual is not None:
            ev.add(Fact("actual", actual.text, actual.values, actual.source))
            return _plan(c, "perf", "benchmark_gap", "gap_honest", f"{c.kind} contradicted; benchmark gap instead", ev)
    mode, action = ("strength", "ride_spike") if want == "above" else ("gap", "benchmark_gap")
    return _plan(c, "perf", action, mode, f"{c.kind} {claim.status}; verified benchmark instead", ev)


def b_milestone(c: Ctx) -> Built:
    now_v, target, metric = _num(c.payload.get("value_now")), _num(c.payload.get("milestone_value")), c.payload.get("metric")
    if now_v is None or target is None or not isinstance(metric, str) or now_v >= target:
        return b_perf_strength(c, "milestone details missing")
    word = _metric_word(metric)
    ev = Evidence()
    ev.add(Fact("now", f"{fmt_num(now_v)} {word}", (now_v,), "trigger.payload.value_now"))
    ev.add(Fact("target", f"{fmt_num(target)}", (target,), "trigger.payload.milestone_value"))
    ev.add(Fact("remaining", f"{fmt_num(target - now_v)}", (target - now_v,), "derived: milestone - now"))
    return _plan(c, "milestone", "milestone_push", "imminent", f"{fmt_num(target - now_v)} {word} from {fmt_num(target)}", ev)


def b_perf_strength(c: Ctx, why: str) -> Built:
    gap = best_peer_fact(c.mv, c.category, "above")
    if gap is None:
        return b_generic(c, why)
    ev = Evidence()
    ev.add(Fact("gap", gap.text, gap.values, gap.source))
    return _plan(c, "perf", "ride_spike", "strength", f"{why}; verified strength instead", ev)


def b_reviews(c: Ctx) -> Built:
    theme = c.payload.get("theme") if isinstance(c.payload.get("theme"), str) else None
    count, quote, sentiment = _num(c.payload.get("occurrences_30d")), c.payload.get("common_quote"), None
    known = {t.get("theme"): t for t in c.mv.themes}
    if theme is None and c.mv.themes:
        pick = sorted(c.mv.themes, key=lambda t: (t.get("sentiment") != "neg", -(_num(t.get("occurrences_30d")) or 0),
                                                  str(t.get("theme"))))[0]
        theme, count, quote = pick.get("theme"), _num(pick.get("occurrences_30d")), pick.get("common_quote")
    if not theme or not count:
        return b_generic(c, "no review theme data")
    sentiment = (known.get(theme) or {}).get("sentiment") or ("neg" if c.payload.get("trend") == "rising" else "neg")
    ev = Evidence()
    ev.add(Fact("count", f"{fmt_num(count)} reviews in the last 30 days", (count,), "review_theme.occurrences_30d"))
    ev.add(Fact("theme", humanize(theme).lower(), (), "review_theme.theme"))
    if isinstance(quote, str) and quote:
        ev.add(Fact("quote", quote, (quote,), "review_theme.common_quote"))
    mode = "negative" if sentiment == "neg" else "positive"
    return _plan(c, "reviews", "review_response", mode, f"{mode} review theme: {humanize(theme)}", ev)


def b_competitor(c: Ctx) -> Built:
    name = c.payload.get("competitor_name")
    if not isinstance(name, str) or not name:
        return b_generic(c, "competitor trigger without a named competitor")
    ev = Evidence()
    ev.add(Fact("competitor", name, (name,), "trigger.payload.competitor_name"))
    if is_num(c.payload.get("distance_km")):
        ev.add(Fact("distance", f"{fmt_num(c.payload['distance_km'])} km", (c.payload["distance_km"],), "trigger.payload"))
    if fmt_date(c.payload.get("opened_date")):
        ev.add(Fact("opened", fmt_date(c.payload["opened_date"]), (c.payload["opened_date"],), "trigger.payload"))
    if isinstance(c.payload.get("their_offer"), str):
        ev.add(Fact("their_offer", c.payload["their_offer"], (c.payload["their_offer"],), "trigger.payload"))
    ev.add(offer_fact(c.mv))
    return _plan(c, "competitor", "competitor_response", "named", f"{name} opened nearby", ev)


def b_seasonal(c: Ctx) -> Built:
    ev = Evidence()
    if c.kind == "festival_upcoming" and isinstance(c.payload.get("festival"), str):
        ev.add(Fact("festival", c.payload["festival"], (c.payload["festival"],), "trigger.payload.festival"))
        ev.add(Fact("date", fmt_date(c.payload.get("date")) or "its date", (c.payload.get("date", ""),), "trigger.payload.date"))
        if is_num(c.payload.get("days_until")):
            ev.add(Fact("days", f"{fmt_num(c.payload['days_until'])} days", (c.payload["days_until"],), "trigger.payload"))
        ev.add(offer_fact(c.mv))
        return _plan(c, "seasonal", "seasonal_play", "festival", f"{c.payload['festival']} ahead", ev)
    trends = c.payload.get("trends")
    if isinstance(trends, list) and trends:
        shown = [humanize(t) for t in trends[:3] if isinstance(t, str)]
        ev.add(Fact("trends", ", ".join(shown), tuple(trends[:3]), "trigger.payload.trends"))
        item = digest_item(c.category, None, ("seasonal",))
        if item and item.get("actionable"):
            ev.add(Fact("action_line", first_sentence(item["actionable"]), (item["actionable"],), "digest.actionable"))
        return _plan(c, "seasonal", "seasonal_play", "trends", "seasonal demand shift", ev)
    beat = seasonal_beat(c.category, c.now.month if c.now else None)
    if beat is None:
        return b_generic(c, "seasonal trigger without details")
    ev.add(Fact("season_note", f"{beat['month_range']}: {beat['note']}", (beat["month_range"], beat["note"]), "category.seasonal_beats"))
    ev.add(offer_fact(c.mv))
    return _plan(c, "seasonal", "seasonal_play", "beat", f"seasonal window {beat['month_range']}", ev)


def b_event(c: Ctx) -> Built:
    match = c.payload.get("match")
    start = parse_iso(c.payload.get("match_time_iso"))
    if not isinstance(match, str) or start is None:
        return b_generic(c, "event trigger without match details")
    ev = Evidence()
    ev.add(Fact("match", match, (match,), "trigger.payload.match"))
    ev.add(Fact("venue", str(c.payload.get("venue") or c.mv.city), (c.payload.get("venue", ""),), "trigger.payload.venue"))
    hour12 = start.hour % 12 or 12
    ev.add(Fact("time", f"{hour12}:{start.minute:02d}{'pm' if start.hour >= 12 else 'am'}",
                (c.payload["match_time_iso"], hour12, start.minute), "trigger.payload.match_time_iso"))
    ev.add(offer_fact(c.mv))
    weekend = c.payload.get("is_weeknight") is False
    if weekend:
        item = next((d for d in (c.category or {}).get("digest") or []
                     if isinstance(d, dict) and "ipl" in str(d.get("title", "")).lower()), None)
        if item:
            ev.add(Fact("insight", first_sentence(item.get("summary")), (item.get("summary", ""),), f"digest.{item.get('id')}"))
            ev.add(Fact("insight_source", str(item.get("source", "")), (item.get("source", ""),), "digest.source"))
    return _plan(c, "event", "event_play_weekend" if weekend else "event_play",
                 "weekend" if weekend else "weeknight", f"{match} today", ev)


def b_lifecycle(c: Ctx) -> Built:
    ev, mv = Evidence(), c.mv
    if c.kind == "renewal_due":
        days = _num(c.payload.get("days_remaining")) or _num(mv.subscription.get("days_remaining"))
        if not days:
            return b_generic(c, "renewal trigger without days remaining")
        ev.add(Fact("days", f"{fmt_num(days)} days", (days,), "subscription.days_remaining"))
        ev.add(Fact("plan", str(c.payload.get("plan") or mv.subscription.get("plan") or "current"),
                    (), "subscription.plan"))
        if is_num(c.payload.get("renewal_amount")):
            ev.add(Fact("amount", fmt_money(c.payload["renewal_amount"]), (c.payload["renewal_amount"],), "trigger.payload"))
        views, calls = mv.metric("views"), mv.metric("calls")
        if views and calls:
            ev.add(Fact("stat", f"{fmt_num(views)} profile views and {fmt_num(calls)} calls", (views, calls), "merchant.performance"))
        return _plan(c, "lifecycle", "renewal", "renewal", f"plan ends in {fmt_num(days)} days", ev)
    if c.kind == "winback_eligible":
        days = _num(c.payload.get("days_since_expiry")) or _num(mv.subscription.get("days_since_expiry"))
        if not days:
            return b_generic(c, "winback trigger without expiry data")
        ev.add(Fact("since", f"{fmt_num(days)} days", (days,), "subscription.days_since_expiry"))
        drop = _num(c.payload.get("perf_dip_pct"))
        if drop and drop < 0:
            ev.add(Fact("drop", f"listing activity is down {fmt_pct(drop)}", (drop,), "trigger.payload.perf_dip_pct"))
        lapsed = _num(c.payload.get("lapsed_customers_added_since_expiry"))
        if lapsed:
            ev.add(Fact("lapsed", f"{fmt_num(lapsed)} more {c.pack.customer_noun} have lapsed", (lapsed,), "trigger.payload"))
        return _plan(c, "lifecycle", "winback_merchant", "winback", f"subscription lapsed {fmt_num(days)} days ago", ev)
    if c.kind == "gbp_unverified":
        if mv.verified is True and _placeholder(c):
            return Skip("profile already verified")
        uplift = _num(c.payload.get("estimated_uplift_pct"))
        if uplift:
            ev.add(Fact("uplift", fmt_pct(uplift), (uplift,), "trigger.payload.estimated_uplift_pct"))
        if isinstance(c.payload.get("verification_path"), str):
            ev.add(Fact("path", humanize(c.payload["verification_path"]).replace(" or ", " or a "), (), "trigger.payload"))
        ev.add(Fact("headline", "your Google profile is still unverified", (), "merchant.identity.verified"))
        return _plan(c, "lifecycle", "verify_profile", "verify", "unverified Google profile", ev)
    # dormant_with_vera
    days = _num(c.payload.get("days_since_last_merchant_message")) or _dormant_days(mv)
    fresh = _strongest_move(mv) or best_peer_fact(mv, c.category, "below") or best_peer_fact(mv, c.category, "above")
    if not days:
        if fresh is None:
            return Skip("dormancy trigger with nothing fresh to share")
        ev.add(Fact("fresh", fresh.text, fresh.values, fresh.source))
        return _plan(c, "lifecycle", "reengage", "checkin", "merchant has gone quiet; sharing one fresh number", ev)
    ev.add(Fact("since", f"{fmt_num(days)} days", (days,), "trigger.payload/signals"))
    if isinstance(c.payload.get("last_topic"), str):
        ev.add(Fact("topic", humanize(c.payload["last_topic"]).lower(), (), "trigger.payload.last_topic"))
    if fresh:
        ev.add(Fact("fresh", fresh.text, fresh.values, fresh.source))
    return _plan(c, "lifecycle", "reengage", "dormant", f"no merchant message for {fmt_num(days)} days", ev)


def _dormant_days(mv: MerchantView) -> Optional[int]:
    for name, value in mv.signals.items():
        match = re.match(r"^dormant_with_vera_(\d+)d$", name)
        if match:
            return int(match.group(1))
    return None


def _strongest_move(mv: MerchantView) -> Optional[Fact]:
    moves = [(abs(mv.delta_of(m)), m) for m in ("calls", "views") if mv.delta_of(m) is not None
             and abs(mv.delta_of(m)) >= SIGNIFICANT_DELTA]
    return delta_fact(mv, max(moves)[1]) if moves else None


def b_curious(c: Ctx) -> Built:
    ev = Evidence()
    ev.add(offer_fact(c.mv))
    ev.add(Fact("headline", f"which {c.pack.customer_noun} requests are trending this week", (), "trigger.kind"))
    return _plan(c, "curious", "curious_ask", "ask", "weekly curiosity check-in", ev, cta_type="open_ended")


def b_planning(c: Ctx) -> Built:
    topic = c.payload.get("intent_topic")
    if not isinstance(topic, str):
        return b_generic(c, "planning trigger without a topic")
    ask = c.payload.get("merchant_last_message") or (c.mv.open_intent() or {}).get("body") or ""
    lines: list[str] = []
    grounding: list[Any] = []
    for turn in c.mv.history:
        body = turn.get("body") if turn.get("from") == "vera" else None
        if isinstance(body, str) and re.search(r"\d", body):
            proposal = re.search(r"(?:Suggest|suggest)\s+(.+?)(?:\.|$)", body)
            if proposal:
                lines.extend(p.strip() for p in re.split(r",\s+", proposal.group(1)) if p.strip())
                grounding.append(body)
            else:
                stat = re.search(r"(\d[\d,]*\s+[a-z/]+(?:\s+avg)?)", body)
                if stat:
                    lines.append(f"Current pull: {stat.group(1)}")
                    grounding.append(body)
    if not any(re.search(r"₹", line) for line in lines):
        for offer in c.mv.active_offers[:1]:
            lines.insert(0, f"Base: your {offer['title']}")
            grounding.append(offer["title"])
    lines.append("Bookings confirmed a day ahead so your team can plan")
    ev = Evidence()
    ev.add(Fact("topic", humanize(topic).lower(), (), "trigger.payload.intent_topic"))
    ev.add(Fact("ask", first_sentence(ask) if ask else humanize(topic), (ask,), "merchant.last_message"))
    ev.add(Fact("outline", " | ".join(lines), tuple(grounding), "merchant.offers/history"))
    return _plan(c, "planning", "deliver_plan", "draft", f"merchant asked: {humanize(topic)}", ev)


CUSTOMER_MODES = {"recall_due": "recall", "appointment_tomorrow": "appointment", "chronic_refill_due": "refill",
                  "customer_lapsed_soft": "winback", "customer_lapsed_hard": "winback", "trial_followup": "trial",
                  "wedding_package_followup": "occasion"}


def b_customer(c: Ctx) -> Built:
    mode = CUSTOMER_MODES.get(c.kind, "recall")
    if mode == "refill" and c.pack.slug != "pharmacies":
        mode = "recall"  # no medicine language outside pharmacies (category leakage guard)
    p = c.payload
    ev = Evidence()
    slots = p.get("available_slots") or p.get("next_session_options") or []
    for i, slot in enumerate(s for s in slots if isinstance(s, dict) and isinstance(s.get("label"), str)):
        ev.add(Fact(f"slot_{i}", slot["label"], (slot["label"], slot.get("iso", "")), "trigger.payload.slots"))
    if isinstance(p.get("service_due"), str):
        ev.add(Fact("service", humanize_service(p["service_due"]), (p["service_due"],), "trigger.payload.service_due"))
    if fmt_date(p.get("due_date")):
        ev.add(Fact("due", fmt_date(p["due_date"]), (p["due_date"],), "trigger.payload.due_date"))

    if c.customer is None:
        if not any(k in ev.facts for k in ("service", "due")) and not isinstance(p.get("molecule_list"), list) \
                and not any(k.startswith("slot_") for k in ev.facts):
            return Skip("customer-scope trigger with neither customer context nor details to approve")
        what = ev.text("service") or (", ".join(p["molecule_list"]) + " refill" if isinstance(p.get("molecule_list"), list)
                                      else humanize(c.kind).lower())
        ev.add(Fact("what", what + (f", due {ev.text('due')}" if ev.get("due") else ""), (), "trigger.payload"))
        slot_labels = [f.text for k, f in sorted(ev.facts.items()) if k.startswith("slot_")]
        if slot_labels:
            ev.add(Fact("detail", "Open slots: " + " and ".join(slot_labels), (), "trigger.payload.slots"))
        return _plan(c, "approval", "customer_outreach_approval", mode,
                     "customer-scope trigger but no customer context: ask the merchant to approve outreach", ev)

    cv = customer_view(c.customer)
    gate = customer_gate(cv, c.kind, c.mv.merchant_id, c.engine.opted_out_customers)
    if not gate.ok:
        return Skip(f"customer gate: {gate.reason}")

    last = fmt_date(p.get("last_service_date") or cv.last_visit)
    if last and mode == "recall":
        ev.add(Fact("last", last, (p.get("last_service_date") or cv.last_visit,), "customer.relationship.last_visit"))
    if mode == "refill":
        if isinstance(p.get("molecule_list"), list):
            ev.add(Fact("molecules", ", ".join(str(m) for m in p["molecule_list"]).capitalize(),
                        tuple(p["molecule_list"]), "trigger.payload.molecule_list"))
        if fmt_date(p.get("stock_runs_out_iso")):
            ev.add(Fact("runout", fmt_date(p["stock_runs_out_iso"]), (p["stock_runs_out_iso"],), "trigger.payload"))
    if mode == "winback":
        if is_num(p.get("days_since_last_visit")):
            ev.add(Fact("gap", f"{fmt_num(p['days_since_last_visit'])} days", (p["days_since_last_visit"],), "trigger.payload"))
        focus = p.get("previous_focus") or cv.preferences.get("training_focus")
        if isinstance(focus, str):
            ev.add(Fact("focus", humanize(focus).lower(), (), "customer.previous_focus"))
    if mode == "trial" and fmt_date(p.get("trial_date")):
        ev.add(Fact("trial", fmt_date(p["trial_date"]), (p["trial_date"],), "trigger.payload.trial_date"))
    if mode == "occasion":
        if is_num(p.get("days_to_wedding")):
            ev.add(Fact("days", f"{fmt_num(p['days_to_wedding'])} days", (p["days_to_wedding"],), "trigger.payload"))
        ev.add(Fact("date", fmt_date(p.get("wedding_date") or cv.preferences.get("wedding_date")) or "your date",
                    (p.get("wedding_date", ""),), "trigger.payload.wedding_date"))
        if isinstance(p.get("next_step_window_open"), str):
            ev.add(Fact("next", humanize_service(p["next_step_window_open"]).lower(), (p["next_step_window_open"],), "trigger.payload"))
        if not ev.get("days"):
            mode = "recall"
    ev.add(_customer_offer(c, cv, mode, ev))
    if mode == "refill" and p.get("delivery_address_saved"):
        delivery = next((o for o in c.mv.active_offers if "deliver" in o["title"].lower()), None)
        if delivery:
            ev.add(Fact("delivery", delivery["title"], (delivery["title"],), "merchant.offers"))
    cta_type = "binary_confirm_cancel" if mode == "refill" else "binary_yes_no"
    return _plan(c, "customer", f"customer_{mode if mode != 'occasion' else 'occasion'}", mode,
                 f"{humanize(c.kind).lower()} for {cv.name or 'customer'} ({gate.reason})", ev,
                 cta_type=cta_type, send_as="merchant_on_behalf", cv=cv)


_STOP = {"the", "and", "with", "for", "your", "free", "off", "get", "of", "a", "@", "on"}


def _customer_offer(c: Ctx, cv, mode: str, ev: Evidence) -> Optional[Fact]:
    """Attach an offer only when it plausibly fits this customer's message."""
    offers = c.mv.active_offers
    if cv.senior:
        senior = [o for o in offers if "senior" in o["title"].lower()]
        if senior:
            return Fact("offer", senior[0]["title"], (senior[0]["title"],), "merchant.offers")
    if mode in ("winback", "appointment") or not offers:
        return offer_fact(c.mv) if mode == "winback" else None
    context = " ".join(f.text for f in ev.facts.values()) + " " + " ".join(cv.services)
    words = {w for w in re.findall(r"[a-z]+", context.lower()) if w not in _STOP and len(w) > 2}
    for offer in offers:
        if words & {w for w in re.findall(r"[a-z]+", offer["title"].lower()) if w not in _STOP}:
            return Fact("offer", offer["title"], (offer["title"],), "merchant.offers")
    return None


def b_generic(c: Ctx, why: str = "") -> Built:
    mv = c.mv
    headline = _strongest_move(mv) or best_peer_fact(mv, c.category, "below") or best_peer_fact(mv, c.category, "above") \
        or metric_fact(mv, "calls")
    if headline is None:
        return Skip(f"nothing grounded to say ({why or 'no data'})")
    ev = Evidence()
    ev.add(Fact("headline", headline.text, headline.values, headline.source))
    text = headline.text.lower()
    action = "fix_dip" if " down " in text else "benchmark_gap" if "average" in text and _below(headline) \
        else "ride_spike"
    plan = _plan(c, "generic", action, "snapshot",
                 f"{humanize(c.kind).lower()}: {why or 'using verified merchant numbers'}", ev)
    plan.priority_notes.append("generic fallback")
    return plan


def _below(fact: Fact) -> bool:
    return len(fact.values) >= 2 and is_num(fact.values[0]) and is_num(fact.values[1]) and fact.values[0] < fact.values[1]


BUILDERS: dict[str, Callable[[Ctx], Built]] = {
    "knowledge": b_knowledge, "compliance": b_compliance, "perf": b_perf, "milestone": b_milestone,
    "reviews": b_reviews, "competitor": b_competitor, "seasonal": b_seasonal, "event": b_event,
    "lifecycle": b_lifecycle, "curious": b_curious, "planning": b_planning, "customer": b_customer,
}


# --------------------------------------------------------------- priority
def score(plan: Plan, c: Ctx) -> None:
    urgency = c.trigger.get("urgency") if is_num(c.trigger.get("urgency")) else 2
    total, notes = float(urgency) * 10, [f"urgency {urgency}"]

    def bump(points: float, note: str) -> None:
        nonlocal total
        total += points
        notes.append(f"{'+' if points >= 0 else ''}{points:g} {note}")

    p = c.payload
    if c.kind == "ipl_match_today" or p.get("stock_runs_out_iso") or c.kind == "appointment_tomorrow":
        bump(4, "same-day/imminent")
    elif p.get("deadline_iso"):
        bump(2, "hard deadline")
    if plan.family == "compliance":
        bump(3, "compliance/patient-safety impact")
    delta = plan.evidence.get("delta")
    if delta and delta.values and is_num(delta.values[0]) and abs(delta.values[0]) >= 0.30:
        bump(4, "large verified move")
    if c.kind == "renewal_due" and (_num(p.get("days_remaining")) or 99) <= 14:
        bump(2, "revenue at risk")
    if plan.send_as == "merchant_on_behalf":
        bump(2, "consented customer service message")
    if plan.family == "planning" or (c.mv.open_intent() and plan.family in ("planning", "knowledge")):
        bump(6, "merchant explicitly asked")
    if plan.mode in ("gap_honest", "gap", "strength") and plan.family == "perf":
        bump(-8, "trigger claim not verified; pivoted")
    if plan.family == "generic":
        bump(-6, "generic fallback")
    if plan.family == "approval":
        bump(-4, "needs merchant approval first")
    relevance = p.get("category_relevance")
    if isinstance(relevance, list) and relevance and c.mv.category not in relevance:
        bump(-10, "festival not relevant to category")
    expires = parse_iso(c.trigger.get("expires_at"))
    if expires and c.now and aware(expires) < aware(c.now):
        bump(-2, "past expires_at (judge still lists it)")
    plan.priority = total
    plan.priority_notes = notes + plan.priority_notes


def rank_key(plan: Plan) -> tuple:
    return (-plan.priority, str(plan.trigger.get("expires_at") or "9999"), plan.trigger_id)


# ------------------------------------------------------------------ engine
class VeraEngine:
    def __init__(self) -> None:
        self._lock = threading.RLock()
        self.reset()

    def reset(self) -> None:
        with getattr(self, "_lock", threading.RLock()):
            self.sent_keys: set[tuple[str, str]] = set()
            self.conversations: dict[str, dict] = {}
            self.bodies_by_merchant: dict[str, list[str]] = {}
            self.opted_out_merchants: set[str] = set()
            self.opted_out_customers: set[str] = set()
            self.snoozed_until: dict[str, datetime] = {}
            self.last_decisions: list[dict] = []
            # Reply-side state (reply.py): per-merchant counters and reply de-duplication.
            self.merchant_state: dict[str, dict] = {}
            self.last_conversation_by_merchant: dict[str, str] = {}
            self.reply_cache: dict[tuple, dict] = {}

    # ------------------------------------------------------------- tick
    def tick(self, request: dict, store: ContextStore) -> dict:
        now = parse_iso(request.get("now"))
        with self._lock:
            self.last_decisions = []
            plans: list[Plan] = []
            for trigger_id in dict.fromkeys(request.get("available_triggers") or []):
                built = self.plan(trigger_id, store, now)
                if isinstance(built, Skip):
                    self.last_decisions.append({"trigger_id": trigger_id, "outcome": "skip", "reason": built.reason})
                else:
                    plans.append(built)

            by_merchant: dict[str, list[Plan]] = {}
            for plan in plans:
                by_merchant.setdefault(plan.mv.merchant_id, []).append(plan)
            order = sorted(by_merchant, key=lambda m: (min(rank_key(p) for p in by_merchant[m]), m))

            actions = []
            for merchant_id in order:
                if len(actions) >= MAX_ACTIONS_PER_TICK:
                    break
                chosen = None
                for plan in sorted(by_merchant[merchant_id], key=rank_key):
                    if chosen is None:
                        composed = compose(plan, self._previous_bodies(plan))
                        if composed is not None:
                            chosen = plan
                            actions.append(self._commit(plan, composed))
                            self.last_decisions.append({"trigger_id": plan.trigger_id, "outcome": "sent",
                                                        "priority": plan.priority})
                            continue
                        reason = "validation failed"
                    else:
                        reason = f"deferred: {chosen.trigger_id} ranked higher for this merchant"
                    self.last_decisions.append({"trigger_id": plan.trigger_id, "outcome": "defer",
                                                "reason": reason, "priority": plan.priority})
            return {"actions": actions}

    def plan(self, trigger_id: str, store: ContextStore, now: Optional[datetime]) -> Built:
        trigger = store.trigger(trigger_id)
        if trigger is None:
            return Skip("unknown trigger")
        merchant = store.merchant(trigger.get("merchant_id"))
        if merchant is None:
            return Skip("merchant context not loaded")
        mv = merchant_view(merchant)
        gate = merchant_gate(mv.merchant_id, now, self.opted_out_merchants, self.snoozed_until)
        if not gate.ok:
            return Skip(gate.reason)
        kind = str(trigger.get("kind") or "")
        category = store.category(mv.category or as_dict(trigger.get("payload")).get("category"))
        customer = store.customer(trigger.get("customer_id")) if trigger.get("customer_id") else None
        ctx = Ctx(trigger_id, trigger, kind, as_dict(trigger.get("payload")), mv, category, pack_for(mv.category),
                  customer, now, self)
        family = FAMILY_OF_KIND.get(kind) or ("customer" if trigger.get("scope") == "customer" else None)
        built = BUILDERS[family](ctx) if family else b_generic(ctx, "unrecognised trigger kind")
        if isinstance(built, Skip):
            return built
        built.suppression_key = str(trigger.get("suppression_key") or f"{kind}:{mv.merchant_id}:{trigger_id}")
        if (self._recipient(built), built.suppression_key) in self.sent_keys:
            return Skip("suppression key already sent to this recipient")
        score(built, ctx)
        return built

    # ------------------------------------------------------------ reply
    def reply(self, request: dict, store: ContextStore) -> dict:
        from reply import handle_reply  # local import: reply.py depends on this module
        with self._lock:
            return handle_reply(self, request, store)

    # ---------------------------------------------------------- helpers
    @staticmethod
    def _recipient(plan: Plan) -> str:
        return plan.cv.customer_id if plan.send_as == "merchant_on_behalf" and plan.cv else plan.mv.merchant_id

    def _previous_bodies(self, plan: Plan) -> list[str]:
        return self.bodies_by_merchant.get(plan.mv.merchant_id, []) + plan.mv.previous_bodies()

    def _commit(self, plan: Plan, composed: Composed) -> dict:
        recipient = self._recipient(plan)
        digest = hashlib.sha256(f"{recipient}|{plan.suppression_key}".encode()).hexdigest()[:8]
        conversation_id = f"conv_{plan.mv.merchant_id}_{plan.kind}_{digest}"
        self.sent_keys.add((recipient, plan.suppression_key))
        self.bodies_by_merchant.setdefault(plan.mv.merchant_id, []).append(composed.body)
        customer_id = plan.cv.customer_id if plan.send_as == "merchant_on_behalf" and plan.cv else None
        self.conversations[conversation_id] = {
            "merchant_id": plan.mv.merchant_id, "customer_id": customer_id, "trigger_id": plan.trigger_id,
            "kind": plan.kind, "family": plan.family, "action": plan.action, "send_as": plan.send_as,
            "bodies": [composed.body], "status": "open", "turns": [{"from": "vera", "body": composed.body}],
            "proposal": {
                "action": plan.action, "family": plan.family, "kind": plan.kind, "mode": plan.mode,
                "deliverable": plan.pack.deliverable(plan.action), "reason": plan.reason,
                "facts": {k: f.text for k, f in plan.evidence.facts.items()},
                "grounding": _grounding(plan), "cta": plan.cta_type,
                "slots": [f.text for k, f in sorted(plan.evidence.facts.items()) if k.startswith("slot_")],
            },
        }
        self.last_conversation_by_merchant[plan.mv.merchant_id] = conversation_id
        return {
            "conversation_id": conversation_id,
            "merchant_id": plan.mv.merchant_id,
            "customer_id": customer_id,
            "send_as": plan.send_as,
            "trigger_id": plan.trigger_id,
            "template_name": composed.template_name,
            "template_params": composed.template_params,
            "body": composed.body,
            "cta": composed.cta,
            "suppression_key": plan.suppression_key,
            "rationale": composed.rationale,
        }
