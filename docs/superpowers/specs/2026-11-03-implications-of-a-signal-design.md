# Implications of a signal: the reasoning leap

Design approved on 3 November 2026.

## Context and problem

The owner rented a car with OK Mobility for 12–15 November (Asturias airport) and Radar was proactive about nothing: it neither asked whether they had insurance, nor looked in the mail for which insurance they have, nor considered lodging. What happened, verified in the database:

1. **The confirmation mail arrived as HTML only** and Radar does not convert HTML to text: the model received 500 characters of `<!DOCTYPE html><head><style>…` and wrote "no dates or place because the body is truncated". It is not an isolated case: 79 of the 1,125 mails of the last 14 days are HTML-only (invoices, alerts, booking confirmations).
2. Without dates, the planner read "Your booking is complete" as "trip over" and closed the task (`done`) in the same minute, with a single informative notice.
3. Even if it had read the body: the trip checklist (outbound, lodging, return, transfers, check-in, documents, calendar) is fixed, does not depend on the means of transport, contains no "insurance", and a `missing` item turns into no step. The prompt already said "relatives near the destination → lodging not applicable", but the memory query is generic (title + entities) and does not ask that.
4. **Chats have the same hole**: Radar captures the turns of Desktop, Telegram, mobile and CLI, but a session in which the owner and the agent rent a car falls under the rule "resolved conversation = noise": the model marks it `resolved` and the code drops it without creating anything.

What the host's memory (hindsight) already knows, checked: the complete itinerary of the trip (train Chamartín–Avilés on the 12th, event at 17:00, OK Mobility car until Sunday the 15th, 130 €, automatic hybrid), who the owner's parents and sister are —but not where they live— and that they studied in Oviedo. About car insurance it has nothing. One hindsight query takes about 17 s.

## Goal

That any signal (mail, chat, calendar, GitHub) triggers the reasoning "this implies things": the model derives what *this* signal implies for *this* owner; the code queries memory with concrete questions, adds the facts and the own history, and the planner resolves each implication: not applicable / done / missing. **What is missing is always a step**: investigate (mailbox, web) when Radar can find out; ask the owner only when only they know, and their answer is kept as a fact so it is not asked twice.

No content is written in code. The only thing written is judgement (a few lines of prompt with examples to calibrate, as today explains what a `work_item` is) and the minimal list per kind that already exists, which the model extends.

## Principles

- The model derives; the code does plumbing and never invents content; the planner decides.
- The owner's word beats any inference (`owner` > `inferred` facts), as today.
- Mail is never sent on its own; the external (contracting, booking, paying) is always the owner's decision with consent. Searching the mailbox and comparing options is reading.
- Independent of the origin: implications are derived in the extractor, which already receives every bundle.
- Bounded cost: two model calls per bundle, as today; the targeted memory queries run in parallel within the current timeout, and what does not arrive in time simply does not enter.

## Design

### 1. Prerequisite: HTML-only mails and re-reading the body

- `radar/providers/mcp_parsing.py`: `html_to_text(text) -> str` with the standard library's `html.parser.HTMLParser` (no new dependencies). Drops `script`, `style`, `head`, `title`, `noscript`; line break on blocks (`p`, `div`, `br`, `tr`, `li`, `h1`–`h6`, `table`, `section`, `article`, `blockquote`); entities decoded (`convert_charrefs`); spaces and blank lines collapsed; `href`s are not kept (the extractor already has the `url` entity). `looks_like_html(text) -> bool`: the gmail-mcp server's note ("This email is HTML-formatted. Plain text version not available.") or `<html`, `<!doctype`, `<body` in the first 400 characters.
- `GmailMcpProvider.read()` and `thread()` apply the conversion when `looks_like_html`; `snippet` comes from the converted text. The `MAX_BODY` cap applies after converting.
- `radar/pipeline/process.py`: `reload_bodies(rt, signals, now_dt)` before extracting. For each `email` signal of the bundle without `raw_content` and with a `content_ref` `gmail:<account>:<id>` whose `source_id` has an adapter with a provider in `rt.sources`, read the message in a thread (`asyncio.to_thread`, 20 s per message, at most 8 per bundle), store `raw_content` (converted) with `raw_expires_at = now + RAW_HOLD_HOURS` through `store.update_signal` and use the fresh body in the extractor. A failure is logged and ignored: the extractor goes on with the snippet, as today. This covers the body expired after 48 h, late retries and `hermes radar replay`.

### 2. Extractor: the model derives the implications (any origin)

- `EXTRACT_SCHEMA` gains `implications`, a list of objects `{concern, why, settled_by, memory_question}` with `settled_by ∈ {memory, mailbox, web, owner, evidence}`. All properties are required (strict providers); `memory_question` is `""` when not applicable.
- `validate_extract`: at most 8 implications; `concern` ≤ 160 characters, `why` ≤ 300, `memory_question` ≤ 200; `settled_by` out of range → `evidence`; without `concern` → dropped; absent → `[]`.
- `EXTRACT_PROMPT`, a new rule of judgement (not of list):

  > implications: what this bundle implies for the owner beyond what it says — the things that will need to be true, decided or arranged because of it. Reason from the situation, not from a template: a trip or a move implies somewhere to sleep unless the owner has a home or family there; the means of transport implies its own needs (a rented car: insurance cover, a licence, a card for the deposit; a flight or train to a city the owner does not know: how to get from the airport or the station; a foreign destination: documents, roaming, currency); a purchase implies its warranty and return window; an appointment or a deadline implies what must be ready before it. List only what matters for this owner now, never generic advice, at most six. For each one say who can settle it: "memory" when the owner's long-term memory may already know (where their family lives, whether they visited the city before), "mailbox" when their past mail would show it (a policy, a cover, a receipt), "web" when it must be looked up, "owner" when only they can answer (whether they will stay with relatives), "evidence" when this bundle itself settles it. memory_question: the one short question to ask the owner's memory about it, in the owner's language, or "" for evidence.

- Conversations: the same rule applies to the chat bundle, and the definition of `resolved` changes: *"a conversation in which the owner and the agent did something that happens later — booked a car or a room, planned a trip, set an appointment, placed an order — is not resolved in the sense that matters: mark it `deferred`, track it as the task it is (trip, purchase, commitment) with its dates, and derive its implications like for any other signal. Only a conversation that leaves nothing ahead is noise."* The safety net in code (`resolved` + decision `new`/`none` → drop) stays.
- Synthetic extracts (job result, owner's answer, refresh, own mail) carry `implications: []`; for those paths the planner receives the implications stored on the task.
- Persistence: `task.entities.implications` (list of `{concern, why, settled_by, memory_question}`), merged by `_merge_entities` (deduplication by hash, cap 40). No schema migration.

### 3. Plumbing: targeted memory and own history

- `radar/pipeline/recall.py`: `Recall.answer(questions) -> list[{question, memory}]`. Deduplicates and drops empty ones, takes at most `context.questions` (new, 3 by default), launches the queries to the memory provider **in parallel** (`asyncio.gather`) with the current `context.timeout_seconds` as a common deadline; each answer is clipped to `context.max_chars // 2`. Without a provider or with `context.memory: false` it returns `[]`. It never raises.
- Where it is called: in `process_bundle` after the extractor (bundles with a model extract) and in `review_task` (with the questions stored on the task), always before the plan. In bundles with a synthetic extract (job result, owner's answer) nothing is asked again: the planner already has the checklist states. The added latency (up to the timeout) is acceptable: the pipeline runs in the background.
- Own history: `history(store, places, exclude_id)`: for the first two `entities.places`, `store.search_tasks(place, limit=5, open_only=False)` → `[{id, title, kind, status, when}]` without the current task, at most 6. "Has been there before" comes out of here for free.
- The planner receives `implications: [{concern, why, settled_by, memory}]` (the memory answer already attached to its implication) and `history`.

### 4. Planner: resolve, do not list

- `reason.build_input` adds `implications` and `history` to the payload.
- `PLAN_PROMPT`, a new rule (after 3, the checklist):

  > 3b. implications are what the bundle implies for the owner, each with what their memory answered. Every implication is a checklist item with its concern as the item text. Decide its status from evidence, never from guesswork: not_applicable when memory, the facts or history settle it away (family or a home at the destination, the owner lives there or has been there before, the cover is already included, the document is valid); done when the bundle or the facts prove it; missing when nothing settles it. What is missing always becomes a step, never a note: settled_by mailbox or web → a read step (investigate, web.read) that finds out — search the mailbox for the policy or the cover, prepare the transfer options — because the assistant never asks the owner to check what it can check itself; settled_by owner → an "ask" whose fact.key is stable for that subject (lodging_asturias, not a date), so the answer is kept and never asked again; settled_by memory with no answer → treat as owner when only they can know, else investigate. Order the steps by what blocks the owner soonest. The caps stay: three steps, one question per task unless two are truly independent, nothing the owner declined or let expire.

- Trip lifecycle, in the same prompt (rule 2, `status`): *"a booking confirmation ("booking complete", "confirmed") is a booked step, never the end of the trip: due_at is the return date, the task stays open until then, and next_review_at falls a few days before departure, when transfers and check-ins become actionable."*
- Safety net in code (`act.apply`): a `trip` task with a future date (maximum of `entities.dates[].when` and `due_at` above `now + 24 h`) does not move to `done` by the plan's `status` unless the decision is `completes` or `contradicts` (cancellation). The previous state is kept (or `open` if new) and the `pipeline` audit notes it (`kept_open: future_dates`). It is exactly what closed the rental.
- `PLAN_SCHEMA` does not change: checklist items already admit free text and `merge_checklist` adds the new ones; `label()` lets unknown items through, which already come in the owner's language.

### 5. Work: finding out in the mailbox

- `radar/jobs/prompts.py`: a `FIND_OUT` block for `read` steps of tasks that are not `work_item`:

  > HOW TO FIND OUT WHETHER THE OWNER ALREADY HAS SOMETHING (a policy, a cover, a booking, a document, a membership): search their whole mailbox with several queries (the provider's name, the thing itself, its synonyms in the owner's language; old mail included, not only recent), their calendar and the owner facts. Report what you found with provider, dates, what it covers and what it does not, and the message ids as refs; separate what you verified from what you assume. If nothing turns up, say so plainly and name what would settle it. Never contract, book or pay: comparing options is reading; choosing is the owner's.

- With `autonomous` trust, read steps run on their own and the result reaches the owner; "if it is not fully comprehensive, look for options" is another read step; contracting is external and always their decision.
- Tasks born in a chat: `observation.session_auto_jobs: false` makes those steps be offered as a decision instead of running. Protection against duplicated work with the chat is already given separately by `task_session_active`. Owner's decision: set `session_auto_jobs: true` in their profile when deploying (one line of config); the plugin's default does not change.

### 6. Config, observability and documentation

- `context.questions` (positive integer, 3 by default) in `CONFIG_DEFAULTS`, `_NESTED_KEYS`, the validation (`_positive_int`) and `config_schema.json` (property and `required` list).
- Observability: today the plugin's INFO logs do not reach `gateway.log`, so there is no way to know whether memory answered. `act.apply` gains the `recall` argument and the `pipeline` audit records `recall: {memory_chars, sessions, answers}`; `recall_summary` gains `answers`. `process_bundle` and `review_task` pass it.
- README: new bullet "Implications" (what the model derives, how it is resolved, what happens with what is missing) and update "Memory and sessions" (targeted queries, `context.questions`) and "Sessions judged as a whole" (a conversation that leaves something ahead is not noise).

### 7. Tests

- `tests/unit/test_sources.py`: an HTML-only `read_email` (fixture with the server's note and a rental confirmation with dates and place between `<table>`/`<style>`) returns a text body with the dates and the place, without `style` or tags; the snippet is text.
- `tests/unit/test_implications.py` (new):
  - the validator bounds the implications (at most 8, `settled_by` out of range → `evidence`, without `concern` → out, absent → `[]`);
  - `process_bundle` sends each implication's `memory_question` to memory (a fake provider that records queries, in parallel and with the `context.questions` cap) and the plan receives `implications` with their answers and `history` with an earlier task at the same place;
  - the implications stay in `task.entities.implications` and `review_task` asks them again;
  - a chat bundle "we rented a car for the 12th" with `conversation_state: deferred` creates a `trip` task with implications; a `resolved` one with nothing ahead is still ignored;
  - a `trip` task with future dates does not close by the plan's `status: done` with decision `new`; it does with `contradicts`;
  - a signal without `raw_content` and with a `content_ref` is re-read by the provider before extracting, and a provider that is down does not block.
- `tests/unit/test_recall.py`: `answer()` runs in parallel, respects the cap and survives timeouts and errors.
- `tests/unit/test_config_v3.py`: valid and invalid `context.questions`.
- Prompt assertions like the existing ones (`"implications" in EXTRACT_PROMPT`, `"never the end of the trip" in PLAN_PROMPT`).
- Whole suite green (`uv run --with pytest --with fastapi --with httpx python -m pytest tests -q`).

### 8. Deployment and verification with the real trip

1. `session_auto_jobs: true` in the owner's profile (config.yaml; saving the config restarts the gateway).
2. Restart of `hermes-gateway.service` only with 0 jobs running (read the database with its `-wal`).
3. `hermes radar replay <OK Mobility signal 190481a8…>`: the body is re-read as text now → task "Trip to Asturias 12–15 Nov, OK Mobility car" open until the 15th. Expected: a **question** "Will you stay at your family's in Asturias?" (answer → fact `alojamiento_asturias`) and an **automatic investigation** in the mailbox about the car insurance, with a result and, if there is no full cover, options for the owner to decide.
4. A short chat in Desktop ("I booked X for day Y") → when the session goes quiet, a task with implications.
5. Check in the database (with `-wal`) and in the `pipeline` audit that `recall.answers` > 0 and what memory answered.

### 9. Risks and limits

- Invented implications → noise: cap of 8 per extract (6 suggested), states by evidence, three steps per plan, one question per task, what was rejected or expired is not asked again, and investigations run silently and only arrive with a result.
- Memory latency (17 s per query): `context.questions` cap, parallel, common deadline; what does not arrive does not enter. If hindsight serialises the queries, lowering `context.questions` to 2 is one line of config.
- The "trip with future dates does not close" net may hold a trip the owner considered finished early: their word in a chat arrives as `completes` and passes.
- A converted HTML body may bring template text (footers, legal notices): the extractor already ignores the irrelevant and `MAX_BODY` bounds it.

## Out of scope

- A dedicated implications slot with a stronger model (the natural evolution if the extractor's quality is not enough).
- Desktop panel: the checklist is already shown; nothing changes.
- Card kinds, consolidation and reminders: no changes.
