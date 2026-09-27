"""Message composer: turns a decided Plan into WhatsApp copy.

Shape of every proactive body (RESEARCH.md §11.2):
    greeting + why-now hook + one piece of evidence/judgment + single CTA (last)

The composer never looks anything up on its own. It renders only the facts
the decision layer put into ``plan.evidence``, then runs the scoped validator.
If the rich draft fails validation it falls back to a minimal template built
from the same evidence; if that also fails, nothing is sent.
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, field
from typing import Any, Callable, Optional

from signals import CustomerView, Evidence, MerchantView, first_sentence, fmt_date, humanize
from strategies.base import CategoryPack
from validator import ValidationResult, Validator

HINGLISH_CATEGORIES = {"dentists", "salons", "restaurants", "pharmacies"}  # code_mix: hindi_english_natural


@dataclass
class Plan:
    trigger_id: str
    kind: str
    family: str
    action: str                    # action type from the pack vocabulary
    mode: str                      # template variant within the family
    send_as: str                   # "vera" | "merchant_on_behalf"
    cta_type: str
    reason: str                    # one-line why-now, used in the rationale
    evidence: Evidence
    pack: CategoryPack
    mv: MerchantView
    trigger: dict
    category: Optional[dict]
    cv: Optional[CustomerView] = None
    priority: float = 0.0
    priority_notes: list[str] = field(default_factory=list)
    suppression_key: str = ""
    extra_grounding: list[Any] = field(default_factory=list)


@dataclass
class Composed:
    body: str
    cta: str
    template_name: str
    template_params: list[str]
    rationale: str
    validation: ValidationResult
    fallback_used: bool


# ------------------------------------------------------------------ helpers
def _sentence(text: str) -> str:
    text = " ".join(text.split()).strip()
    if not text:
        return ""
    text = text[0].upper() + text[1:]
    return text if text[-1] in ".!?" else text + "."


def merchant_greeting(plan: Plan) -> str:
    who = plan.pack.salute(plan.mv.owner) if plan.mv.owner else ""
    if who:
        return f"{who}," if plan.pack.doctor_title else f"Hi {who},"
    return f"Hi {plan.mv.name} team," if plan.mv.name else "Hi,"


def merchant_cta(plan: Plan) -> str:
    deliverable = plan.pack.deliverable(plan.action)
    template = plan.pack.cta(plan.action)
    text = template.format(deliverable=deliverable)
    if template.startswith("Reply YES and I'll") and plan.mv.speaks_hindi and plan.pack.slug in HINGLISH_CATEGORIES:
        text = f"Bas YES bhejiye — I'll {deliverable}."
    return text


def _variant(seed: str, options: list[str]) -> str:
    """Deterministic choice so different merchants don't get identical copy."""
    return options[int(hashlib.sha256(seed.encode()).hexdigest(), 16) % len(options)]


def _join(*parts: str) -> str:
    return " ".join(p for p in parts if p)


# ------------------------------------------------ merchant-facing families
def _knowledge(plan: Plan) -> tuple[str, str]:
    ev = plan.evidence
    if plan.mode == "cde_opportunity":
        hook = _sentence(f"CDE opportunity — {ev.text('title')}, {ev.text('event_detail')}")
        return hook, _sentence(ev.text("finding"))
    hook = _sentence(f"{ev.text('source')} has a new item worth a look: {ev.text('title')}")
    detail = _sentence(ev.text("finding"))
    if ev.get("cohort"):
        detail = _join(detail, _sentence(f"Relevant for {ev.text('cohort')}"))
    elif ev.get("event_detail"):
        detail = _join(detail, _sentence(ev.text("event_detail")))
    return hook, detail


def _compliance(plan: Plan) -> tuple[str, str]:
    ev = plan.evidence
    if plan.mode == "supply":
        hook = _sentence(f"Urgent: voluntary recall on {ev.text('molecule')} batches {ev.text('batches')}"
                         + (f" from {ev.text('manufacturer')}" if ev.get("manufacturer") else "")
                         + (f" ({ev.text('source')})" if ev.get("source") else ""))
        detail = _join(_sentence(ev.text("risk")),
                       _sentence(f"You have {ev.text('rx_base')} on file" if ev.get("rx_base") else ""))
        return hook, detail
    hook = _sentence(f"Compliance heads-up — {ev.text('title')} ({ev.text('source')})")
    detail = _join(_sentence(ev.text("finding")), _sentence(ev.text("action_line")))
    return hook, detail


def _perf(plan: Plan) -> tuple[str, str]:
    ev = plan.evidence
    if plan.mode == "seasonal":
        hook = _sentence(f"{ev.text('delta')} — before you worry, this is the usual "
                         f"{ev.text('season')} pattern for {plan.pack.slug}")
        detail = _join(_sentence(f"Seasonal note: {ev.text('season_note')}") if ev.get("season_note") else "",
                       _sentence(f"I'd put the energy into your {ev.text('base')} rather than ad spend")
                       if ev.get("base") else "")
        return hook, detail
    if plan.mode == "dip":
        hook = _sentence(f"Flagging a drop: {ev.text('delta')}" + (f" ({ev.text('baseline')})" if ev.get("baseline") else ""))
        detail = _sentence(ev.text("context")) if ev.get("context") else ""
        return hook, detail
    if plan.mode == "spike":
        hook = _sentence(f"Good news: {ev.text('delta')}" + (f", likely from {ev.text('driver')}" if ev.get("driver") else ""))
        detail = _sentence(ev.text("context")) if ev.get("context") else ""
        return hook, detail
    if plan.mode == "gap_honest":
        verdict = "steady rather than a spike" if plan.kind == "perf_spike" else "nothing alarming"
        hook = _sentence(f"Checked your numbers: {ev.text('actual')}, so {verdict} — but {ev.text('gap')}")
        return hook, ""
    if plan.mode == "strength":
        hook = _sentence(f"Quick win to build on: {ev.text('gap')}")
        return hook, _sentence(ev.text("context")) if ev.get("context") else ""
    hook = _sentence(f"One number stood out this week: {ev.text('gap')}")
    return hook, _sentence(ev.text("context")) if ev.get("context") else ""


def _milestone(plan: Plan) -> tuple[str, str]:
    ev = plan.evidence
    hook = _sentence(f"You're at {ev.text('now')} — just {ev.text('remaining')} short of {ev.text('target')}")
    return hook, _sentence("A small push from happy regulars usually closes a gap like this within days")


def _reviews(plan: Plan) -> tuple[str, str]:
    ev = plan.evidence
    hook = _sentence(f"{ev.text('count')} mentioned {ev.text('theme')}"
                     + (f" — e.g. “{ev.text('quote')}”" if ev.get("quote") else ""))
    detail = _sentence("Left unanswered, a repeated theme like this starts showing up in the review summary"
                       if plan.mode == "negative" else "That's worth featuring on your listing")
    return hook, detail


def _competitor(plan: Plan) -> tuple[str, str]:
    ev = plan.evidence
    hook = _sentence(f"New nearby: {ev.text('competitor')}" + (f", {ev.text('distance')} away" if ev.get("distance") else "")
                     + (f", opened {ev.text('opened')}" if ev.get("opened") else "")
                     + (f", leading with {ev.text('their_offer')}" if ev.get("their_offer") else ""))
    detail = _sentence(f"Your {ev.text('offer')} is already live — I'd compete on trust and quality, not a price cut"
                       if ev.get("offer") else "I'd compete on trust and quality, not a price cut")
    return hook, detail


def _seasonal(plan: Plan) -> tuple[str, str]:
    ev = plan.evidence
    if plan.mode == "festival":
        hook = _sentence(f"{ev.text('festival')} is on {ev.text('date')}"
                         + (f" — {ev.text('days')} to plan" if ev.get("days") else ""))
        detail = _sentence(f"Your {ev.text('offer')} is a ready hook for early bookings" if ev.get("offer")
                           else "Early planning beats last-week discounting")
        return hook, detail
    if plan.mode == "trends":
        hook = _sentence(f"Seasonal shift in your category: {ev.text('trends')}")
        return hook, _sentence(ev.text("action_line")) if ev.get("action_line") else ""
    hook = _sentence(f"Seasonal cue for your category: {ev.text('season_note')}")
    detail = _sentence(f"Your {ev.text('offer')} fits this window" if ev.get("offer") else plan.pack.default_growth_move
                       and f"A good moment for {plan.pack.default_growth_move}")
    return hook, detail


def _event(plan: Plan) -> tuple[str, str]:
    ev = plan.evidence
    hook = _sentence(f"{ev.text('match')} at {ev.text('venue')} today, {ev.text('time')}")
    if plan.mode == "weekend":
        detail = _join(_sentence(f"{ev.text('insight')} ({ev.text('insight_source')})") if ev.get("insight") else "",
                       _sentence(f"So I'd skip a dine-in match promo tonight, lean on delivery, and save your "
                                 f"{ev.text('offer')} for the next weeknight match"
                                 if ev.get("offer") else "So I'd skip a dine-in match promo tonight and lean on delivery"))
    else:
        detail = _sentence(f"Match nights are a good slot for your {ev.text('offer')}" if ev.get("offer")
                           else "Weeknight matches are a good slot for a match-night combo")
    return hook, detail


def _lifecycle(plan: Plan) -> tuple[str, str]:
    ev = plan.evidence
    if plan.mode == "renewal":
        hook = _sentence(f"Your {ev.text('plan')} plan ends in {ev.text('days')}"
                         + (f" (renewal {ev.text('amount')})" if ev.get("amount") else ""))
        detail = _sentence(f"This month you had {ev.text('stat')} — renewing keeps that listing work running"
                           if ev.get("stat") else "Renewing keeps your listing work running without a gap")
        return hook, detail
    if plan.mode == "winback":
        hook = _sentence(f"It's been {ev.text('since')} since your plan lapsed"
                         + (f", and {ev.text('drop')}" if ev.get("drop") else ""))
        detail = _sentence(f"{ev.text('lapsed')} since then" if ev.get("lapsed") else "")
        return hook, detail
    if plan.mode == "verify":
        hook = _sentence("Your Google profile is still unverified"
                         + (f" — verified listings see an estimated {ev.text('uplift')} more engagement" if ev.get("uplift") else ""))
        detail = _sentence(f"Verification is by {ev.text('path')}" if ev.get("path") else "")
        return hook, detail
    if plan.mode == "checkin":
        return _sentence(f"Checking in with one number from your listing: {ev.text('fresh')}"), ""
    hook = _sentence(f"It's been {ev.text('since')} since we last spoke"
                     + (f" (last topic: {ev.text('topic')})" if ev.get("topic") else ""))
    detail = _sentence(f"One update since then: {ev.text('fresh')}" if ev.get("fresh") else "")
    return hook, detail


def _curious(plan: Plan) -> tuple[str, str]:
    ev = plan.evidence
    noun = {"dentists": "treatment", "salons": "service", "restaurants": "dish", "gyms": "class",
            "pharmacies": "product"}.get(plan.pack.slug, "service")
    guess = f" Is it still your {ev.text('offer')}?" if ev.get("offer") else ""
    hook = _sentence(f"Quick one — which {noun} are {plan.pack.customer_noun} asking about most this week")[:-1] + "?" + guess
    detail = _sentence(f"I'll turn your answer into a Google post and a ready WhatsApp reply for {plan.pack.customer_noun}")
    return hook, detail


def _planning(plan: Plan) -> tuple[str, str]:
    ev = plan.evidence
    hook = _sentence(f"Picking up your “{ev.text('ask')}” — here's a starter {ev.text('topic')} draft")
    lines = [f"- {line}" for line in ev.text("outline").split(" | ") if line]
    detail = "\n" + "\n".join(lines) + "\n" if lines else ""
    return hook, detail


def _approval(plan: Plan) -> tuple[str, str]:
    ev = plan.evidence
    hook = _sentence(f"A {plan.pack.customer_noun_one} reminder is ready to go: {ev.text('what')}")
    return hook, _sentence(ev.text("detail")) if ev.get("detail") else ""


def _generic(plan: Plan) -> tuple[str, str]:
    ev = plan.evidence
    return _sentence(f"one number worth acting on: {ev.text('headline')}"), ""


MERCHANT_FAMILIES: dict[str, Callable[[Plan], tuple[str, str]]] = {
    "knowledge": _knowledge, "compliance": _compliance, "perf": _perf, "milestone": _milestone,
    "reviews": _reviews, "competitor": _competitor, "seasonal": _seasonal, "event": _event,
    "lifecycle": _lifecycle, "curious": _curious, "planning": _planning, "approval": _approval,
    "generic": _generic,
}


# ------------------------------------------------ customer-facing family
def _customer(plan: Plan) -> tuple[str, str, str, str]:
    """Returns (greeting, hook, detail, cta) in the customer's language."""
    cv, ev, pack, mv = plan.cv, plan.evidence, plan.pack, plan.mv
    assert cv is not None
    hinglish = cv.language in ("hi", "hi-en")
    emoji = f" {pack.customer_emoji}" if pack.customer_emoji and not cv.senior else ""
    if cv.via == "son":
        greet = f"Namaste — {mv.name} here, regarding {cv.name} ji."
    elif cv.via == "parent" and cv.addressee:
        greet = f"Hi {cv.addressee}, {mv.name} here{emoji} — about {cv.name}."
    elif cv.senior:
        greet = f"Namaste {cv.name} ji, {mv.name} here."
    else:
        greet = f"Hi {cv.addressee or cv.name}, {mv.name} here{emoji}."

    mode = plan.mode
    if mode == "recall":
        hook = _sentence((f"It's been a while since your visit on {ev.text('last')}" if ev.get("last") else "")
                         + (" — " if ev.get("last") else "")
                         + pack.customer_due_line.format(service=ev.text("service", pack.default_service))
                         + (f" (due {ev.text('due')})" if ev.get("due") else ""))
    elif mode == "appointment":
        hook = _sentence(f"Friendly reminder: your {pack.visit_noun} with us is tomorrow"
                         + (f", {ev.text('when')}" if ev.get("when") else ""))
    elif mode == "refill":
        hook = _sentence((f"{ev.text('molecules')} — " if ev.get("molecules") else "")
                         + ("stock runs out on " + ev.text("runout") if ev.get("runout") else pack.customer_due_line))
        if hinglish and ev.get("runout"):
            hook = _sentence((f"{ev.text('molecules')} — " if ev.get("molecules") else "")
                             + f"stock {ev.text('runout')} ko khatam hoga")
    elif mode == "winback":
        hook = _sentence((f"It's been {ev.text('gap')}" if ev.get("gap") else "It's been a while")
                         + f" — {pack.customer_winback_line}")
        if ev.get("focus"):
            hook = _join(hook, _sentence(f"Still working on {ev.text('focus')}? We can pick it right back up"))
    elif mode == "trial":
        hook = _sentence(f"Thanks for coming in for the trial on {ev.text('trial')}" if ev.get("trial")
                         else "Thanks for trying a session with us")
    elif mode == "occasion":
        hook = _sentence(f"{ev.text('days')} to go until the big day on {ev.text('date')}"
                         + (f" — this is the right window for your {ev.text('next')}" if ev.get("next") else ""))
    else:
        hook = _sentence(pack.customer_due_line.format(service=ev.text("service", pack.default_service)))

    detail = _sentence(f"Our \u201c{ev.text('offer')}\u201d offer is open for you" if ev.get("offer") else "")
    if hinglish and ev.get("offer"):
        detail = _sentence(f"Aapke liye \u201c{ev.text('offer')}\u201d offer ready hai")
    if ev.get("delivery"):
        detail = _join(detail, _sentence(f"Delivery to your saved address ({ev.text('delivery')})"))

    slots = [f.text for k, f in sorted(ev.facts.items()) if k.startswith("slot_")]
    if len(slots) >= 2:
        cta = f"Reply 1 for {slots[0]}, 2 for {slots[1]}, or tell us a time that works."
        plan.cta_type = "multi_choice_slot"
        plan.extra_grounding += [1, 2]
    elif len(slots) == 1:
        cta = f"Reply YES to book {slots[0]}, or tell us a time that works."
    elif mode == "refill":
        cta = "Dispatch ke liye CONFIRM reply karein." if hinglish else pack.customer_cta_confirm + "."
    elif mode == "winback":
        cta = pack.customer_winback_cta
    elif mode == "appointment":
        cta = "Reply YES to confirm, or tell us if you need to reschedule."
    else:
        cta = pack.customer_cta_confirm + "."
    if hinglish and cta.startswith("Reply YES"):
        cta = cta.replace("Reply YES", "Bas YES reply karein", 1)
    return greet, hook, detail, cta


# ------------------------------------------------------------ public API
def compose(plan: Plan, previous_bodies: list[str]) -> Optional[Composed]:
    for fallback in (False, True):
        parts = _render(plan, fallback)
        if parts is None:
            continue
        body = _join(*parts).replace(" \n", "\n").replace("\n ", "\n")
        validator = Validator.scoped(_grounding(plan), plan.category, plan.pack.extra_taboos)
        result = validator.validate(body, intent_mode=True, previous_bodies=previous_bodies)
        if result.ok:
            return Composed(
                body=body, cta=plan.cta_type,
                template_name=f"{'merchant' if plan.send_as == 'merchant_on_behalf' else 'vera'}_{plan.family}_v1",
                template_params=[p for p in parts if p],
                rationale=_rationale(plan, fallback),
                validation=result, fallback_used=fallback)
        plan.priority_notes.append(f"validation{'(fallback)' if fallback else ''} failed: {result.codes()}")
    return None


def _render(plan: Plan, fallback: bool) -> Optional[list[str]]:
    if plan.send_as == "merchant_on_behalf":
        greet, hook, detail, cta = _customer(plan)
        return [greet, hook, "" if fallback else detail, cta]
    if fallback:
        headline = plan.evidence.get("headline") or next(iter(plan.evidence.facts.values()), None)
        if headline is None:
            return None
        return [merchant_greeting(plan), _sentence(f"Quick update: {headline.text}"), merchant_cta(plan)]
    hook, detail = MERCHANT_FAMILIES.get(plan.family, _generic)(plan)
    cta = merchant_cta(plan)
    greeting = merchant_greeting(plan)
    return [greeting, _after_comma(hook) if greeting.endswith(",") else hook, detail, cta]


# Template-authored openers that read wrong capitalised after "Hi Name,".
_OPENERS = {"compliance", "urgent:", "flagging", "good", "checked", "quick", "one", "you're", "new", "seasonal",
            "your", "it's", "picking", "a", "cde", "checking", "calls", "profile", "click-through", "direction", "leads"}


def _after_comma(text: str) -> str:
    first = text.split(" ", 1)[0].lower()
    word = text.split(" ", 1)[0]
    acronym = len(word) > 1 and word.isupper()
    return text[0].lower() + text[1:] if first in _OPENERS and not acronym else text


def _grounding(plan: Plan) -> list[Any]:
    names = [plan.mv.name, plan.mv.owner, plan.mv.locality, plan.mv.city]
    if plan.cv is not None:
        names += [plan.cv.name, plan.cv.addressee]
    return plan.evidence.grounding() + [n for n in names if n] + plan.extra_grounding


def _rationale(plan: Plan, fallback: bool) -> str:
    facts = "; ".join(f"{k}={f.text}" for k, f in plan.evidence.facts.items())
    notes = ", ".join(plan.priority_notes)
    return (f"{plan.kind} -> {plan.action} ({plan.send_as}, {plan.pack.slug}). Why now: {plan.reason}. "
            f"Evidence used: {facts}. Priority {plan.priority:.0f}"
            + (f" [{notes}]" if notes else "") + f". CTA: {plan.cta_type}."
            + (" Minimal fallback template used after validation." if fallback else ""))


def humanize_service(token: Any) -> str:
    """'6_month_cleaning' -> '6-month cleaning'; 'skin_prep_program_30day' -> 'skin prep program (30-day)'."""
    text = humanize(token)
    text = re.sub(r"\b(\d+) (month|week|day)\b", r"\1-\2", text)
    text = re.sub(r"\b(\d+)(day|week|month)\b", r"(\1-\2)", text)
    return text


__all__ = ["Plan", "Composed", "compose", "merchant_greeting", "humanize_service", "fmt_date"]
