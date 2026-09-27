#!/usr/bin/env python3
"""Run judge_simulator.py against a local bot without editing the judge file.

The judge is imported as a module and patched in memory:

  1. Configuration: BOT_URL / LLM_PROVIDER / LLM_API_KEY / LLM_MODEL /
     TEST_SCENARIO come from CLI flags or env vars. The stock judge ignores
     env vars despite challenge-testing-brief.md §9.
  2. Simulated clock (default on): the stock judge stamps every request with
     the real wall-clock time, far past the dataset's timeline, so every seed
     trigger would look expired. The patched clock starts at --sim-start and
     advances 5 simulated minutes per /v1/tick, like the real harness
     (challenge-testing-brief.md §4 Phase 2). --real-clock restores stock
     behaviour (the bot must also cope with that).
  3. Auto-reply conversation id (default "single"): the stock judge sends each
     canned auto-reply on a new id (conv_auto_1..4), unlike the documented
     replay, which repeats it in one conversation (api-call-examples.md 4.1).
     "single" routes all four turns into one per-run conversation id;
     "stock" keeps the original rotating ids.
  4. --push-customers (default off): the stock warmup never pushes customer
     contexts, although the real harness pushes all 200 (brief §4 Phase 1).
  5. --offline: stubs the LLM so the harness plumbing can be exercised with no
     API key. Scores then come from the judge's digit-counting fallback and
     are NOT meaningful quality scores.

Examples:
  python run_judge.py --scenario all --offline
  LLM_PROVIDER=openai OPENAI_API_KEY=sk-... python run_judge.py --scenario full_evaluation
"""

from __future__ import annotations

import argparse
import os
import re
import sys
import uuid
from datetime import datetime, timedelta
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

import judge_simulator as js  # noqa: E402  (import after sys.path tweak)

SCENARIOS = ("warmup", "phase2_short", "auto_reply_hell", "intent_transition", "hostile",
             "all", "full_evaluation")
PROVIDER_KEY_ENV = {
    "openai": "OPENAI_API_KEY", "anthropic": "ANTHROPIC_API_KEY", "gemini": "GEMINI_API_KEY",
    "deepseek": "DEEPSEEK_API_KEY", "groq": "GROQ_API_KEY", "openrouter": "OPENROUTER_API_KEY",
}
DEFAULT_SIM_START = "2026-04-26T10:30:00"  # dataset "today" (IPL trigger, week-17 digests)
TICK_STEP = timedelta(minutes=5)
_AUTO_CONV_RE = re.compile(r"^conv_auto_\d+$")


# ------------------------------------------------------------------ clock
class SimClock:
    """Simulated UTC clock: fixed start, advanced explicitly per tick."""

    def __init__(self, start: datetime) -> None:
        self.now = start

    def advance(self, delta: timedelta) -> None:
        self.now += delta


def install_sim_clock(start: datetime) -> SimClock:
    clock = SimClock(start)

    class _SimDatetime(datetime):
        @classmethod
        def utcnow(cls):  # the judge only calls datetime.utcnow()
            return clock.now

    js.datetime = _SimDatetime  # rebinds the name inside the judge module only

    original_tick = js.BotClient.tick

    def tick(self, triggers):
        result = original_tick(self, triggers)
        clock.advance(TICK_STEP)
        return result

    js.BotClient.tick = tick
    return clock


# -------------------------------------------------------- auto-reply conv id
def install_single_auto_reply_conversation() -> str:
    conv_id = f"conv_auto_reply_{uuid.uuid4().hex[:8]}"  # fresh per run: no state leaks between runs
    original_reply = js.BotClient.reply

    def reply(self, conv_id_in, merchant_id, message, turn):
        if _AUTO_CONV_RE.match(conv_id_in):
            conv_id_in = conv_id
        return original_reply(self, conv_id_in, merchant_id, message, turn)

    js.BotClient.reply = reply
    return conv_id


# ------------------------------------------------------------ customers
def install_customer_push() -> None:
    original_warmup = js.JudgeSimulator._warmup

    def warmup(self):
        ok = original_warmup(self)
        if ok:
            pushed = sum(
                1 for cid, c in self.dataset.customers.items()
                if (self.client.push_context("customer", cid, 1, c)[0] or {}).get("accepted")
            )
            js.print_info(f"[run_judge] pushed {pushed}/{len(self.dataset.customers)} customer contexts")
        return ok

    js.JudgeSimulator._warmup = warmup


# ---------------------------------------------------------------- offline
class OfflineProvider(js.LLMProvider):
    """No-network stand-in. Non-JSON answers force the judge's fallback scorer."""

    def name(self) -> str:
        return "OFFLINE STUB (fallback heuristic scores only; not a quality signal)"

    def complete(self, prompt: str, system: str = None) -> str:
        return "ready (offline stub)"


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--scenario", default=os.environ.get("TEST_SCENARIO", "all"), choices=SCENARIOS)
    parser.add_argument("--bot-url", default=os.environ.get("BOT_URL", "http://localhost:8080"))
    parser.add_argument("--provider", default=os.environ.get("LLM_PROVIDER", "openai"))
    parser.add_argument("--model", default=os.environ.get("LLM_MODEL", ""))
    parser.add_argument("--sim-start", default=os.environ.get("SIM_START", DEFAULT_SIM_START),
                        help="simulated UTC start time (ISO, naive)")
    parser.add_argument("--real-clock", action="store_true", help="keep the judge's wall-clock timestamps")
    parser.add_argument("--auto-reply-conv", choices=("single", "stock"), default="single")
    parser.add_argument("--push-customers", action="store_true")
    parser.add_argument("--offline", action="store_true", help="stub the LLM (no API key needed)")
    args = parser.parse_args()

    js.BOT_URL = args.bot_url.rstrip("/")
    js.TEST_SCENARIO = args.scenario
    js.LLM_PROVIDER = args.provider
    js.LLM_MODEL = args.model

    if args.offline:
        js.LLM_API_KEY = "offline"
        js.create_provider = OfflineProvider
    else:
        key_env = PROVIDER_KEY_ENV.get(args.provider, "")
        js.LLM_API_KEY = os.environ.get("LLM_API_KEY") or os.environ.get(key_env, "")

    notes = [f"bot={js.BOT_URL}", f"scenario={args.scenario}"]
    if not args.real_clock:
        clock = install_sim_clock(datetime.fromisoformat(args.sim_start))
        notes.append(f"sim-clock from {clock.now.isoformat()}Z (+5 min per tick)")
    else:
        notes.append("wall-clock (stock)")
    if args.auto_reply_conv == "single":
        notes.append(f"auto-reply conv={install_single_auto_reply_conversation()}")
    else:
        notes.append("auto-reply conv ids rotate (stock)")
    if args.push_customers:
        install_customer_push()
        notes.append("customers pushed at warmup")
    if args.offline:
        notes.append("OFFLINE: LLM stubbed")
    print("[run_judge] " + " | ".join(notes))

    js.main()


if __name__ == "__main__":
    main()
