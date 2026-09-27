"""Strict output validator: the last gate before any message leaves the bot.

Checks (RESEARCH.md §6, §14, §16, §17):
  * numbers   - every numeral in the body must be grounded in the contexts that
                were used to compose it (or explicitly registered derived facts)
  * codes     - no raw internal tokens (snake_case, ids, unfilled placeholders)
  * urls      - no links (api-call-examples.md F.4: -3 per URL)
  * phrases   - no anti-pattern phrasing; in intent mode, none of the
                qualifying phrases the judge's intent-transition check rejects
  * taboos    - none of the category's vocab_taboo terms
  * basics    - non-empty, sane length, not a verbatim repeat

Everything is deterministic and pure; the validator never mutates its inputs.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Iterable, Optional

# ---------------------------------------------------------------- numbers
# A numeral not glued to a preceding letter/digit ("Studio11", "W17" are
# skipped on both sides). Indian/Western digit grouping ("1,499", "1,00,000")
# is folded into one number; "5, 6pm" stays two numbers.
_NUMBER_RE = re.compile(r"(?<![A-Za-z0-9])\d+(?:,\d{2,3})*(?:\.\d+)?")
_ISO_DATETIME_RE = re.compile(r"(\d{4})-(\d{2})-(\d{2})(?:[T ](\d{2}):(\d{2}))?")
_MAX_DECIMALS = 2


def extract_numbers(text: str) -> list[tuple[str, float]]:
    """Return (token, value) for every numeral in ``text``."""
    return [(m.group(0), float(m.group(0).replace(",", ""))) for m in _NUMBER_RE.finditer(text)]


def _variants(value: float) -> set[float]:
    """Acceptable renderings of a context value (rounding and percent forms)."""
    value = abs(value)
    forms = {round(value, d) for d in range(_MAX_DECIMALS + 1)}
    if 0 < value <= 1 and value != int(value):  # fraction -> percent (0.021 -> 2.1)
        pct = value * 100
        forms |= {round(pct, d) for d in range(_MAX_DECIMALS + 1)}
    return forms


class FactBank:
    """Set of numbers the body is allowed to mention."""

    def __init__(self) -> None:
        self._values: set[float] = set()

    @classmethod
    def from_contexts(cls, *contexts: Any, extra: Iterable[Any] = ()) -> "FactBank":
        bank = cls()
        for ctx in contexts:
            if ctx is not None:
                bank.add_context(ctx)
        for value in extra:
            bank.add(value)
        return bank

    def add(self, value: Any) -> None:
        """Register one fact: a number, or text whose numerals are all facts."""
        if isinstance(value, bool) or value is None:
            return
        if isinstance(value, (int, float)):
            self._values |= _variants(float(value))
        elif isinstance(value, str):
            self._add_text(value)

    def add_context(self, ctx: Any) -> None:
        stack = [ctx]
        while stack:
            node = stack.pop()
            if isinstance(node, dict):
                stack.extend(node.values())
            elif isinstance(node, (list, tuple)):
                stack.extend(node)
            else:
                self.add(node)

    def allows(self, value: float) -> bool:
        return round(abs(value), _MAX_DECIMALS) in self._values or abs(value) in self._values

    def __len__(self) -> int:
        return len(self._values)

    def _add_text(self, text: str) -> None:
        for m in _ISO_DATETIME_RE.finditer(text):
            year, month, day, hour, minute = m.groups()
            for part in (year, month, day):
                self._values |= _variants(float(part))
            if hour is not None:
                h = int(hour)
                self._values |= _variants(float(h)) | _variants(float(h % 12 or 12))
                self._values |= _variants(float(minute))
        # "T" between date and time would hide the hour from _NUMBER_RE.
        for _, number in extract_numbers(re.sub(r"(\d)T(\d)", r"\1 \2", text)):
            self._values |= _variants(number)


# ----------------------------------------------------------------- text rules
_SNAKE_CASE_RE = re.compile(r"\b[A-Za-z0-9]+(?:_[A-Za-z0-9]+)+\b")
_PLACEHOLDER_RE = re.compile(r"\{\{\s*\w*\s*\}\}|\{\s*[A-Za-z_]\w*\s*\}|<\s*(?:phone|redacted|name)\s*>", re.I)
_NULLISH_RE = re.compile(r"\b(?:None|null|undefined|NaN)\b|\[object Object\]")
_JARGON_RE = re.compile(r"\b(?:payload|suppression[ _]key|context[ _]id|trigger[ _]id|merchant[ _]id|"
                        r"json|llm|template[ _]params)\b", re.I)
_URL_RE = re.compile(r"https?://|\bwww\.|\b[\w-]+\.(?:com|in|org|net|io|co|ai|app|ly|me|info|biz)\b(?:/\S*)?",
                     re.I)

# Mirrors judge_simulator.py:740 exactly: plain case-insensitive substring
# match, so "do your" also trips "do you".
INTENT_FORBIDDEN = ("would you", "do you", "can you tell", "what if", "how about")

GENERIC_FORBIDDEN = (
    "would you like to know more",
    "i hope you're doing well",
    "i hope you are doing well",
    "i'm reaching out",
    "i am reaching out",
    "increase your sales",
    "amazing deal",
)

MIN_BODY_CHARS = 20
MAX_BODY_CHARS = 1000


@dataclass(frozen=True)
class Issue:
    code: str
    detail: str


@dataclass
class ValidationResult:
    issues: list[Issue] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.issues

    def codes(self) -> list[str]:
        return [i.code for i in self.issues]


def _norm(text: str) -> str:
    return " ".join(text.lower().split())


class Validator:
    """Validates bodies composed from one (category, merchant, trigger, customer) tuple."""

    def __init__(self, category: Optional[dict] = None, merchant: Optional[dict] = None,
                 trigger: Optional[dict] = None, customer: Optional[dict] = None,
                 extra_facts: Iterable[Any] = ()) -> None:
        self.facts = FactBank.from_contexts(category, merchant, trigger, customer, extra=extra_facts)
        self.taboos = _category_taboos(category)

    @classmethod
    def scoped(cls, grounding: Iterable[Any], category: Optional[dict] = None,
               extra_taboos: Iterable[str] = ()) -> "Validator":
        """Validator whose fact bank holds ONLY the evidence chosen for one decision.

        This is the production path: coincidental numbers elsewhere in the
        contexts (dates, minutes, unrelated stats) can no longer ground a
        fabricated figure.
        """
        validator = cls(category=category)
        validator.facts = FactBank.from_contexts(extra=grounding)
        validator.taboos = sorted(set(validator.taboos) | {_norm(t) for t in extra_taboos if t})
        return validator

    def register(self, *values: Any) -> None:
        """Allow derived values the composer computed (e.g. 12 calls -> 6 calls)."""
        for value in values:
            self.facts.add(value)

    # ------------------------------------------------------------- checks
    def check_numbers(self, body: str) -> list[Issue]:
        return [Issue("ungrounded_number", token) for token, value in extract_numbers(body)
                if not self.facts.allows(value)]

    @staticmethod
    def check_codes(body: str) -> list[Issue]:
        issues = [Issue("raw_code", m.group(0)) for m in _SNAKE_CASE_RE.finditer(body)]
        issues += [Issue("placeholder", m.group(0)) for m in _PLACEHOLDER_RE.finditer(body)]
        issues += [Issue("nullish", m.group(0)) for m in _NULLISH_RE.finditer(body)]
        issues += [Issue("jargon", m.group(0)) for m in _JARGON_RE.finditer(body)]
        return issues

    @staticmethod
    def check_urls(body: str) -> list[Issue]:
        return [Issue("url", m.group(0)) for m in _URL_RE.finditer(body)]

    @staticmethod
    def check_phrases(body: str, intent_mode: bool = False) -> list[Issue]:
        text = _norm(body)
        issues = [Issue("forbidden_phrase", p) for p in GENERIC_FORBIDDEN if p in text]
        if intent_mode:
            issues += [Issue("qualifying_phrase", p) for p in INTENT_FORBIDDEN if p in text]
        return issues

    def check_taboos(self, body: str) -> list[Issue]:
        text = _norm(body)
        return [Issue("taboo", t) for t in self.taboos
                if re.search(r"(?<!\w)" + re.escape(t) + r"(?!\w)", text)]

    @staticmethod
    def check_basics(body: str) -> list[Issue]:
        stripped = body.strip()
        if not stripped:
            return [Issue("empty_body", "")]
        if len(stripped) < MIN_BODY_CHARS:
            return [Issue("too_short", str(len(stripped)))]
        if len(stripped) > MAX_BODY_CHARS:
            return [Issue("too_long", str(len(stripped)))]
        return []

    @staticmethod
    def check_repeat(body: str, previous_bodies: Iterable[str]) -> list[Issue]:
        target = _norm(body)
        return [Issue("repeat", "identical to a previously sent body")] \
            if any(_norm(p) == target for p in previous_bodies if isinstance(p, str)) else []

    # ----------------------------------------------------------- aggregate
    def validate(self, body: Any, *, intent_mode: bool = False,
                 previous_bodies: Iterable[str] = ()) -> ValidationResult:
        if not isinstance(body, str):
            return ValidationResult([Issue("empty_body", "body is not a string")])
        issues = self.check_basics(body)
        if issues and issues[0].code == "empty_body":
            return ValidationResult(issues)
        issues += self.check_numbers(body)
        issues += self.check_codes(body)
        issues += self.check_urls(body)
        issues += self.check_phrases(body, intent_mode=intent_mode)
        issues += self.check_taboos(body)
        issues += self.check_repeat(body, previous_bodies)
        return ValidationResult(issues)


def _category_taboos(category: Optional[dict]) -> list[str]:
    voice = (category or {}).get("voice") or {}
    raw = voice.get("vocab_taboo") or voice.get("taboos") or []
    taboos = []
    for term in raw if isinstance(raw, list) else []:
        if isinstance(term, str):
            cleaned = _norm(re.sub(r"\(.*?\)", "", term))  # "FDA-approved (use only ...)" -> "fda-approved"
            if cleaned:
                taboos.append(cleaned)
    return sorted(set(taboos))
