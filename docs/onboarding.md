# Radar onboarding

## 1. Install and enable

```bash
hermes plugins install Naroh091/hermes-proactive-radar
```

This leaves the plugin in `plugins.enabled`. Radar installs disabled and in shadow mode; `hermes radar setup` turns it on and writes `plugins.entries.hermes-proactive-radar.settings`. Two things are never Radar's to grant itself: the model fallback is a pair of capabilities Hermes asks for when you install or enable the plugin, and the MCP servers Radar may call go into `mcp_allowlist` only when you answer yes to that question in `setup` (never under `--yes`; it prints the line to add instead).

Restart the gateway (`hermes gateway restart`). The scheduler starts when the gateway connects your messaging platform or on its first `on_session_start`.

## 2. Discover and configure

```bash
hermes radar discover
hermes radar setup
```

`setup` asks: language, trust level, whether to read your habits of the last twelve months on the first start, owner, timezone, quiet hours, where to write to you (the platform, the chat and your user id on it), which Gmail/Calendar servers or command-line connectors to watch and the address of each account. It writes `plugins.entries.hermes-proactive-radar.settings`, and then asks, as a question of its own with no as the default, whether Radar may call the MCP servers behind the sources you chose; only a yes you type adds them to `mcp_allowlist`.

### Command-line connectors

`discover` lists, next to the MCP servers, the tools it finds on the machine: `gws` (Google's Workspace CLI; it runs on its own login, `gws auth login`, or on the credentials file you set as `connector.token`; Radar never borrows another tool's token), the google-workspace skill's own `google_api.py` script when that skill is authorised, and `gog`, which needs a mapping. The first two are offered in `setup` like any MCP (`provider: gws` or `hermes_google`, no `server`); they have no push, they poll every `interval_seconds`. For a tool without a profile of its own:

```bash
hermes radar connector derive gog --source-id gog-gmail --address me@example.org --save
hermes radar connector test gog-gmail
```

`derive` reads the help of the command and of its subcommands (two levels), asks the model for the mapping of `search`, `read` (required), `thread`, `reply` and `send` (optional) onto the tool's argv and its JSON keys, and submits it to the conformance test (list recent mail and read one). It is saved only if it passes; afterwards it is never derived again: the mapping lives in `mapping` inside the source. `test` repeats the test on any command-line source.

### Habits (warmup)

On the gateway's first cycle after onboarding, Radar reads the last `warmup.months` (12) of mail and calendar, without calling any model, and keeps what it finds as habits: which organisations and addresses you write to and how often; which mails repeat with a cadence (a monthly invoice); how you answered each kind of event and each organizer. It does it again every `warmup.refresh_days` (7) in quiet hours, when you ask with `/radar warmup` (it tells you the result) or with `hermes radar warmup` (the gateway does it on its next cycle); `/radar warmup status` says when it ran and what it found. You can turn it off with `warmup.enabled: false`.

### Trust levels

Every step Radar proposes has a **class** according to what it changes:

| Class | What it is | Examples |
|---|---|---|
| `read` | Changes nothing | search hotels, compare prices, read the code, reproduce a bug |
| `local` | Reversible and private | reply draft in Gmail, branch and draft PR, HTML report, reminder |
| `external` | Irreversible or visible to third parties | send an email, book, pay, deploy, merge, publish |

The level decides which classes run on their own and which ask first (with buttons on Telegram, by a written reply on any other platform):

| Level | `read` | `local` | `external` |
|---|---|---|---|
| `cautious` — only looks | alone | asks | asks |
| `balanced` — looks and prepares (recommended) | alone | alone | asks |
| `autonomous` — looks, prepares and acts | alone | alone | alone, only for the operations listed in `trust.auto_external`; paying, booking, deploying and merging never |

**Example 1.** A customer emails reporting a login bug in `org/app` (configured in `workspaces`).

- `cautious`: you get a notice with what the email says and the button "Shall I investigate?". Only after your yes does it reproduce the bug.
- `balanced`: it reproduces the bug in a bounded session, creates a branch and a **draft** PR, and notifies you with the summary, the link to the PR and a reply draft. To send the reply it asks you.
- `autonomous` with `auto_external: [gmail.reply]`: it also sends the reply to the customer without asking. Merging the PR or deploying always keeps asking.

### What happens when you authorise an external action

The notice brings buttons with the step's options (for instance "Yes, create the CNAME with the proxy off"). When you press one:

1. The proposal is authorised and the message is edited with `Authorised: …` followed by `Running; I'll report the result` or by `Not run yet: <reason>` (another job running, daily quota spent, jobs disabled).
2. If the operation is native (`gmail.reply`, `calendar.respond`, `github.comment`) and its parameters resolve from the task, the plugin executes it itself: it records a single-use consent bound to the operation and its exact parameters, consumes it when acting, acts once and verifies by reading back. In any other case a `hermes chat` job is launched that carries the exact authorisation (step, option, time) and the rule of doing only that action, verifying it by reading the result back and listing what changed.
3. When it finishes, the result returns to the pipeline and you get a notice with what was done and what remains. That notice does not count towards the daily notices quota (`notices_per_day`, 8 by default, in the Desktop form): what answers an authorisation of yours always arrives.

If it could not start, the proposal stays in the `waiting` state (in `/radar status` it shows as "waiting", in `/radar list` with ⏸) and is retried every tick until the reason goes away (for instance, when the daily quota renews). Pressing "No" on the notice cancels it.

### Conversations in progress

What you discuss with the agent in a session is resolved by that session. Radar only learns from it once it has been quiet for half an hour (`observation.session_quiet_minutes`) and reads it whole, not turn by turn: what a question opens the next answer closes, and what the agent took as done is done. A finished conversation leaves no task (and silently closes the one it had opened itself); an open one records only what was left pending and for whom; one deferred with a reminder or a cron is followed on that date and nothing else. If you come back to that session, Radar steps aside: no notices, no parallel jobs, reviews postponed. What began in a chat launches no jobs on its own (`observation.session_auto_jobs`): if something is left pending, it offers it in the summary. Tokens or passwords you paste in the chat are masked before being stored.

### Urgency and summaries

Not everything deserves to interrupt you. The planner judges the urgency of each matter: `now` arrives at once (security, blocked money, a deadline within hours, someone waiting today, a service down); `today` enters the day's next summary; `this_week` and `whenever` wait for the morning summary. The summary slots are set in `notifications.digest_times` (local time; by default 09:00, 14:00 and 19:00). What you already know because you did it (a mail you wrote, what you decided in the chat) is not told to you again.

### Who acts: escalation

Before notifying you, the planner decides whether the assistant can take care of it alone (`handle`: read the notification, look up the message, check the payments; nothing reaches you until there is a result), whether there is only something to tell you (`inform`: a message with the news and an "OK" button, no questions; if the news closes an invoice or a return Radar was following, it offers to mark it with a button) or whether it is really yours to decide (`ask`). Under `cautious` what would be `handle` also asks, because no step runs on its own.

### Reply drafts

When the task comes from a mail thread, the notice carries an "Email" block with the sender of the last message, the date in your timezone, the subject, the number of messages in the thread and an excerpt of the last one (without the quoted text of earlier messages). If you watch several accounts, it says which one.

If Radar prepares a reply (class `local`), it leaves it saved as a draft in your Gmail and shows it to you whole in the notice, with "Send draft" and "Review it first". Only the first button sends, and it sends exactly that text. "Review it first" sends you the draft in a separate message without changing anything; you can answer "yes" to that message to send it or "no" to discard it. If the draft is very long, it also arrives as an attached file.

### Loop brake

Per task, at most `auto_jobs_per_task` automatic jobs run a day (3 by default) with `auto_job_cooldown_minutes` between them (15). Your explicit authorisation is not subject to that limit. The total daily quota is `jobs_per_day` (100 by default). All three are in the plugin's form in Desktop (Capabilities → Plugins → radar → Settings) as "Jobs per day", "Automatic jobs per task" and "Minutes between automatic jobs". A step you dismiss with "No" is not proposed again, nor rephrased, for 24 h; the planner receives the list of what you rejected.

**Example 2.** The confirmation of a flight from the 3rd to the 5th arrives and there is no hotel for those nights.

- `cautious`: notice "lodging missing" and button "shall I look for options?".
- `balanced`: it looks for options alone and sends you three candidates with prices. Booking it asks you.
- `autonomous`: same as `balanced`: booking and paying are never automated.

Tune by task kind with `trust.overrides`, for instance `{work_item: {local: auto}}` so that incidents prepare PRs even when the level is `cautious`, or `{trip: {local: ask}}` so that trips ask before creating reminders. Change the level whenever you want with `/radar trust <level>` in the chat, from Hermes Desktop (Capabilities → Plugins → radar → Settings → *Trust level*) or by editing `trust_level` in the configuration; that flat value takes precedence over `trust.level`.

### Where Radar writes to you

Radar writes on one messaging platform, the one you name in `setup` (`destination.platform`): any platform your gateway is connected to, such as Telegram, Discord, Slack, WhatsApp, Signal, Matrix, Mattermost, Teams, Google Chat, email or SMS. `setup` also asks for the chat and for your user id on that platform.

Verify the destination from that chat: `/radar verify`. It binds Radar to that chat and that user; a message from anyone else, or from another platform, is never read as an answer. (The gateway answers unknown commands itself, which is why verification goes through the host's command registry.)

How you answer depends on the platform:

| Platform | What you get | How you answer |
|---|---|---|
| Telegram | Rich text with buttons, and the task's report as a file | A button, or a written reply to the message |
| Any other gateway platform | The same words as markdown, with the answers on offer named on a line under the message (`↩ Go ahead · Dismiss · Remind me tomorrow`) | A written reply to the message, in your own words |

A written reply is matched to its notice by the message it quotes, so answering needs a platform with replies or threads; on one without them (SMS, IRC) the notices still arrive, and you answer from the Desktop panel or by asking the agent. Native buttons outside Telegram and a Hermes Bot Chat as the destination are not supported yet. To move to another platform, change `destination.platform`, the chat and the user id (or run `setup` again) and send `/radar verify` from the new chat.

## 3. Gmail: polling or push

By default each account is synchronised by **polling**: a `search_emails` search every `interval_seconds` (300 s), without a model, with a cursor by date. It is enough for most and needs nothing outside Hermes.

**Push** (Gmail Pub/Sub) is optional and enabled per account: it delivers in seconds and allows raising `interval_seconds` (polling stays as a safety net if the watch expires or a notification is lost). It requires a public HTTPS endpoint (tunnel) and a GCP project:

1. In GCP: a Pub/Sub topic (e.g. `projects/<id>/topics/gmail`) with the `pubsub.publisher` permission for `gmail-api-push@system.gserviceaccount.com`, and a **push subscription** with OIDC authentication (service account) towards `https://<your-tunnel>/?token=<RADAR_GMAIL_PUSH_TOKEN>`.
2. In `setup`, enable push for each account and give its Pub/Sub topic.
3. `setup` prints the host command that creates the `gmail-push` webhook route (`hermes webhook subscribe gmail-push --deliver log`: no prompt, so **no agent turn**). Save the secret it prints as `RADAR_GMAIL_PUSH_HERMES_SECRET` and a long random string as `RADAR_GMAIL_PUSH_TOKEN` in Radar's settings or your host's secret store; Radar reads both through the host and never opens `.env`.
4. Enable the receiver inside the gateway (`ingress.gmail_receiver.enabled: true`, port 8666 by default) and point the tunnel at that port. Alternative: `hermes radar gmail receiver` in the foreground or as a service.
5. Keep the Gmail watch alive from your mail integration (`users.watch` expires after seven days). Radar only receives the push: it holds no Gmail credentials and does not renew the watch. If the watch lapses, polling carries on.

To go back to polling, `push.enabled: false` on the account is enough; the receiver is turned off with `ingress.gmail_receiver.enabled: false`.

### GitHub

For the work items of a repo, `workspaces: {org/repo: /path/to/checkout}` says where to work: each job runs in an isolated worktree of that checkout (`jobs.worktree: true`), pushes its branch and does not open the PR; Radar opens it as a draft in a separate step, with an origin mark, and never merges.

A `github` source can observe a route of the host's webhook (`route: github-pr`, `consume: false` if something else already uses it, such as a PR gatekeeper), consume a route of its own (`hermes radar github route github-radar` and then `consume: true`; on GitHub, payload URL `<webhook base>/webhooks/github-radar`, secret the route's) and poll repos with `gh` (`repos: [org/repo]`, events API with ETag, first read as a baseline). `gh` must be authenticated for the user running the gateway (`gh auth status`). Events are filtered before the model (see README) and the signals carry repo, type, number, actor, branch and link.

### Calendar

`discover` recognises the Google Calendar MCP and `setup` proposes a `calendar` source (`calendar_id: primary`, every 10 minutes). Each read compares the events of the window (`lookback_hours`, `lookahead_days`) with the stored fingerprint: a new, moved, renamed, cancelled event or one you answered produces a signal; the rest do not. The first read only records the baseline (`initial_import: true` if you want the events that already exist to come in too). Declined events are not followed. For several accounts on the same server, `account` names the MCP's nickname.

### Memory and earlier sessions

Before planning a task, Radar consults two context sources of the host, both optional (`context.memory`, `context.sessions`):

- The **memory** of the active provider (`memory.provider` in `config.yaml`): its own search tool is used, be it hindsight, mnemosyne or another that implements `MemoryProvider` (`context.memory_tool` pins the name if automatic detection misses). The provider is loaded read-only; Radar does not write to memory.
- The **earlier sessions** with the agent, via the host's `session_search` (`context.session_results`).

What is recalled goes to the model as data, bounded by `context.max_chars` and `context.timeout_seconds`; if something fails or takes too long, the task is planned without it.

### Preferences

Answer any notice with what you want changed and Radar stores it as a scoped preference: "don't notify me about this sender any more" (silence for that address), "send me the CI ones in the digest" (digest), "always notify me about this" (interrupt), "silence this whole account". The model interprets the sentence with the notice's context; without a model it is stored as is with the scope of the sender or the task kind. Security alerts, service outages, deadlines and financial losses keep getting through unless you say otherwise.

- `/radar prefs` lists the active ones; `/radar forget <id>` disables one (they stay versioned, never deleted).
- In the chat, the agent can store them with `radar_preference` (explicit effect and scope).
- They apply from the next signal on: triage drops what is silenced before calling the model and attention decides immediate notice or summary.

## 4. Patterns and panel

One hour after starting, and then every 24 h, Radar looks for patterns in the signals (mail, GitHub, chat turns, job results). When it finds one worth automating you get an "Automate" proposal in the summary (it never runs on its own); accepting it creates a host cron that delivers in your chat and whose outputs come back to the radar; dismissing it silences it. `/radar patterns` shows what was detected.

The Hermes Desktop panel loads on its own once the plugin is installed (Capabilities → Plugins → Rescan if it does not show up; the plugin must be in `plugins.enabled` for its backend to mount). It shows the tasks by status as object cards (boarding pass, receipt, PR, calendar leaf, label, dial), with a side drawer for the detail: thread, proposals with the same buttons as Telegram, drafts, report, and below patterns, preferences and the card kinds (the ones the model designs for repeated tasks can be disabled there).

## 5. Check

```bash
hermes radar status
hermes radar sync gmail-work         # force a read
hermes radar signals                 # triage of the latest signals
hermes radar run-once                # one full cycle: sync, pipeline, jobs, notices
hermes radar tasks
hermes radar explain <task_id> --html
hermes radar simulate scenarios/failed-payment.yaml   # a situation through the real pipeline, nothing sent
```

`simulate` is the way to try a situation before it happens: write it as a scenario (see `scenarios/`), run it and read what Radar would judge, hold back and say. It works on a copy of your store, with your facts, habits and preferences, and with the real model; a `replies:` list lets you answer the message it would have sent and see what it learns and does next.

In your chat: every new task with gaps arrives as a notice with two numbered blocks and, on Telegram, buttons (on other platforms the same answers are named under the message and you reply in words). "Confirm for me" are yes/no questions ("Is size 48 correct?"): "Yes, correct" closes the doubt and "No, that's wrong" leaves it marked for you to say what is wrong. "I can do" are steps Radar would do for you: "Go ahead" launches them (or asks your authorisation if they reach third parties) and "Dismiss" withdraws them and they are not proposed again. The buttons at the bottom act on the whole task: "Already resolved" closes it, "Remind me tomorrow" parks it until the first summary of the next day and "Stop notifying about this" silences it for good (it is reactivated from the panel). You can also answer the notice in your own words: "yes", "the 2nd", "tomorrow", "already done" or any nuance; what is not a direct order reaches the agent in the chat with the pending proposals in its context (`radar_resolve`).

## 6. Migrate from mail-intelligence

```bash
hermes radar import mail-intelligence      # active preferences only
systemctl --user disable --now gmail-push.service   # the receiver moves into the gateway
```

Remove `mail-intelligence` from `plugins.enabled`. Its repo stays as an archive; its cases and decisions are not imported.
