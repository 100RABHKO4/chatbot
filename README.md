# Vera: a deterministic merchant-engagement bot (magicpin AI Challenge)

Vera decides first and writes second. For each trigger it asks whether anything is worth saying, what the single strongest verified reason is, and what one low-friction step the merchant should take. Only then does it compose the WhatsApp message.

## Approach
- **Zero dependencies.** Python 3.11+ standard library only (`http.server`, `json`, `re`, `threading`). Nothing to install.
- **No LLM in the loop.** Composition is rule-based and deterministic: identical inputs give byte-identical outputs. No API keys, no network calls, no sampling. Measured: a tick decides in ≤ 8 ms; with 10 concurrent clients, p99 is about 30 ms.
- **Fact-bank validator.** Every outgoing body (proactive or reply) is checked against a validator scoped to the evidence *selected for that decision*. Any numeral not present in that evidence is rejected. So is any raw internal code (`snake_case`, ids, `{placeholders}`), any URL, any category taboo word, and any qualifying phrase ("do you", "would you", "how about"). A failed draft falls back to a minimal template; if that also fails, nothing is sent. Names, offers, citations and competitors are only ever copied from context fields, never generated.

## Architecture
```
/v1/context -> state.py       versioned store (same/lower version -> 409), per-merchant indexes
/v1/tick    -> decision.py    per trigger: gate (consent, opt-out, snooze, suppression)
                              -> family builder picks evidence or declines (contradiction-checked)
                              -> priority = urgency*10 + time/impact/goal - penalties
                              -> best plan per merchant (max 1 per tick), rest deferred
            -> composer.py    greeting + why-now + evidence + one CTA, via strategies/<category>.py
            -> validator.py   scoped fact bank; fallback template or restraint
/v1/reply   -> reply.py       opt_out > hostile > auto_reply > defer > decline > accept
                              > off_topic > question > unclear; "yes" binds to the pending proposal
```
- **Signals** (`signals.py`) are cross-checked. A `perf_dip` trigger is never repeated if the merchant's own 7-day numbers show growth: Vera pivots to a verified peer gap or stays silent.
- **Five category packs** (`strategies/`) supply vocabulary, deliverables, CTAs, customer tone and extra compliance terms. Examples: "Dr." salutation and citations for dentists; no diagnosis or dosage language for pharmacies; contrarian IPL advice for restaurants.
- **Customer messages** are sent `merchant_on_behalf` only with matching consent. They honour language preference and parent/son channels, and offer only real slots and offers.
- **Auto-replies and hostility** are counted per conversation *and* per merchant, so a changing conversation id can't defeat detection. For auto-replies: one note, then wait 24 h, then end.

## Run
```bash
python3 server.py --port 8080          # or: python3 bot.py --port 8080  (PORT env honoured)
python3 -m unittest discover -s tests -t .
python3 make_submission.py             # regenerates submission.jsonl (30 canonical pairs)
python3 run_judge.py --scenario all --offline                 # judge plumbing, no key needed
OPENAI_API_KEY=... python3 run_judge.py --scenario full_evaluation --push-customers
```
`run_judge.py` imports `judge_simulator.py` unmodified. It reads the bot URL and LLM key from the environment, and optionally simulates the dataset clock and the documented single-conversation auto-reply replay. Deploy anywhere that runs Python: `Procfile` and `Dockerfile` are included. Optional env: `VERA_TEAM_NAME`, `VERA_TEAM_MEMBERS`, `VERA_CONTACT_EMAIL`.

## Tradeoffs
- **Rules over an LLM.** Copy is less varied than LLM prose, but every claim is traceable and a test run is exactly reproducible. Category packs are data, so adding a vertical is mostly data work.
- **Restraint over coverage.** Contradicted or empty triggers can produce no message, and only one proactive message per merchant goes out per tick.
- **Acceptances describe the work** ("I'll draft… you'll see it here"). They include a real draft only when the context supports one.

## What would have helped most
Real payloads for generated triggers (about 75% are placeholders), open appointment slots per merchant, and per-service prices in `offers`. These would let more messages carry service+price and slot specifics instead of benchmark comparisons.
