"""Category strategy pack contract.

A pack is pure data plus a few tiny formatting hooks. It decides *how* Vera
talks to a vertical (salutation, nouns, deliverables, CTA verbs, compliance);
it never decides *what* to say. That is decision.py's job.

Voice sources: dataset/categories/<slug>.json (voice, taboos) and the judge's
category expectations (judge_simulator.py:453-458). Merchant-facing copy uses
the operator/peer voice from the dataset; customer-facing copy uses the
customer tone listed per pack.
"""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True)
class CategoryPack:
    slug: str
    # Nouns keep category vocabulary out of family templates (no leakage).
    customer_noun: str           # "patients", "clients", ...
    customer_noun_one: str       # "patient", "client", ...
    visit_noun: str              # "appointment", "booking", ...
    business_noun: str           # "clinic", "salon", ...
    merchant_tone: str           # for rationale/readme only
    customer_tone: str
    # Merchant salutation: "Dr. {name}" for dentists, "{name}" otherwise.
    doctor_title: bool = False
    # action_type -> deliverable Vera offers to do for the merchant.
    deliverables: dict[str, str] = field(default_factory=dict)
    # action_type -> single CTA phrase (merchant-facing, final sentence).
    ctas: dict[str, str] = field(default_factory=dict)
    # Customer-facing: what "due"/"come back" means in this vertical.
    customer_due_line: str = ""
    customer_winback_line: str = ""
    customer_cta_confirm: str = "Reply YES to confirm"
    customer_winback_cta: str = "Reply YES and we'll hold a slot for you — no commitment."
    default_service: str = "visit"          # used when a trigger names no service
    customer_emoji: str = ""
    # Extra compliance terms beyond the dataset's vocab_taboo list.
    extra_taboos: tuple[str, ...] = ()
    # Short, category-correct idea used when a merchant needs a next step and
    # the data offers nothing more specific (never contains numbers).
    default_growth_move: str = ""
    allowed_actions: frozenset[str] = frozenset()

    def salute(self, first_name: str) -> str:
        return f"Dr. {first_name}" if self.doctor_title and first_name else first_name

    def deliverable(self, action: str) -> str:
        return (self.deliverables.get(action) or self.deliverables.get(action.rsplit("_", 1)[0])
                or self.deliverables["default"])

    def cta(self, action: str) -> str:
        return self.ctas.get(action) or self.ctas["default"]


COMMON_ACTIONS = frozenset({
    "share_knowledge", "compliance_alert", "fix_dip", "benchmark_gap", "ride_spike",
    "milestone_push", "review_response", "competitor_response", "seasonal_play",
    "event_play", "renewal", "winback_merchant", "reengage", "verify_profile",
    "curious_ask", "deliver_plan", "customer_outreach_approval", "generic_nudge",
    "customer_recall", "customer_appointment", "customer_refill", "customer_winback",
    "customer_trial_followup", "customer_occasion", "event_register",
})
