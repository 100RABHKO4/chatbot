"""Category strategy packs, keyed by category slug."""

from strategies import dentists, gyms, pharmacies, restaurants, salons
from strategies.base import COMMON_ACTIONS, CategoryPack

PACKS: dict[str, CategoryPack] = {p.slug: p for p in (
    dentists.PACK, salons.PACK, restaurants.PACK, gyms.PACK, pharmacies.PACK)}

# Unknown categories get a neutral, compliance-safe pack.
DEFAULT_PACK = CategoryPack(
    slug="default",
    customer_noun="customers", customer_noun_one="customer",
    visit_noun="visit", business_noun="business",
    merchant_tone="peer, practical", customer_tone="warm, respectful",
    deliverables=dict(salons.PACK.deliverables),
    ctas={"default": "Reply YES and I'll {deliverable}.",
          "curious_ask": "Just reply with the name — I'll draft the post.",
          "renewal": "Reply YES to renew — takes a minute."},
    customer_due_line="your next visit is due",
    customer_winback_line="we'd love to see you again",
    extra_taboos=("guaranteed", "cure"),
    default_growth_move="a fresh post on your Google listing",
    allowed_actions=COMMON_ACTIONS,
)


def pack_for(slug: str | None) -> CategoryPack:
    return PACKS.get(slug or "", DEFAULT_PACK)
