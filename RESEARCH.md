# RESEARCH.md: Reverse-engineering the magicpin Vera AI Challenge

Scope: this is analysis only. No bot code exists yet. Every claim below is tagged with the file it comes from:

- `[CB]`: challenge-brief.md
- `[TB]`: challenge-testing-brief.md
- `[ED]`: engagement-design.md
- `[ER]`: engagement-research.md
- `[JS]`: judge_simulator.py (line numbers given)
- `[API]`: examples/api-call-examples.md
- `[CS]`: examples/case-studies.md
- `[GEN]`: dataset/generate_dataset.py
- `[DATA]`: dataset/*.json

Anything marked **Recommendation** is my own design judgment, not a documented requirement.

The expanded dataset was generated into a scratch directory with `generate_dataset.py`, which is deterministic (seed `20260426`), so the 30 canonical test pairs could be inspected. Nothing in the repository was modified.

---

## 1. Exact Challenge Objective

We need to build **"Vera"**, magicpin's WhatsApp merchant-growth assistant, as a **stateful HTTP service** `[TB §1–2]`. Its job:

1. **Absorb context over time.** The judge pushes four context types: category, merchant, customer and trigger. They are versioned, can be updated mid-test, and are pushed via `POST /v1/context`.
2. **Decide proactively.** On each `POST /v1/tick`, the bot decides *whether* anything is worth sending right now. If so, it composes **one grounded, category-correct WhatsApp message with a single low-friction next step** per chosen opportunity. The conceptual core is `compose(category, merchant, trigger, customer?) → {body, cta, send_as, suppression_key, rationale}` `[CB §5]`.
3. **Converse.** On `POST /v1/reply`, the judge's LLM plays the merchant or customer. For each reply the bot returns exactly one of `send`, `wait` or `end` `[TB §2.3]`.

What the final submission must expose:

- **HTTP surface:** 5 required endpoints, `/v1/healthz`, `/v1/metadata`, `/v1/context`, `/v1/tick` and `/v1/reply` `[TB §2]`, plus an optional `POST /v1/teardown` that wipes state `[TB §11]`. It must be reachable at a public URL `[TB §6]`.
- **Offline artifacts** from the original brief `[CB §7]`:
  - `bot.py` with a pure `compose(category, merchant, trigger, customer) -> dict` that is deterministic and runs in under 30s
  - `submission.jsonl` with 30 lines, one per canonical test pair
  - `README.md` of at most one page
  - optionally `conversation_handlers.py` with `respond(state, merchant_message)`
- **Recommendation:** provide both. The HTTP service and the offline `compose()` should share one engine.

What "better than Vera" means `[CB §3]`. There are four named production pain points:

1. **Auto-reply detection.** 40–70% of replies are canned WhatsApp Business auto-replies.
2. **Intent handoff.** When a merchant says "let's do it", switch to action and don't re-qualify.
3. **Specific copy.** Service + price ("Haircut @ ₹99") beats "% off".
4. **Engagement frequency.** Use curiosity- and knowledge-driven conversations, not only functional reminders.

---

## 2. Complete API Contract

All endpoints use JSON in and out, UTF-8 `[TB §2]`.

Latency:

- Documented budget: 30s hard `[TB §5]`, with tighter "latency budgets" in `[API summary table]`: healthz 2s, context 5s, tick 10s, reply 10s.
- The local simulator times out even sooner: healthz and metadata 5s, context 10s, tick 15s, reply 15s `[JS 412-434]`.
- **Design target: under 200 ms on every endpoint.**

### 2.1 `GET /v1/healthz`

| Item | Detail |
|---|---|
| Request | none |
| Response 200 | `{"status":"ok","uptime_seconds":int,"contexts_loaded":{"category":n,"merchant":n,"customer":n,"trigger":n}}` |
| Required | `status`, `uptime_seconds`, `contexts_loaded` with all 4 keys (zeros at boot) `[API 1.1]` |
| Errors | Non-200 three times in a row means disqualified for the slot, −10 operational `[TB §2.4, §10]`. Polled every 60s. Retried ×3 `[API table]`. |
| State | Read-only. Counts must equal what was pushed, e.g. 5/50/200/0 after warmup, or warmup fails `[TB §4 P1.4]`, `[API 1.7]`. |

### 2.2 `GET /v1/metadata`

| Item | Detail |
|---|---|
| Response 200 | `{"team_name","team_members":[...],"model","approach","contact_email","version","submitted_at"}` `[TB §2.5]` |
| Notes | The simulator prints only `team_name` and `model` `[JS 638]`. The bot is rule-based, so `model` should state that honestly (e.g. `"deterministic-rules-v1 (no LLM)"`). |

### 2.3 `POST /v1/context`

**Request** `[TB §2.1]`:

```json
{
  "scope": "category|merchant|customer|trigger",
  "context_id": "...",
  "version": 3,
  "payload": { ... },
  "delivered_at": "..."
}
```

| Case | Response |
|---|---|
| New `context_id`, or higher `version` | 200 `{"accepted":true,"ack_id":"ack_<id>_v<ver>","stored_at":ISO}`. The new version **replaces** the old one atomically. |
| Same version re-pushed | See the conflict note below |
| Lower version | 409 `{"accepted":false,"reason":"stale_version","current_version":N}` |
| Bad scope, missing fields, non-object payload, bad JSON | 400 `{"accepted":false,"reason":"invalid_scope"\|"malformed","details":"..."}` |

**Documented conflict.** `[TB §2.1]` says re-posting the same version "is a no-op", while `[API 1.5]` says the same version returns **409 stale_version**.

**Recommendation:** return 409 `stale_version` with `current_version` and *never* touch state. That satisfies both: it is a no-op, and it is the concrete example's response.

Harness behaviour to account for:

- The simulator never reads the status code of a context push after warmup. During warmup it prints FAIL only if `accepted` is falsy on the *first* push `[JS 641-650]`.
- In `full_evaluation`, the first 5 merchants are re-pushed at v1, so they get a 409. This is harmless `[JS 646, 807]`.

Other rules:

- The payload size cap is 500 KB `[TB §5]`.
- **Payloads can be partial.** In `[API 2.8]`, a category v2 omits `offer_catalog`, `peer_stats` and others. The bot must tolerate any missing field.
- **Keying.** Store by `(scope, context_id)`. `context_id` normally equals `payload.merchant_id`, `payload.customer_id`, `payload.id` or `payload.slug`. **Recommendation:** also index merchants by `payload.merchant_id` and categories by `payload.slug` for robustness.

### 2.4 `POST /v1/tick`

**Request:** `{"now": ISO, "available_triggers": ["trg_id", ...]}` `[TB §2.2]`.

**Response 200:** `{"actions":[Action, ...]}`. An empty list is valid and "restraint is rewarded" `[TB §14]`, `[API 2.3]`.

The Action fields that must all be present (`[API F.2]` lists the required ones; `[TB §2.2]` has the full shape):

| Field | Notes |
|---|---|
| `conversation_id` | New, unique, meaningful (e.g. `conv_<merchant>_<kind>_<key>`) `[CS pattern 8]`. Reusing an existing one is invalid. |
| `merchant_id` | Exact dataset id. The simulator looks the merchant up by this id `[JS 836-842]`. |
| `customer_id` | `null` for merchant-facing messages |
| `send_as` | `"vera"` or `"merchant_on_behalf"` |
| `trigger_id` | Exact trigger id. The simulator looks it up `[JS 839]`. |
| `template_name`, `template_params` | The first outbound must be a pre-approved template with `{{n}}` params `[CB §5.1]` |
| `body` | Non-empty. An empty body counts as malformed, −2. |
| `cta` | See the CTA vocabulary in §7 |
| `suppression_key` | Echo the trigger's key (all examples do) |
| `rationale` | The judge cross-checks it against the body; a mismatch is penalized `[TB §14]`, `[CS pattern 9]` |

Limits:

- At most 20 actions per tick `[TB §5]`.
- At most one action per `(merchant_id, conversation_id)` pair per tick `[TB §14]`.
- Missing required fields means the action scores 0 and incurs −2 `[API F.2]`.

### 2.5 `POST /v1/reply`

**Request:** `{"conversation_id","merchant_id","customer_id","from_role":"merchant|customer","message","received_at","turn_number"}` `[TB §2.3]`.

`merchant_id` and `from_role` may be missing (see `[API 2.5]` and `[API 2.6]`). The simulator sends `customer_id: null` and `from_role: "merchant"` always `[JS 429-434]`.

**Response 200:** exactly one of:

- `{"action":"send","body":str,"cta":str,"rationale":str}`
- `{"action":"wait","wait_seconds":int,"rationale":str}`
- `{"action":"end","rationale":str}`

Requirements:

- It must handle **conversation_ids it never created**. The simulator's replay scenarios reply on `conv_auto_1..4`, `conv_intent_1` and `conv_hostile` without any preceding tick `[JS 694, 727, 765]`.
- An empty body on `send` is malformed (−2).
- Repeating the same body verbatim in the same conversation costs −2 per repeat `[TB §10]`.
- A timeout marks the turn `bot_silent`, −1.

### 2.6 `POST /v1/teardown` (optional)

Wipe all state `[TB §11]`. Return 200 `{"ok":true}`.

### 2.7 Error-handling contract (Recommendation)

- Never return 5xx.
- Wrap every handler. On an internal error, return a safe, valid response: `{"actions":[]}` for tick, and `{"action":"wait","wait_seconds":1800,...}` for reply.
- Unknown paths return 404 JSON.
- Bad JSON on `/v1/context` returns 400.
- Bad JSON on tick or reply returns 200 with the safe default, since the harness scores those endpoints and a 4xx is useless to it.

---

## 3. Judge Simulator (judge_simulator.py, read line by line)

### 3.1 Configuration and launch

- Configuration lives in module-level constants `BOT_URL="http://localhost:8080"`, `LLM_PROVIDER="openai"`, `LLM_API_KEY=""`, `LLM_MODEL=""` and `TEST_SCENARIO="all"` `[JS 24-39]`.
- **It does NOT read the `BOT_URL` env var**, despite `[TB §9]` claiming `export BOT_URL=...` works. It also does not read the API key from the environment.
- `main()` exits if `LLM_API_KEY` is empty (unless the provider is ollama), then does a live LLM "ready" ping `[JS 926-952]`.
- **Recommendation:** since we must not edit the judge, write a separate runner (e.g. `run_judge.py`) that imports the module, overrides these globals from env vars, and calls `main()`. **An LLM API key is required to run the judge at all.**
- The judge LLM runs at temperature 0.2 with max_tokens 1500. It supports OpenAI (default `gpt-4o-mini`), Anthropic, Gemini, DeepSeek, Groq, Ollama and OpenRouter `[JS 153-345]`. The **judge is therefore itself non-deterministic**: expect ±1–2 points per dimension between runs.

### 3.2 Dataset visible to the simulator

- `DatasetLoader` reads only `dataset/categories/*.json` plus the three **seed** files. That is 5 categories, 10 merchants, 15 customers and 25 triggers `[JS 351-383]`.
- It does **not** use the expanded 50/200/100 dataset. The real judge does `[CB §6]`.

### 3.3 How it talks to the bot (`BotClient`, `[JS 386-434]`)

- It uses `urllib`, a JSON body and the `Content-Type: application/json` header. **No retries.**
- On an HTTP error it still tries to parse the JSON body. A 401 gives "Unauthorized".
- `tick` sends `now = datetime.utcnow()`, i.e. **real wall-clock time**, not simulated time (currently late 2026). **Almost every seed trigger's `expires_at` (April to June 2026) is in the past relative to that `now`.**
- `reply` always sends `customer_id: null` and `from_role: "merchant"`.

### 3.4 Scenarios (`[JS 606-614]`)

| Scenario | What happens | Scored by LLM? |
|---|---|---|
| `warmup` | healthz, then metadata, then push all 5 categories at v1 and the **first 5 merchants** (m_001…m_005) at v1. **No customers, no triggers.** | no |
| `phase2_short` | warmup, then push the first 3 triggers (`trg_001` research digest, `trg_002` DCI regulation, `trg_003` Priya recall; all three are for m_001), then one tick with those 3 ids. Every returned action is scored. | yes, up to 3 |
| `auto_reply_hell` | 4 replies of `"Thank you for contacting us! Our team will respond shortly."` on **4 different conversation ids** `conv_auto_1…4`, turn_number 2…5, merchant = first merchant (m_001). `end` on any turn prints PASS and stops. `wait` prints PASS but continues. `send` prints WARN. It ends with "Bot never ended" if there is no `end`. | no, behavioural |
| `intent_transition` | One reply `"Ok lets do it. Whats next?"` on `conv_intent_1`, turn 2. PASS if the body contains any of `done, sending, draft, here, confirm, proceed, next` **and none of** `would you, do you, can you tell, what if, how about` (case-insensitive substring match) `[JS 740-749]`. | no, keyword check |
| `hostile` | One reply `"Stop messaging me. This is useless spam."` on `conv_hostile`. PASS if `action == "end"`, or if it is `send` with a body containing `sorry`, `apolog` or `won't` `[JS 776-781]`. | no |
| `all` (**default**) | warmup, auto_reply, intent, hostile. **No LLM scoring at all.** | no |
| `full_evaluation` | warmup, then push all 10 merchants and all 25 triggers, then tick in **batches of 5 trigger ids** (5 ticks), scoring every action. | yes, up to 25 |

Important consequences:

- **Customers are never pushed** in any scenario. In `phase2_short`/`full_evaluation`, the 5 customer-scope seed triggers (`trg_003`, `007`, `015`, `017`, `019`) arrive **without a customer context**. The bot must still behave sensibly: skip, or send a grounded merchant-facing approval message instead.
- Because ticks are batched by file order, **one tick can contain several triggers for the same merchant** (e.g. batch 1 has 3 for m_001 and 2 for m_002).
- Because the auto-reply test rotates conversation ids, **detection keyed only on "same text 3× in the same conversation" never fires locally**. Detection must also use lexical auto-reply patterns and a **per-merchant** consecutive counter.
- `turn_number` starts at 2 on replies to conversations the bot never opened.
- The intent test's forbidden substrings include very common phrasings ("do you", "would you", "how about"). **The action-mode reply must avoid them entirely.**

### 3.5 What the scorer sees (`LLMScorer.score`, `[JS 498-530]`)

The prompt includes only:

- **Category:** slug, `voice.tone`, first 5 of `voice.vocab_taboo`
- **Merchant:** `identity.name`, `identity.owner_first_name`, `identity.locality`, `identity.languages`, `performance.views/calls/ctr`, `signals`, titles of offers with `status=="active"`
- **Trigger:** `kind`, the full `payload` JSON, `urgency`
- **Customer:** `identity` only, or `None` if `customer_id` is null/absent
- **Bot output:** `body` (with char count), `cta`, `send_as`

**Hidden from the scorer** (so the local judge cannot verify it, and may treat it as fabricated):

- category `digest`, `peer_stats`, `offer_catalog`, seasonal beats and trends
- merchant `subscription`, `delta_7d`, `customer_aggregate`, `review_themes`, `conversation_history`, `leads`, `directions`, inactive offers
- customer `relationship`, `state`, `preferences`, `consent`
- the action's `rationale`, `template_*`, `suppression_key` and `conversation_id`

The real judge has everything `[CB §16]`. **Recommendation:** ground facts in the full context, which is correct for the real judge. Prefer trigger-payload facts as the *primary* evidence, since both judges can verify those, and attach a source citation whenever a digest fact is used.

### 3.6 Scoring dimensions and weights (`SYSTEM`, `[JS 443-492]`)

Each dimension is scored 0–10: "be strict — 5 is average, 7+ good, 9+ excellent".

| # | JSON key | What the rubric text says |
|---|---|---|
| 1 | `specificity` | Verifiable facts: numbers (%, counts, prices), dates/times, source citations, concrete rather than vague |
| 2 | `category_fit` | Dentists are "clinical, peer-to-peer, technical OK, **use 'Dr.' prefix**". Salons are "warm, friendly, practical". Restaurants are "operator-to-operator". Gyms are "coaching, motivational". Pharmacies are "trustworthy, precise". |
| 3 | `merchant_fit` | Uses the name / owner's name correctly, references actual data (not fabricated), honours the language preference |
| 4 | `decision_quality` | The prompt heading is **"TRIGGER RELEVANCE: Does it connect to WHY NOW?"** It asks for a clear reason, use of payload data, and "not a generic nudge". If `decision_quality` is missing, the parser also accepts `trigger_relevance` `[JS 555]`. |
| 5 | `engagement_compulsion` | Loss aversion, curiosity, social proof, a clear CTA, a low-friction ask |

- **Weights:** equal. Total = sum of the 5 dimensions (max 50) minus `penalties`, floored at 0 `[JS 134-137]`.
- **Penalties:** the prompt lists "fabricating data −2" and "exposing internal jargon −1", **but the parser never reads a penalties field**, so `penalties` is always 0 locally `[JS 548-560]`. The LLM may still *lower dimension scores* for fabrication or jargon.
- **Fallback:** if the LLM call or JSON parse fails, specificity = min(10, 3 + 2×number-of-digit-groups) and 5 for everything else `[JS 566-578]`.
- **Summary:** per-dimension averages use integer floor division; the total is the sum of the floored averages `[JS 886-905]`. Bands are ≥80% EXCELLENT, ≥60% GOOD, ≥40% NEEDS IMPROVEMENT.

### 3.7 Hard failures and timing (simulator)

- healthz error: the scenario returns False.
- tick error: `phase2_short` fails, `full_evaluation` continues.
- reply error: the scenario fails.
- The simulator never checks schema; it `.get()`s everything. But `data.get(...)` on a non-dict response would crash, so **the response must always be a JSON object**.

### 3.8 Fixed or generated scenarios, and determinism

- All simulator scenarios are **fixed** (hard-coded messages and ids).
- The *real* harness adds generated merchant replies, 15 new triggers, updated performance, new digest items and mid-test customers `[TB §4 Phase 3]`.
- Determinism is required of the bot ("set temperature=0", "must be deterministic given the same inputs") `[CB §7.1]`. It is not a property of the judge.

---

## 4. Scoring Model

The real evaluation (`[CB §8]` + `[TB §4-5, §10]`):

- **Phase 2:** 5 dimensions × 10 = 50 per message.
- **Phase 3:** adaptation bonus up to +5 per dimension, for using newly pushed context.
- **Phase 4:** replay, top 10 only, up to +30. It has 3 scenarios × 5 turns, scored on conversation flow.
- **Operational penalties:** up to −20.
- **Hard caps** from `[CS cross-case]`:
  - Any fabrication or repetition caps the case at 5 per dimension.
  - A research or compliance claim without a citation caps at 7.
  - Near-duplicates of case-study text count as plagiarism (similarity check).

| Criterion | What the judge checks | How to maximize it | Common failure |
|---|---|---|---|
| **Specificity** | Concrete, verifiable number, date, headline, price, source `[CB §8]`, `[JS 447-451]` | Lead with 1–2 facts from the trigger payload or merchant data (e.g. "calls −50% in 7d, 12 → 6"); service@price offers; cite the source for digest items (e.g. "JIDA Oct 2026, p.14") | "increase your sales"; "10% off"; numbers with no provenance (treated as fabrication) |
| **Category fit** | Voice, vocabulary and offer format match the vertical; taboos avoided `[CB §8]`, `[JS 453-458]` | Per-category voice packs built from `voice.tone/register/vocab_allowed/taboo/salutation_examples`; "Dr. {name}" for dentists; operator vocabulary ("covers", "AOV") for restaurants | Promotional hype for a dentist or pharmacy; "guaranteed"; retail tone for clinical categories |
| **Merchant fit** | This merchant's numbers, offers and history; owner name; language `[CB §8]`, `[JS 460-463]` | Owner first name; locality; *their* active offer; *their* metric vs `peer_stats`; continue their `conversation_history`; Hinglish touch if `hi` is in their languages | Generic "Hi"; invented offers; ignoring an explicit prior ask in the history |
| **Trigger relevance / Decision quality** | Clear *why-now* tied to trigger payload data; not a generic nudge `[CB §8]`, `[JS 465-468]` | Open with the trigger event in the first clause; one primary reason; add *judgment* (e.g. the IPL Saturday contrarian call in CS5); choose the right action or restraint | Paraphrasing the trigger without a decision; stuffing several reasons into one message; acting on a trigger that the data contradicts |
| **Engagement compulsion** | Would they reply? Levers, clear low-friction CTA `[CB §10]`, `[JS 470-473]` | Exactly one CTA in the **last sentence**; effort externalization ("I've drafted X — reply YES"); loss aversion from real deltas; curiosity; asking the merchant | Multiple CTAs; buried CTA; long preamble; "Would you like to know more?" |
| **Operational floor** | Schema, latency, health, repetition, URLs `[TB §10]`, `[API F.2-F.5]` | Always-valid JSON, sub-second responses, anti-repeat store, no URLs | Timeouts (−1), malformed (−2), repeat (−2), URL (−3), healthz ×3 (−10) |
| **Replay (top 10)** | Auto-reply detection, intent transition, hostile/off-topic, knowing when to stop `[TB §4 P4]` | Reply state machine (§13) | Re-qualifying after "let's do it"; replying to bots forever; arguing with hostile merchants |
| **Rationale** | Judge cross-checks it against the message `[TB §14]` | Generate the rationale from the same decision object that produced the body | Boilerplate rationale that doesn't match the body |

---

## 5. Decision Quality

The docs explicitly reward **deciding** over **generating**:

- "decide whether anything is worth saying" (restraint is rewarded, spam is penalized) `[TB §14]`
- "the bot adds judgment, not just templating" `[CS pattern 7]`
- "Did it route action requests correctly?" `[CB §8 replay]`

### 5.1 Competing signals (what is available at decision time)

1. **The trigger**, with `kind`, `urgency` (1–5, "ranks against other queued triggers" `[ED]`), `expires_at`, `scope` and `payload`.
2. **Merchant state:**
   - `performance` + `delta_7d` against category `peer_stats`
   - `signals[]` (e.g. `ctr_below_peer_median`, `perf_dip_severe`, `renewal_due_soon:12d`, `dormant_with_vera_14d`, `no_active_offers`, `unverified_gbp`, `engaged_in_last_48h`)
   - subscription status and days remaining
   - `review_themes` and `offers`
   - `conversation_history`, including an unfulfilled merchant ask such as m_001 "Yes please, focus on whitening and aligners"
3. **Category knowledge:** digest items (research, compliance, trend, tech, CDE, supply, alert, seasonal), seasonal beats and trend signals.
4. **Customer state and consent** (customer scope).
5. **Our own state:** what we have already sent, opt-outs, open or waiting conversations, auto-reply streaks.

### 5.2 How the correct choice is determined (documented principles)

- **Every message must have one trigger.** The trigger is the *why now* `[CB §4.3]`.
- **Urgency ranks competing triggers** `[ED TriggerContext]`.
- **One primary reason per message**, with "the single most important next step" `[CS pattern 4]`.
- **Explicit merchant intent overrides pitching** `[CB §3.2, §9 Pattern D, §12.2]`.
- **Contrarian, data-backed judgment** scores highest. CS5 recommends *against* an IPL promo on a Saturday because the restaurants digest says Saturday matches reduce covers by 12%.
- **Reframing, not alarming.** CS7 treats a seasonal dip as expected, backed by `is_expected_seasonal: true` and the gym `seasonal_beats` "Apr-Jun lowest acquisition window".
- **Restraint is a valid decision.** Return `actions: []` `[API 2.3]`.
- **Stop conditions:** opt-out, hostility, three auto-replies, or three unanswered nudges `[CB §12.5]`, `[API 2.6, 4.1, 4.3]`.

### 5.3 Recommendation: deterministic priority score

The `urgency` field is the documented ranker. Everything else is only a small, explainable tie-break adjustment:

```
priority = urgency*10
         + 6 if merchant has an explicit unfulfilled intent matching this trigger (active_planning_intent / history engagement ∈ {intent_action, intent_question})
         + 4 if |metric delta| ≥ 0.30 (perf_dip/perf_spike/seasonal)
         + 3 if hard deadline in payload (deadline_iso, stock_runs_out_iso, expires within the day)
         + 2 if customer-scope transactional (recall/appointment/refill) with consent
         − 8 if trigger contradicted by data (e.g. perf_dip but delta_7d ≥ 0 on every metric)
tie-break: earlier expires_at, then trigger id (lexicographic) → fully deterministic
```

Gating is applied **before** ranking. A trigger is dropped if:

- it is unknown or not loaded
- its merchant or category is missing
- the recipient has opted out
- the suppression key has already been sent to this recipient
- the customer's consent is missing
- the merchant is ended or in a wait period

After ranking: at most **one merchant-facing action per merchant per tick** and one per customer. The rest are *deferred*, not suppressed, so they can go out on a later tick if they are re-offered.

---

## 6. Specificity: usable evidence fields

The anchor must be a fact the merchant can verify `[CB §5.5]`. Numbers without provenance are scored as fabrication `[CS pattern 2]`.

| Source | Fields that make a message specific |
|---|---|
| Trigger payload (**primary**, and visible to both judges) | `delta_pct`, `metric`, `window`, `vs_baseline`, `days_remaining`, `renewal_amount`, `plan`, `festival`, `date`, `days_until`, `match`, `venue`, `match_time_iso`, `is_weeknight`, `theme`, `occurrences_30d`, `common_quote`, `value_now`, `milestone_value`, `competitor_name`, `distance_km`, `their_offer`, `opened_date`, `molecule`, `affected_batches`, `manufacturer`, `molecule_list`, `stock_runs_out_iso`, `available_slots[].label`, `next_session_options[].label`, `wedding_date`, `days_to_wedding`, `days_since_last_visit`, `previous_focus`, `days_since_expiry`, `lapsed_customers_added_since_expiry`, `trends[]`, `estimated_uplift_pct`, `verification_path`, `credits`, `fee`, `deadline_iso`, `top_item_id` / `digest_item_id` / `alert_id` (these resolve to a digest item), `intent_topic`, `merchant_last_message`, `last_topic`, `days_since_last_merchant_message` |
| Digest item (resolved by id) | `title`, `source` (citation), `trial_n`, `patient_segment`, `summary` numbers, `date`, `credits`, `actionable` |
| Merchant `performance` | `views`, `calls`, `directions`, `ctr`, `leads`, `window_days`, `delta_7d.{views_pct, calls_pct, ctr_pct}` |
| Category `peer_stats` | `avg_ctr`, `avg_calls_30d`, `avg_views_30d`, `avg_rating`, `avg_review_count`, `avg_post_freq_days`, retention and churn benchmarks. Always compare like with like, e.g. merchant CTR 2.1% vs peer 3.0%. |
| Merchant `offers` (active only) | service@price titles such as "Dental Cleaning @ ₹299" or "Haircut @ ₹99" |
| Merchant `customer_aggregate` | `total_unique_ytd`, `lapsed_180d_plus` / `lapsed_90d_plus`, `retention_*`, `high_risk_adult_count`, `total_active_members`, `chronic_rx_count`, `delivery_orders_30d`, `repeat_customer_pct` |
| Merchant `review_themes` | `theme`, `occurrences_30d`, `common_quote`, `sentiment` |
| Merchant `subscription` | `days_remaining`, `plan`, `days_since_expiry` |
| Merchant `signals` | `stale_posts:22d`, `renewal_due_soon:12d`, `dormant_with_vera_38d` (these parse into numbers) |
| Merchant identity | `owner_first_name`, `name`, `locality`, `city`, `verified`, `established_year` |
| Customer | `identity.name`, `relationship.last_visit`, `visits_total`, `services_received`, `preferences.preferred_slots`, `preferred_stylist`, `wedding_date`, `training_focus`, `favourite_dish`, `state` |
| Category offer catalog | Only as a *suggestion* ("the catalog pattern Dental Cleaning @ ₹299 could work") and never presented as the merchant's own offer |

**Recommendation (anti-fabrication):** build a per-message **fact bank** of every number and date from the contexts, including derived values the engine computes and registers (e.g. "0.021 → 2.1%", "−0.50 → 50%", "12 → 6 calls"). The validator rejects any body containing a numeral not in the bank and falls back to a safer template.

---

## 7. Category Intelligence

Sources: `[DATA categories]` for voice, offers, peer stats, digest, beats and trends; `[JS 453-458]` for the judge's voice expectations; `[CS]` for demonstrated moves.

### Common CTA vocabulary (observed values)

| CTA | Where it appears |
|---|---|
| `open_ended` | `[TB 2.2]`, `[API 2.2, 2.7]` |
| `binary_yes_no` | `[API 2.4, 4.1]` |
| `binary_confirm_cancel` | `[API 4.2]` |
| `multi_choice_slot` | `[API 2.9]` |
| `none` | `[API 4.3]`, `[CB §5.3]` "no CTA acceptable for pure-information triggers" |

`[CB §5.3]`: "binary choice (YES/STOP) for action triggers."

### 7.1 Dentists

- **Voice:** `peer_clinical`, `respectful_collegial`. Salutation "Dr. {first_name}" (the judge explicitly wants the "Dr." prefix). Hindi-English natural code-mix.
- **Vocabulary:** fluoride varnish, scaling, caries, bruxism, RCT, IOPA, OPG, aligner, zirconia…
- **Taboos:** guaranteed, 100% safe, completely cure, miracle, best in city, doctor approved.
- **Important signals:** research, compliance and CDE digest items (JIDA, DCI, IDA). CTR vs peer (0.030). Recall and lapsed cohorts (`lapsed_180d_plus`, `high_risk_adult_count`). Seasonal beats: Nov–Feb bruxism, Oct–Dec wedding whitening, Jan check-ups, Apr–Jun pediatric +50%. Trends: aligners +62%, whitening +41%.
- **Customer behaviour:** 6-month recall cycles, trust-driven, family/pediatric parent-mediated (e.g. "Aanya (parent: Sneha)").
- **Appropriate actions:** pull the abstract and draft patient-ed content; SOP/X-ray audit checklist before a DCI deadline; recall campaign to the lapsed cohort; GBP posts; respond to a competitor on quality or trust.
- **CTAs:** open-ended or YES to "pull/draft". Slot choice for patient recall.
- **Inappropriate:** price wars with hype ("beat Smile Studio's ₹199!"), medical outcome claims, promotional exclamation.
- **Special constraints:** customer-facing messages make no medical claims `[CB App. B]`. Always cite the source for research and compliance. Generated owner names already contain "Dr." (e.g. `"owner_first_name":"Dr. Sameer"`), so normalize and never write "Dr. Dr.".

### 7.2 Salons

- **Voice:** `warm_practical`, `approachable_expert`. "Hi {first_name}". Emojis are OK sparingly.
- **Vocabulary:** balayage, keratin, smoothening, hair spa, olaplex…
- **Taboos:** guaranteed glow, permanent results, instant transformation, miracle, best in city.
- **Important signals:** bridal windows (Apr–May secondary, Oct–Dec primary 4×), Holi recovery (Mar), monsoon anti-frizz (Jul–Aug), stylist-specific reviews, "walk-in available" GBP tag (+23% calls, magicpin internal digest), service@price offers.
- **Metrics:** calls vs peer 28, CTR vs peer 0.040, `retention_3mo_pct` vs peer 0.55, `lapsed_90d_plus`.
- **Customer behaviour:** occasion-driven (weddings, festivals), stylist loyalty, weekend slots.
- **Appropriate actions:** GBP post of the bridal package, festival booking push using the *existing* offer, curious-ask ("what's most asked for this week?"), win-back of lapsed clients, adding the walk-in tag.
- **CTAs:** YES to draft or post; slot-hold for customers.
- **Inappropriate:** clinical tone, invented packages or prices (CS3's "₹2,499 skin-prep" is *not* in the data, and even the CS notes "verify in offers").

### 7.3 Restaurants

- **Voice:** `warm_busy_practical`, `fellow_operator`. Operator-to-operator.
- **Vocabulary:** covers, footfall, AOV, table turnover, thali, delivery radius.
- **Taboos:** best food in city, guaranteed packed house, viral guarantee.
- **Important signals:**
  - IPL: digest `d_2026W17_ipl_window` says Saturday matches mean −12% covers and weeknights +18%; seasonal beat "Tue/Wed/Thu not weekends"
  - delivery vs dine-in split (`delivery_orders_30d`, `dine_in_orders_30d`)
  - review themes such as `delivery_late` and `weekend_busy`
  - GST packaging compliance from 2026-06-01
  - trends: sugar-free dessert +52%, match-night offer +65%, weekday lunch thali +34%
  - review-count milestones
- **Customer behaviour:** time-of-day and occasion-driven, match nights, office lunch, family brunch.
- **Appropriate actions:** promo-day selection, delivery-SLA fixes after a review theme, bulk/corporate package drafts (only with grounded prices), review-ask to cross a milestone, counter-positioning against a competitor.
- **CTAs:** YES to "draft banner/post/WhatsApp".
- **Inappropriate:** pushing a match-night promo on a non-weeknight (contrarian case), inventing building names (CS6 flags this risk), "flat 30% off" when the merchant has a BOGO or thali price.

### 7.4 Gyms

- **Voice:** `energetic_disciplined`, `coach_to_member`, English-primary with some Hindi.
- **Vocabulary:** membership churn, PT sessions, HIIT, footfall, trial-to-paid.
- **Taboos:** guaranteed weight loss, shred in 7 days, miracle transformation, fastest results.
- **Important signals:** seasonality (Jan surge 4×, **Apr–Jun lowest acquisition, so focus on retention**, Aug–Oct wedding prep, Nov–Dec slowdown), `total_active_members`, `monthly_churn_pct` vs peer 0.08, `trial_to_paid_pct` vs 0.32, PT demand +38% in the 30–50 corporate cohort, 7am slots at 60% capacity, boutique yoga competition.
- **Customer behaviour:** habit/lapse cycles; win-back needs no-shame framing `[CS8]`.
- **Appropriate actions:** reframe seasonal dips (retention challenge instead of ad spend), win-back with free trial (only if the merchant offers it), kids/program launch drafts, trial follow-up with real session options.
- **CTAs:** YES to hold a trial spot or draft a challenge.
- **Inappropriate:** body-shaming, guilt, weight-loss promises, "no auto-charge" claims unless grounded.

### 7.5 Pharmacies

- **Voice:** `trustworthy_precise`, `neighbourhood_pharmacist`, Hindi-English. "Namaste" for seniors `[CS10]`.
- **Vocabulary:** molecule, batch, generic/branded, schedule H/H1, MRP, expiry.
- **Taboos:** miracle cure, guaranteed result, 100% safe, "doctor recommended (without disclosure)", "best price (without supporting data)".
- **Important signals:**
  - supply/recall alerts: urgency 5, batch numbers, `chronic_rx_count`
  - Schedule H1 audit (₹50,000+ penalties)
  - generic metformin price drop (−22%)
  - summer demand shift (ORS/sunscreen/anti-fungal up, cold/cough −60%)
  - chronic-Rx subscription retention (88% vs 27%)
  - delivery set-up, GBP verification
- **Customer behaviour:** chronic refills on fixed cycles, seniors messaged via a family member (`channel: whatsapp_via_son`), high trust needs.
- **Appropriate actions:** filtered affected-customer list plus notification draft, refill confirm-and-dispatch, shelf rearrangement, H1 register audit, verification walkthrough.
- **CTAs:** CONFIRM/YES.
- **Inappropriate:**
  - **any** dosage or medical advice
  - inventing affected-customer counts: CS9's "22 of 240" is *not* in the data, so say "your 240 chronic-Rx customers" and offer to filter
  - inventing totals or phone numbers (CS10's ₹1,420 and phone number are not in the data)
  - alarmist tone on recalls (the digest says "no safety risk beyond suboptimal LDL control")

### 7.6 Category leakage to guard against

The expanded data pairs kinds with the "wrong" categories: `chronic_refill_due` for a **dentist** customer (T08) and `recall_due` for a **gym** (T29) `[GEN random kinds]`. Handlers must take vocabulary from the *merchant's category*, not from the trigger kind. For example, a gym "recall" becomes a "check-in / come back for your next session" message, and a dentist "refill" becomes a generic follow-up with no medicine language.

---

## 8. Merchant Intelligence (merchants_seed.json + generator)

### 8.1 Fields

| Group | Fields |
|---|---|
| Top level | `merchant_id`, `category_slug` |
| `identity` | name, city, locality, place_id, verified, languages, owner_first_name, established_year |
| `subscription` | status (active/expired/trial), plan, days_remaining, renewed_at, days_since_expiry |
| `performance` | window_days, views, calls, directions, ctr, leads, delta_7d{views_pct, calls_pct, ctr_pct?} |
| `offers[]` | id, title, status (active/expired), started, ended |
| `conversation_history[]` | ts, from, body, engagement (merchant_replied, merchant_no_reply, intent_action, intent_question, intent_planning) |
| `customer_aggregate` | category-specific keys |
| `signals[]` | strings, some with a `:value` suffix |
| `review_themes[]` | theme, sentiment, occurrences_30d, common_quote? |

### 8.2 Characteristics of the 10 seeds

| Merchant | Profile |
|---|---|
| m_001 Dr. Meera | Engaged; CTR below peer; stale posts; **unfulfilled ask**: 3 posts on whitening + aligners |
| m_002 Bharat | Severe dip; renewal in 12 days; unverified; dormant; no offers |
| m_003 Studio11 | High performer, growing; 2 service@price offers; unanswered bridal nudge |
| m_004 Glamour | Expired 38 days; dormant; win-back |
| m_005 SK Pizza | Trial ending in 7 days; BOGO Tue–Thu; late-delivery reviews; unanswered IPL question |
| m_006 Mylari | High volume; engaged; **unfulfilled planning ask** (corporate thali) |
| m_007 PowerHouse | Seasonal dip; 245 members; free-trial offer |
| m_008 Zen Yoga | Planning a kids program; spike; 2 offers |
| m_009 Apollo | Compliance-aware; engaged; 240 chronic-Rx; 2 offers |
| m_010 Sunrise | Unverified; no offers; no conversation history; delivery not set up |

### 8.3 Generated merchants (40 of the 50)

- Random views, calls and CTR; `delta_7d` uniform in ±30%.
- **`offers: []`, `signals: []`, `conversation_history: []`, `review_themes: []`.**
- `customer_aggregate` holds only `total_unique_ytd`.
- `languages` is always `["en","hi"]` plus a regional code.
- `owner_first_name` may carry "Dr." for dentists.

This means **most hidden-test merchants give us only identity, performance and subscription.** The engine must produce strong messages from performance vs `peer_stats` alone.

### 8.4 How to use merchant data without hallucinating (Recommendation)

- Name: `owner_first_name` with the category salutation, falling back to the business name.
- Offers: only `status=="active"` offers are "yours". An expired offer may be referenced *as expired* ("your ₹499 deep-cleaning offer ended 28 Feb").
- Peer comparison: only when both sides exist and the gap is meaningful (≥10% relative).
- `customer_aggregate`: key-aware per category. Never compute sub-counts that aren't there.
- `conversation_history`: continue an open merchant ask. Never repeat a previous Vera body. An unanswered previous nudge on the same topic lowers priority.
- `signals`: parse `name:value`, humanize, and never print raw tokens (the judge applies a −1 penalty for "internal jargon").

---

## 9. Customer Intelligence (customers_seed.json + generator)

| Area | Detail |
|---|---|
| **Fields** | `customer_id`, `merchant_id`, `identity{name, phone_redacted, language_pref, age_band, senior_citizen?}`, `relationship{first_visit, last_visit, visits_total, services_received[], lifetime_value, favourite_dish?, chronic_conditions?}`, `state`, `preferences{preferred_slots, channel, reminder_opt_in, preferred_stylist?, wedding_date?, training_focus?, health_focus?, family_size?, office_nearby?, delivery_address?, household_size?}`, `consent{opted_in_at, scope[]}` |
| **States** | `new`, `active`, `lapsed_soft` (3–6 months), `lapsed_hard` (6+ months), `churned` (12+ months) `[ED]`. Expanded counts: active 98, lapsed_soft 48, lapsed_hard 19, new 19, churned 16. |
| **Languages** | `en`/`english`, `hi`, `hi-en mix`, `ta-en mix`, `kn-en mix`, `te-en mix` |
| **Special identities** | Minor via parent ("Aanya (parent: Sneha)", `channel: whatsapp_via_parent`); senior via son (`whatsapp_via_son`); anonymous walk-in with **no consent** (c_015: `opted_in_at: null`, `scope: []`, `reminder_opt_in: false`) |
| **Consent scopes seen** | recall_reminders, appointment_reminders, treatment_followup, promotional_offers, stylist_specific, bridal_package_followup, match_night_specials, lunch_thali_updates, program_updates, renewal_reminders, winback_offers, health_content, kids_program_updates, refill_reminders, delivery_notifications, recall_alerts, seasonal_health_content |
| **Generated customers (185)** | services_received is `[]`; the same first/last visit dates for everyone (2025-09-01 → 2026-04-01); `consent.scope = ["promotional_offers"]` only; `reminder_opt_in` is False for about 20% |
| **Recency** | Use `last_visit` and trigger payload values (`days_since_last_visit`, `last_service_date`). Never compute "days since" from the tick's `now`, which is wall-clock in the simulator. |

**Opportunities:** recall, appointment reminder, refill, win-back, trial follow-up, bridal follow-up and stylist continuity (all tied to trigger kinds, §10).

**Eligibility (Recommendation).** Send to a customer only if all of these hold:

- the customer context is loaded
- `consent.opted_in_at` is set and `scope` is non-empty
- the scope matches the trigger family, **or** (`reminder_opt_in` is true **and** the message is a transactional reminder about the customer's own service)
- the customer has not opted out in our state

If any check fails, **do not message the customer**. Either skip, or send a merchant-facing heads-up, e.g. "Recall window open for 1 patient; want me to send the reminder?".

---

## 10. Trigger System

Documented fields `[CB §4.3]`, `[TB §3.4]`: `id`, `scope`, `kind`, `source`, `merchant_id`, `customer_id`, `payload`, `urgency` (1–5), `suppression_key`, `expires_at`.

**About 75% of the expanded triggers have payload `{"placeholder": true, "metric_or_topic": kind}`**, and so do 14 of the 30 canonical pairs. For those, all evidence must come from the merchant and category.

| Trigger kind | Meaning | Data available (seed payload) | Expected decision | Important constraints |
|---|---|---|---|---|
| `research_digest` | New research in the category digest | `top_item_id` resolves to a digest item (title, source, trial_n, segment, summary) | Summarize one item tied to the merchant's cohort. Offer to pull the abstract and draft patient content. | Must cite the source. If `top_item_id` is not found, pick a digest item with `kind=research` or skip. Never invent a paper. |
| `regulation_change` | Compliance change | `top_item_id`, `deadline_iso` | Deadline plus the concrete change; offer an audit checklist | Urgency 4. Cite the circular. Precise, not alarmist. |
| `cde_opportunity` | Continuing-education event | `digest_item_id`, `credits`, `fee` | Event, date, credits, fee; offer to register or remind | Fee wording must come from the data ("free for IDA members; ₹500 for non-members") |
| `supply_alert` | Batch recall / supply issue | `molecule`, `affected_batches[]`, `manufacturer`, `alert_id` | Urgent: batches, source, plus an offer to filter the affected customers and draft the notice | Urgency 5, the top priority. Don't invent affected counts. State the digest's risk level accurately. |
| `category_seasonal` | Seasonal demand shift | `season`, `trends[]` (e.g. `ORS_demand_+40`) | Shelf/stock or promo action | Humanize tokens like "ORS demand +40%" |
| `festival_upcoming` | Festival ahead | `festival`, `date`, `days_until`, `category_relevance[]` | A plan anchored on an *existing* offer or a catalog pattern; early-booking framing | Skip or deprioritize if the category is not in `category_relevance`. Don't manufacture urgency when it is 188 days away (frame as "early planning"). |
| `ipl_match_today` | Local match | `match`, `venue`, `city`, `match_time_iso`, `is_weeknight` | Weeknight: push a match-night combo. **Not a weeknight: contrarian call** (use the digest's −12%) and lean on delivery or an existing offer. | Time-sensitive, same day |
| `perf_dip` | Metric decline | `metric`, `delta_pct`, `window`, `vs_baseline` | Name the drop with numbers plus one diagnostic or fix action | **Check against `performance.delta_7d`.** If the data contradicts it, don't claim a dip (e.g. T25). |
| `seasonal_perf_dip` | Expected seasonal dip | `metric`, `delta_pct`, `is_expected_seasonal`, `season_note` | Reframe as normal; shift from acquisition to retention | Use the category `seasonal_beats` for the "normal" claim; don't invent peer ranges |
| `perf_spike` | Metric rise | `metric`, `delta_pct`, `vs_baseline`, `likely_driver` | Celebrate briefly; capitalize (double down on the driver) | If tiny (<10%) or contradicted, stay low-key or skip |
| `milestone_reached` | Near or at a milestone | `metric`, `value_now`, `milestone_value`, `is_imminent` | "5 reviews from 150": a review-ask drive | Phrase "imminent" correctly (145 is *not yet* 150) |
| `review_theme_emerged` | Recurring review theme | `theme`, `occurrences_30d`, `trend`, `common_quote` | Quote it and propose an operational fix plus a review-response draft | Negative themes need an empathetic, operator tone |
| `competitor_opened` | New nearby competitor | `competitor_name`, `distance_km`, `their_offer`, `opened_date` | Curiosity/loss framing and differentiation (quality, trust), not a race to the bottom | Name only the competitor given in the payload |
| `dormant_with_vera` | Merchant silent N days | `days_since_last_merchant_message`, `last_topic` | Low-pressure re-open with one piece of fresh value (their own numbers); easy yes | Don't guilt-trip. If opted out, stay silent. |
| `winback_eligible` | Expired subscriber | `days_since_expiry`, `perf_dip_pct`, `lapsed_customers_added_since_expiry` | Loss framing with their numbers since expiry; offer to restart | Don't invent a price |
| `renewal_due` | Subscription ending | `days_remaining`, `plan`, `renewal_amount` | Days left plus what they would lose (their stats) plus an easy renew | Urgency 4; functional; binary CTA |
| `gbp_unverified` | Google profile unverified | `verified`, `verification_path`, `estimated_uplift_pct` | Uplift plus the path; offer a walkthrough | Say "estimated" (it is an estimate) |
| `curious_ask_due` | Weekly question cadence | `ask_template`, `last_ask_at` | Ask one easy question plus reciprocity ("I'll turn it into a post") | The "asking the merchant" lever `[CB §10.7]`. Specificity from their services/offers. |
| `active_planning_intent` | Merchant asked to plan something | `intent_topic`, `merchant_last_message` | **Deliver the artifact now** (a draft plan) with no re-qualifying; one CTA to finalize | Prices only from existing offers/history (e.g. Zen's ₹2,499 kids camp is *in* its conversation history; Mylari's ₹149 thali is its offer) |
| `recall_due` (customer) | Service recall due | `service_due`, `last_service_date`, `due_date`, `available_slots[]` | `merchant_on_behalf`, name, service, real slots, price if the merchant has an active offer | Consent; language_pref; no medical claims; `multi_choice_slot` allowed for booking |
| `appointment_tomorrow` (customer) | Booking tomorrow | (placeholder in all instances) | Friendly reminder plus confirm/reschedule | Don't invent a time if none is given |
| `chronic_refill_due` (customer) | Refill due | `molecule_list`, `last_refill`, `stock_runs_out_iso`, `delivery_address_saved` | Molecules plus run-out date plus confirm dispatch; via the family member if the channel says so | No dosage advice. Don't invent a total price. Category leakage (T08 dentist). |
| `customer_lapsed_soft` / `customer_lapsed_hard` (customer) | Customer lapsed | `days_since_last_visit`, `previous_focus`, `previous_membership_months` | No-shame win-back tied to their past focus; low-commitment offer from the merchant's actual offers | Consent (`winback_offers`/`promotional_offers`); `churned` needs extra gentleness |
| `trial_followup` (customer) | After a trial session | `trial_date`, `next_session_options[]` | Offer the real next session; a parent for kids | `whatsapp_via_parent` means address the parent |
| `wedding_package_followup` (customer) | Bridal pipeline | `wedding_date`, `trial_completed`, `days_to_wedding`, `next_step_window_open` | Days to wedding plus the next step | Don't invent package prices (not in offers) |
| Brief-only kinds (`weather_heatwave`, `local_news_event`, `category_trend_movement`, `scheduled_recurring`, `unplanned_slot_open`, `research_digest_release`, `customer_lapsed_hard`…) `[CB §4.3]`, `[ED]` | May appear in hidden tests | Unknown | **Generic grounded handler:** use the payload's human-readable fields plus the merchant's best metric; binary CTA | Unknown kinds must never crash or fabricate |

---

## 11. Engagement Design Principles

### 11.1 Documented requirements (quoted or paraphrased from the sources)

**When to message**

- Only with a trigger (the why-now) `[CB §4.3]`.
- Restraint is rewarded and spam is penalized `[TB §14]`.
- Stop after "not interested" or after 3 unanswered nudges `[CB §12.5]`.
- Auto-reply 3 times in a row: close `[API 4.1]`.
- First outbound must use a pre-approved template; free-form only within 24 hours of a merchant reply `[CB §5.1]`.

**What to message**

- Service + price over % discounts `[CB §3.3, §11]`.
- Anchor on a verifiable fact `[CB §5.5]`.
- Don't fabricate offers, research, citations or competitor names `[CB §5.8, §11]`.
- Use curiosity- and knowledge-driven conversations to reach 3–5 conversations a week `[CB §3.4]`, `[ED Why]`.

**How much to say**

- Concise, no hard cap `[CB §5.2]`, `[API F.3]`.
- No long preambles `[CB §11]`.
- Don't re-introduce yourself after the first message `[CB §11]`.

**CTA**

- A single primary CTA: binary YES/STOP for action triggers, none for pure information `[CB §5.3]`.
- The CTA lands in the **last sentence** `[CB §11]`.
- Multi-choice only for booking flows `[CS2]`, `[API 2.9]`.

**Tone and language**

- Peer/colleague tone, not promotional `[CB §5.6]`.
- Match the merchant's language; Hindi-English code-mix encouraged when the preference includes `hi` `[CB §5.7]`, `[TB §14]`.
- Detect language per turn `[CB §12.4]`.

**Compulsion levers** `[CB §10]`

1. specificity
2. loss aversion
3. social proof
4. effort externalization
5. curiosity
6. reciprocity
7. asking the merchant
8. single binary commitment

Production Vera underuses **#3 social proof and #7 asking the merchant**.

**Follow-up and conversation flow**

- After "yes", move to action immediately `[CB §9 D, §12.2]`.
- On an auto-reply, try once, then wait, then end `[API 4.1]`, `[CB §9 B]`.
- On an off-topic ask, politely decline and redirect to the thread `[API 2.7]`.
- On hostility, end or apologize once `[API 4.3]`.
- On "asked for time", wait `[TB §2.3]`.

**Suppression:** see §14.

**Merchant attention:** at most one action per (merchant, conversation) per tick `[TB §14]`. URLs are allowed by the brief `[CB §5.4]` but penalized −3 in the examples `[API F.4]` (see the conflict note in §17).

### 11.2 Recommendations (mine)

- One proactive message per merchant per tick, chosen by the priority score. Defer the rest.
- Structure every proactive body the same way:
  - **[salutation]** plus the **why-now hook** (the trigger fact)
  - **evidence** (1–2 grounded numbers)
  - **implication or judgment** (1 line)
  - **single CTA** as the final sentence
- Aim for about 280–480 characters for merchants and about 220–380 for customers.
- Use social proof only when it is grounded: peer_stats ("metro solo practices average 3.0% CTR") or a digest item ("salons adding 'walk-in available' saw +23% calls"). Never "3 dentists near you did X" unless the data says so.
- Stay English-primary for merchants, with one short natural Hinglish phrase when `hi` is in `languages` and the category's `code_mix` is `hindi_english_natural`. For gyms (English-primary) keep it minimal.

---

## 12. State Management

| Concern | Required behaviour | Source |
|---|---|---|
| Context store | Keyed `(scope, context_id)` → `{version, payload, stored_at}`; in-memory is fine; persists for the whole test; never restart | `[TB §2.1]` |
| Versions | Higher version replaces atomically (swap under a lock). Same or lower version returns 409 with no change. Composition always reads the **latest** version at compose time. | `[TB §2.1]`, `[API 1.5-1.6, 2.8]` |
| Stale compositions | Never cache message text across context versions. Recompose on every tick. Adaptation is scored (Phase 3 bonus). | `[TB §4 P3]` |
| Persistence and restart | Memory only, plus an optional `/v1/teardown` wipe. No disk persistence of payloads after the test (privacy). | `[TB §11]` |
| Idempotency of tick | The same trigger offered again must not produce a duplicate send: suppression key per recipient plus conversation registry. A duplicate tick with the same `now` should return `[]` for already-sent items. | `[TB §10]` anti-repetition, `[ED]` dedup |
| Duplicate replies | The same `(conversation_id, turn_number, message)` replayed should return the **same** response (cache the last response per conversation turn) rather than advancing state twice. **Recommendation.** | Determinism `[CB §7.1]` |
| Conversation state | Per `conversation_id`: merchant_id, customer_id, trigger_id, kind, send_as, status (open/waiting/ended), turns (both sides), bodies sent (hash set), auto-reply streak, last detected language, pending proposal (what "it" refers to), wait_until | `[TB §1]` "stateful per-conversation" |
| Merchant engagement state | opted_out(+ts), consecutive auto-replies, unanswered proactive count, last proactive ts, sent suppression keys, last proposal | `[CB §12]`, `[API 4.3]` "suppress all triggers for this merchant for 30 days" |
| Customer state | opted_out, sent suppression keys | `[API 2.6]` |
| Concurrency | Up to 10 req/s. Use a single process with a global `threading.RLock` around state mutation; compose is pure CPU in microseconds. | `[TB §5]` |
| Time | Use the request's `now` / `received_at` for ordering and waits; never assume it matches dataset dates (the simulator uses wall-clock) | `[JS 426]` |

---

## 13. Reply Engine

### 13.1 Reply intents established by the materials

| Intent | Evidence | Expected behaviour |
|---|---|---|
| **Engaged accept / request** ("Yes, send me the abstract", "Yes please send the abstract. Also draft the patient WhatsApp.") | `[TB 2.3]`, `[API 2.4]` | `send`: fulfil **every** ask in one turn, then one low-friction next step (binary) |
| **Commitment / intent transition** ("Ok lets do it. Whats next?", "let's do it", "go ahead", "I want to join") | `[CB §3.2, §9 D, §12.2]`, `[API 4.2]`, `[JS 724]` | `send`: action mode. Concrete next step and a CONFIRM-type CTA. **No qualifying question.** Avoid the forbidden substrings. |
| **Auto-reply** (canned "Thank you for contacting…", Hindi variants such as "aapki jaankari ke liye bahut-bahut shukriya…team tak pahuncha", "main ek automated assistant hoon") | `[CB §3.1, §9 B, §12.1]`, `[API 2.5, 4.1]` | Streak 1: `send` one short owner-flag note (binary). Streak 2: `wait` 86400 (or 14400). Streak 3 and above: `end`. Track per conversation **and** per merchant. |
| **Hard no / opt-out** ("Not interested. Stop messaging me.") | `[API 2.6]`, `[JS 762]` | `end`; suppress the merchant (all proactive) |
| **Hostile / abusive** ("Why are you bothering me. This is useless.") | `[TB §4 P4.3]`, `[API 4.3]` | With an explicit stop: `end`. Without an explicit stop: a one-line apology plus opt-out path (`send`, cta none), or `end`. Never argue. |
| **Off-topic / curveball** ("help me with GST filing") | `[API 2.7]`, `[TB P4.3]` | `send`: politely out of scope, then redirect to the open thread with a single choice |
| **Asks for time / later** ("busy, later", "next week") | `[TB 2.3]` example "Merchant asked for time; back off 30 min" | `wait`, with wait_seconds scaled to what they said (30 minutes to 1 week) |
| **Question about the proposal** ("how much?", "what will the post say?") | `[CB §8]` "curveball question" | `send`: answer from context only; if the data is unavailable, say so and offer the next step |
| **Language switch** | `[CB §12.4]` | Mirror the detected language for this turn |
| **Customer slot choice / confirm** ("1", "2", "CONFIRM", "YES") | `[CS2]`, `[CS10]`, `[API 2.9]` | `send`: confirm the chosen slot or dispatch using only the labels offered |
| **Acknowledgement** ("ok", "thanks", 👍) | Inferred from `[CB §9 A]` | Continue if a proposal is pending; otherwise `end` politely |
| **Unclear** | Inferred | One clarifying *binary* question, at most once; then `wait` |

### 13.2 Recommendation: deterministic state machine

```
classify(message, conv_state) → intent   (ordered rules; first match wins)
  1 empty/whitespace → UNCLEAR
  2 opt-out keywords (stop, unsubscribe, don't message, not interested, band karo, mat bhejo) → OPT_OUT
  3 abuse/hostility lexicon (useless, spam, bakwas, irritating, fraud, profanity) → HOSTILE (with/without stop)
  4 auto-reply lexicon OR verbatim-repeat of previous inbound (per conv or per merchant) → AUTO_REPLY
  5 commitment (let's do it, go ahead, proceed, yes do it, haan karo, confirm, start, join, send it) → COMMIT
  6 time-deferral (later, busy, next week, kal, baad mein, after X) → DEFER
  7 off-topic domain (GST, tax, loan, visa, cricket score, personal) → OFF_TOPIC
  8 question mark / wh-words → QUESTION
  9 short affirmative (yes, ok, sure, haan, 👍) → ACCEPT
 10 negative-soft (no, not now, nahi) → DECLINE_SOFT
 11 otherwise → UNCLEAR
policy(intent, conv_state) → send|wait|end (+ body from reply templates grounded in pending proposal)
```

The **pending proposal** (what "it" refers to) is resolved in this order:

1. the last Vera offer in this conversation
2. the last proactive action to this merchant
3. the latest Vera→merchant proposal in `conversation_history` that the merchant accepted and that is still unfulfilled (e.g. m_001's "3 posts on whitening + aligners")
4. the merchant's highest-priority available trigger
5. a generic "profile + offer" action

---

## 14. Suppression: when Vera must NOT send

1. **Same `suppression_key` already sent to the same recipient.** It is the trigger-level dedup key `[CB §4.3]`, `[ED]`. **Recommendation:** scope it by recipient, because some keys are category-wide (e.g. `research:dentists:2026-W17`) and would otherwise block other merchants.
2. **Merchant opted out, or was hostile with a stop request:** no proactive sends to that merchant for the test (the docs suggest 30 days) `[API 2.6, 4.3]`.
3. **Conversation ended:** no further sends on that `conversation_id` `[API 2.6]`.
4. **Conversation in `wait`:** no nudges until `wait_until` `[API 2.5]`.
5. **Auto-reply streak of 3 or more, or 3 unanswered nudges:** stop `[CB §12.5]`, `[API 4.1]`.
6. **Customer without consent, or with a non-matching scope and no reminder opt-in, or opted out.** This follows `[ED consent]` and CS "opted in" framing. c_015 is the concrete no-consent case.
7. **Repeated body:** never send a body identical to one already sent in the conversation (−2) or to any past body for the merchant, including `conversation_history` `[TB §10]`, `[CB §11]`.
8. **Nothing worth saying:** placeholder payload **and** no meaningful merchant evidence, or a trigger contradicted by the data. Return `[]` or a lower-priority honest message.
9. **Missing prerequisites:** trigger, merchant or category context not loaded; unknown trigger id.
10. **Per-tick caps:** one action per merchant (Recommendation), one per `(merchant, conversation)` (documented), at most 20 total `[TB §5, §14]`.
11. **Category irrelevance:** `festival_upcoming` with a `category_relevance` list that excludes the merchant's category means deprioritize or skip.
12. **Expiry:** `expires_at < now`.

    **Recommendation:** do **not** hard-drop a trigger the judge lists in `available_triggers`. The testing brief defines that list as what the judge "considers active right now", the judge "is the source of truth" `[TB §1, §2.2]`, and the local simulator's `now` is wall-clock.

    Expiry therefore only lowers priority. Relative-time words ("tonight", "in 4 days") come solely from payload fields, never from `now`.

---

## 15. Canonical Cases

A cross-cutting warning: several case studies **contain facts that are not in the dataset**:

- CS3: ₹2,499 skin-prep, 4 sessions, Saturday 4pm
- CS6: building names and tier prices
- CS7: "−25 to −35%", "2x conversion"
- CS8: HIIT class, 30 Apr
- CS9: "22 of 240"
- CS10: ₹1,420, ₹240 saved, phone number

The case studies themselves warn about this (CS3, CS6 notes), and the fabrication cap applies. **Copy the shape, never those facts.** Also, near-duplicate wording of case-study bodies is penalized as plagiarism.

| Case | Context and trigger | Competing signals | Decision | Evidence | CTA | Principle |
|---|---|---|---|---|---|---|
| **CS1 / App. A** Dentist research digest (m_001, trg_001) | CTR 2.1% < 3.0%, 124 high-risk adults, stale posts, engaged in last 48h | Stale posts; CTR gap; unfulfilled post request; DCI compliance (urgency 4) | Share one research item matched to her cohort | trial_n 2,100; 38%; JIDA Oct 2026 p.14; "your high-risk adult patients" | Open-ended "Want me to pull it + draft patient-ed WhatsApp?" | Match the digest item to the merchant's cohort; cite the source; reciprocity |
| **CS2 / App. B** Dentist recall (Priya) | lapsed_soft, weekday evening, hi-en | — | `merchant_on_behalf` recall | Real slot labels, ₹299 active offer, 6-month service | multi_choice_slot + "or tell us a time" | Customer messages use name, language and preference; real slots only; booking may be multi-choice |
| **CS3** Salon bridal follow-up (Kavya) | new, wedding 2026-11-08, `days_to_wedding` 196 | — | Next-step program | 196 days; *price not in data* | Binary: hold a slot | Days-to-event specificity; relationship continuity; **verify packages exist** |
| **CS4** Salon curious ask (m_003) | High performer | Unanswered bridal nudge 22 Apr | Ask one question plus reciprocity | "5 min" | Open-ended question | The "asking the merchant" lever; could be sharper with a grounded guess (e.g. their Hair Spa @ ₹499 offer) |
| **CS5** Restaurant IPL (m_005, trg_010) | Saturday match, BOGO Tue–Thu, trial ending, late-delivery reviews | Late-delivery theme; trial ending | **Contrarian:** skip the match promo, push delivery | DC vs MI, 7:30pm, −12% (digest), existing BOGO | Binary: draft banner + story | Judgment over templating; use the digest to override the naive action |
| **CS6** Restaurant planning (m_006, trg_013) | Explicit "what would it look like" | Milestone 145→150 | **Deliver the artifact** now | ₹149 thali (real); *tiers and buildings unsupported* | Binary: draft outreach | Intent means action; the artifact must be grounded |
| **CS7** Gym seasonal dip (m_007, trg_014) | Views −30%, 245 members, `is_expected_seasonal` | No recent post; morning-crowd reviews | Reframe the dip; retention over acquisition | −30%, 245, Apr–Jun beat | Binary: draft challenge | Pre-empt anxiety using the category's seasonal beats |
| **CS8** Gym win-back (Rashmi, trg_015) | 57 days, weight_loss, 5 months | — | No-shame win-back | 57 days (≈8 weeks), weight-loss focus; *class invented* | Binary YES, no commitment | Remove barriers; tie to their past goal; use the merchant's real offer (3 FREE Trial Classes) |
| **CS9** Pharmacy recall alert (m_009, trg_018) | Urgency 5, 240 chronic-Rx, merchant already asked for the list | — | Urgent compliance action | Batch numbers, manufacturer; *"22" unsupported* | Binary: draft note + workflow | Highest urgency wins; exact identifiers; bounded risk framing |
| **CS10** Pharmacy refill (Mr. Sharma, trg_019) | Senior via son, hi, 3 molecules, run-out 28 Apr | Senior 15% offer, free delivery > ₹499 | Confirm-and-dispatch | Molecules, date, real offers; *total unsupported* | CONFIRM | Respectful salutation; precision; honour the channel (son) |
| **Pattern A** (real Vera) | Merchant asks for a GBP update | — | Do the work and report back | 62.5%, 24–48h | — | Act, report, handle uncertainty honestly |
| **Pattern B** | Auto-reply | — | Try once, then exit politely | — | — | Detect bots fast |
| **Pattern C** | Missed searches | — | Loss-aversion hook | 6,777, Sector 14 | "Want me to show…" | A local, verifiable number |
| **Pattern D** (anti-pattern) | "I want to join" | — | Should have been action | — | — | Never re-qualify after commitment |
| **API 2.4–2.7, 4.1–4.3** | Replies | — | See §13 | — | — | Reply policy |

---

## 16. Hidden-Test / Generalization Risks

1. **Placeholder payloads.** 14 of the 30 canonical pairs and about 75 of the 100 triggers have none of the payload data that visible examples rely on. A payload-template engine produces empty or garbled text here. **Mitigation:** every handler has a *merchant-evidence fallback* (performance vs peer, active offer, subscription, category beat/trend).
2. **Contradictory triggers.** T25 is a `perf_dip` with views +8% and calls +2%. T27 is a `perf_spike` with +2%/+5%. Claiming the dip is a fabrication. **Mitigation:** verify the claim against `delta_7d`; if contradicted, pivot honestly (e.g. calls vs peer benchmark) or restrain.
3. **Category leakage.** Dentist `chronic_refill_due` and gym `recall_due` exist. **Mitigation:** vocabulary comes from the category pack, not the kind.
4. **Name formatting.** `owner_first_name` "Dr. Sameer" would render as "Dr. Dr. Sameer". Names with parentheses ("Aanya (parent: Sneha)", "Karthik (parent: Sumitra)") must address the parent. "(walk-in, no profile)" is not a name.
5. **Missing customer context.** Customer-scope triggers arrive before the customer is pushed (always true in the simulator; in the real judge the customer comes 2 minutes *before*). **Mitigation:** a merchant-facing fallback or skip, never a guessed name.
6. **Unknown kinds or digest ids.** New triggers and digest items arrive in Phase 3, and `top_item_id` may point to an item only in the newer category version. **Mitigation:** resolve against the current version; if not found, select the best digest item by kind, or use a generic handler.
7. **Partial context updates.** Category v2 may omit `offer_catalog`, `peer_stats` or voice fields `[API 2.8]`. Every accessor needs defaults.
8. **Time assumptions.** The simulator sends a wall-clock `now` far past most `expires_at`. Hard expiry filters would silently send nothing. Weekday or "days until" computed from `now` would be wrong.
9. **Hard-coded IDs or case text.** Any `if merchant_id == "m_001"` or copied case wording fails on the expanded or hidden set and triggers plagiarism penalties.
10. **Jargon leakage.** Raw tokens like `6_month_cleaning`, `post_resolution_window_apr_jun`, `ctr_below_peer_median`, `ORS_demand_+40`, `kids_yoga_post` or `MfrZ` are judged as internal jargon (−1). **Mitigation:** a humanizer plus a validator that rejects `[a-z]+_[a-z_]+` tokens in bodies.
11. **Fabricated numbers.** Derived counts ("22 affected customers"), invented prices, peer "ranges". **Mitigation:** the fact-bank validator (§6).
12. **Priority mistakes.** Sending three messages at once to m_001; picking a low-urgency curious-ask over an urgency-5 recall alert; ignoring an explicit merchant intent in history.
13. **State bugs.** Accepting a stale version; mutating state on a 409; double-counting duplicate replies; auto-reply counters never resetting after a genuine reply; reply to an unknown conversation raising `KeyError`.
14. **Nondeterminism.** Dict/set iteration of unordered sources, `random`, time-based ids, or LLM sampling. **Mitigation:** sort everything; derive ids from inputs; no RNG.
15. **Forbidden-phrase traps.** The intent check fails on "do you", "would you" or "how about" anywhere in the action reply `[JS 740]`.
16. **Operational.** 15s simulator timeout; must return a JSON object always; 10 req/s concurrency.
17. **Customers with no or narrow consent.** Generated customers have only `promotional_offers`, and about 20% have `reminder_opt_in` false.

---

## 17. Anti-Patterns (score reducers)

**Documented** `[CB §11]`, `[TB §10]`, `[API F]`, `[CS]`:

- generic % offers when service@price exists
- multiple CTAs
- a buried CTA
- promotional tone in clinical categories
- hallucinated data, citations or competitors
- long preambles
- re-introducing yourself after the first message
- ignoring the language preference
- verbatim repetition
- URLs (−3 in `[API F.4]`; see the conflict note below)
- empty body
- malformed action
- timeouts
- re-qualifying after commitment
- continuing to talk to an auto-reply
- a rationale that doesn't match the body
- copying case-study text
- taboo vocabulary

**Inferred:**

- raw snake_case or signal tokens
- "Dr. Dr."
- claiming a dip that the data contradicts
- manufactured urgency ("hurry!") for events months away
- medical or dosage advice
- inventing slots, times or prices for customer messages
- messaging customers without consent
- sending more than one proactive message to the same merchant in the same tick
- asking "Would you like to know more?"

**Conflict: URLs.** `[CB §5.4]` says URLs are "allowed when they add clear value", while `[API F.4]` says a URL is a "Hard fail … −3 per URL". **Recommendation: never include URLs.** The downside is certain and the upside is small.

---

## 18. Recommended Architecture

A deterministic decision engine with **no LLM in the loop**. Justification:

- determinism is mandatory `[CB §7.1]`
- latency budgets are tight (15s in the simulator)
- the privacy rule limits external calls `[TB §11]`
- there are no API keys or quota to fail mid-test

Python 3.11 **standard library only** (`http.server.ThreadingHTTPServer`, `json`, `threading`, `re`, `hashlib`). FastAPI is not installed and isn't needed, which gives zero dependency risk and a trivial deploy.

```
HTTP layer (server.py)
  → parse JSON safely → route → handler → always-valid JSON response
        │
        ▼
INPUT → normalize → validate → update state → extract signals → rank opportunities
      → choose action → select evidence → choose CTA → compose → validate → respond
```

| Module | Responsibility |
|---|---|
| `server.py` | ThreadingHTTPServer, routing, body-size cap, JSON errors to 400/200-safe, logging, `PORT` env |
| `store.py` | Versioned context store (`(scope, id)` → version/payload), atomic replace under an RLock, counts for healthz, teardown |
| `state.py` | Conversation registry, merchant/customer engagement state (opt-outs, streaks, sent keys, bodies sent, wait_until, pending proposal), duplicate-reply cache |
| `normalize.py` | Tolerant accessors; name/salutation normalization ("Dr." de-dup, parent extraction); number/percent/date/₹ formatting; snake_case humanizer; language profile (merchant languages + category code_mix; customer language_pref; per-turn detection) |
| `signals.py` | Derives typed signals: perf deltas and whether they're significant, peer gaps, subscription risk, active offers, open intents from history, review themes, digest match by kind/segment, seasonal beat for the trigger month when a payload date exists; **contradiction checks** |
| `triggers.py` | Registry mapping kind → family handler (≈12 families + generic); eligibility/gating (consent, relevance, opt-out, suppression); priority score (§5.3); deterministic ordering |
| `categories.py` | Five strategy packs (salutation, register, lexicon, CTA verbs, forbidden phrases, preferred evidence order, customer-facing rules) generated from category JSON plus judge voice hints; a safe default pack for unknown categories |
| `evidence.py` | Picks the primary fact and at most one supporting fact per decision; registers every emitted number in the **fact bank** |
| `cta.py` | Maps the decision to exactly one CTA type and final-sentence phrasing (binary YES/CONFIRM for action, open_ended for knowledge offers, multi_choice_slot for bookings with real slots, none for pure info or opt-out acknowledgement) |
| `compose.py` | Assembles salutation, hook, evidence, judgment and CTA from category-specific phrase banks. Deterministic variant selection (hash of ids) to avoid identical bodies across merchants. Also produces template_name/params and rationale from the same decision object. |
| `validate.py` | Schema; non-empty; single CTA; no URL; no taboo words; no raw snake_case; every numeral in the fact bank; not previously sent; length bounds. On failure, a safer fallback composition, and if that also fails, skip. |
| `reply.py` | Intent classifier plus the policy state machine (§13); grounded reply templates; language mirroring; forbidden-phrase guard in action mode |
| `bot.py` | Offline `compose(category, merchant, trigger, customer)` wrapper around the same engine, plus `submission.jsonl` generator |
| `conversation_handlers.py` | `respond(state, merchant_message)` wrapper around reply.py |

**Determinism strategy:**

- no randomness
- sorted iteration everywhere
- ids derived from inputs (`conv_{merchant}_{kind}_{short-hash(suppression_key)}`)
- variant choice = `sha256(ids) mod n`
- no dependence on wall-clock except the request's own `now` / `received_at` for waits, and `stored_at`/uptime in acks and healthz

---

## 19. Testing Strategy

The test tool is stdlib `unittest`, plus an HTTP test client that uses `urllib` against a server started in-process on an ephemeral port.

| Area | Tests |
|---|---|
| Contract | Every endpoint; schema of every response; healthz counts after pushing 5/50/200/100; metadata fields; 404 path; bad JSON; oversize body |
| Context versions | New gives 200; same version gives 409 with no mutation; lower gives 409; higher replaces (compose reflects new numbers); partial payload (category v2 without offers) doesn't crash; unknown scope gives 400; context_id ≠ payload id |
| Canonical examples | Every seed trigger and all 30 expanded test pairs produce either a valid action or deliberate restraint with a reason; no crash; bodies pass the validator |
| All categories × all kinds | Cartesian sweep (5 categories × 26+ kinds × merchant variants incl. sparse generated merchants) to check no exceptions, no jargon, no "Dr. Dr.", no fabricated numerals, exactly one CTA |
| Multiple simultaneous signals | A tick with 3 triggers for one merchant yields one action, the highest priority (e.g. urgency-4 regulation beats urgency-2 digest); deferred triggers are sent on a later tick |
| Contradictions | perf_dip with positive deltas does not claim a dip; perf_spike +2% is low-key or skipped |
| Missing data | No offers, no history, no peer_stats, placeholder payload, digest id not found, unknown category, unknown merchant: safe output |
| Repeated requests | The same tick twice gives no duplicate sends; the same reply twice gives the identical response and no double state advance |
| Context updates | Merchant v2 perf change is reflected in the next composition; category v2 new digest item is used when referenced |
| Customer cases | Consent OK / missing / opted out; parent/son channel addressing; language_pref en/hi/hi-en; real slots only; `merchant_on_behalf` |
| Replies | The 3 simulator scenarios exactly (rotating conv ids for auto-reply; the intent forbidden-substring check; hostile stop); accept, question, off-topic GST, defer, ack, unclear, language switch, reply on an unknown conversation, reply after end, rejection after acceptance, acceptance after objection |
| Suppression | Suppression key per recipient; opt-out blocks future ticks; wait blocks nudges; auto-reply streak resets on a genuine reply |
| Malformed input | Non-JSON, JSON array, missing fields, wrong types (version as string), null payload, huge strings, unicode/emoji |
| Determinism | Run the full suite twice in fresh processes; byte-compare all outputs |
| Load | 10 req/s for 60s mixed endpoints; p99 latency under 100 ms; no errors |
| Judge | Runner wrapper for `phase2_short`, `full_evaluation`, `all` (needs an LLM API key); record per-dimension averages; inspect the lowest-scoring messages |
| Unseen scenarios | A seeded scenario generator (different seed from the dataset) that mutates payloads, deltas, languages, names, kinds; asserts validator invariants rather than exact text |

---

## 20. Implementation Plan

1. **Stage 0 — harness scaffolding.** Add `run_judge.py`, which imports `judge_simulator` and overrides `BOT_URL`/`LLM_*`/`TEST_SCENARIO` from env without editing the file. Add `scripts/expand_dataset.sh` (generator output to `dataset/expanded/`, git-ignored or committed as test fixtures) and `.gitignore`.
2. **Stage 1 — service skeleton and contract.** `server.py`, `store.py`, `state.py`; all 6 endpoints; version semantics; safe error paths; contract tests green.
3. **Stage 2 — normalization and signals.** `normalize.py`, `signals.py` with name, number and humanizer utilities; contradiction checks; fact bank.
4. **Stage 3 — decision layer.** `triggers.py` registry, eligibility, consent, suppression, priority, per-tick selection; tests for ranking and suppression.
5. **Stage 4 — category packs and CTA engine.** `categories.py`, `cta.py`.
6. **Stage 5 — composers.** One family at a time (knowledge/digest, compliance/alert, performance, subscription/lifecycle, local event/festival/IPL, reviews/competitor, milestone, curious-ask, planning-intent, customer recall/appointment/refill/lapse/trial/bridal, generic fallback). Then `validate.py` with fallbacks. Sweep tests over all kinds × categories.
7. **Stage 6 — reply engine.** Classifier plus policy state machine; simulator-scenario tests; adversarial reply tests.
8. **Stage 7 — offline artifacts.** `bot.py` `compose()`, `conversation_handlers.py`, `submission.jsonl` for the 30 pairs.
9. **Stage 8 — evaluation loop.** Run the judge (with the user's API key) for `phase2_short` and `full_evaluation`; triage the lowest dimensions; fix general causes (not case text); re-run; determinism and load checks.
10. **Stage 9 — docs and deploy.** README (≤1 page core plus appendix), Procfile/Dockerfile, deploy notes (Render/Railway/Fly/ngrok), final self-audit checklist.

**External dependency needed from the user:** an LLM API key for the judge simulator (OpenAI, Anthropic, Gemini, Groq, DeepSeek or OpenRouter), and outbound network access to that provider from wherever the judge runs. The bot itself needs no key.
