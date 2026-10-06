# Investing Hub — Roadmap

**Status:** foundation commands are implemented: database init, migration, backup and restore
checks; claude.ai export import; IC pack pull, docs and show; document list, show and revise;
custodian import and list; and session-open checks. The scheduled scoring pass, notifier, session launcher
remain planned. Direct IC writes (`hub ic` verbs, the `ic_writes` log and revert) are implemented. This file is public-safe: no portfolio data, account
numbers, or personal specifics belong here.

## What it is

The advisor and attention layer that pairs with
[Investing Companion](https://github.com/smithadifd/investing_companion) (IC).

- **IC** is the system of record for live market state: watchlists, alerts, triggers, trades,
  lessons and execution receipts. It exposes a context pack (read) and an advisor-action
  vocabulary (write).
- **The hub** handles judgment. It owns the operator's theses, principles and profile, works out
  what new information means for *this* book, and decides when something is worth the
  operator's attention. When it is, the hub asks whether to discuss it and gets a session
  ready if the answer is yes.

They serve each other and each runs fine alone. The hub replaces a claude.ai project that did the
advisor role from January to September 2026. It graduates IC's `docs/advisor-starter-kit/` from a
fill-in-the-blanks kit into a working, cloneable companion.

## Principles

1. **Tightly integrated, loosely coupled.** The hub reads IC through its API and the room session
   writes to it directly with an `advisor:write` token (`hub ic ...` verbs). No handoff step sits
   between the decision and the change (settled 2026-10-06). Every write is logged in `ic_writes`
   and posted to IC as a receipt, and can be reverted where IC has an endpoint to undo it.
2. **Public template, private instance.** The repo is only machinery. Personal data lives in local
   SQLite and gitignored local files and is never committed.
3. **Each repo writes only its own files; any repo may read the others.** This is the same
   flywheel rule week-ahead and mv-analyst already follow.
4. **Deterministic scoring before generation.** The sweep's scoring stage has no model in it, so
   every ping can say why it fired and no level can be hallucinated.
5. **The hub never executes trades.** `trade log` records a trade the operator already made. It and
   any alert deactivation or removal need `--yes`, passed only after the operator confirms in chat.
   Everything else applies directly.
6. **Provenance is required.** Every level cites a named series and an as-of date. Every document
   revision records what triggered it.
7. **The operator's attention is the scarce resource.** The sweep is tuned for precision, and the
   Yes/Skip ratio on asks is the health metric.

## Architecture

```text
                 ┌────────────── sources (adapters) ──────────────┐
                 │ producer digests · market data · news · email   │
                 └───────────────────────┬────────────────────────┘
                                         ▼
 IC ──context pack──▶  SWEEP  stage 0: deterministic score (no model)
 ▲                        │   stage 1: model drafts a brief, only above threshold
 │                        ▼
 │                 findings ──▶ NOTIFIER adapter ──▶ ask (Yes / Later / Skip)
 │                                                   │ Yes
 │                                                   ▼
 │                                   SEAT LAUNCHER: cmux workspace, Claude Code in
 │                                   remote-control mode, seeded with the brief
 │                                                   ▼
 │   receipts          direct writes        ADVISOR SESSION (interactive)
 └── IC API ◀──────── (`hub ic` verbs) ◀──── doc revisions · decisions · ic_writes
                                                     │
                                                     ▼
                                          SQLite knowledge store (local)
```

| Component | Role |
|---|---|
| Advisor seat | Interactive Claude Code sessions in this repo, local or reached from a phone through remote control |
| Knowledge store | Local SQLite: versioned documents plus the hub's own state (see Data model) |
| IC adapter | Reads the pack from `GET /api/v1/export/context-pack` with a read-only token. Writes go straight to IC through `hub ic` verbs with an `advisor:write` token, each logged in `ic_writes` |
| Source adapters | Producers (reference instance: week-ahead briefs, mv-analyst index and analyses, feed-condenser, a newsletter triage queue) and market data (Massive) |
| Sweep | Scheduled on the always-on Mac. Stage 0 scores, stage 1 drafts |
| Notifier adapter | Reference: herald asks (Discord buttons, default-if-unanswered, deadlines). Template fallback: webhook or ntfy |
| Seat launcher | Turns a Yes into a ready session and sends a push notification with the handle |

## Data model (sketch)

| Table | Holds |
|---|---|
| `documents`, `document_revisions` | Theses, principles, watchlist rationale, profile, off-book assets. Each revision records `source_kind` and `source_ref` (receipt, digest, session, import) |
| `custodian_snapshots` | Raw positions and transactions imported from brokerage exports, kept as reconciliation evidence |
| `findings` | Sweep output: kind, subject, score, evidence (JSON), status |
| `asks` | Pitched asks: options, default, deadline, answer, answered_at |
| `briefs` | Session briefs: pending, accepted, consumed |
| `ic_writes` | Every direct IC write and revert: action, target, request, before and after state, IC id, receipt id, provenance, and the revert that undid it |
| `decisions` | Decision journal: subject, decision, rationale, links |
| `calls` | The operator's own dated calls and how they resolved. Guests' calls stay in mv-analyst and are referenced, not copied |
| `beats_proposals` | Proposed edits to the producers' shared interests file |

The `handoffs` table from migration 0001 is retired: existing rows are kept (migration 0007 changes
nothing) but no command reads or writes it.

Live portfolio positions belong in IC's trade log, not here. The hub keeps only what IC doesn't
model, such as off-book assets and account metadata.

## Interaction flows

- **A. Session open.** A SessionStart hook pulls the pack, runs freshness and completeness checks
  and a contract-version check, then lists pending briefs, recent IC writes (last 24h) and stale documents. It also prints
  a `now:` line (local weekday, date, time and zone) so the session can state the time first.
- **B. Sweep → ask → session.** The sweep scores, a finding crosses the threshold, a brief is
  drafted and the ask is pitched. On Yes, the seat launcher opens a workspace. If the operator is
  away, the brief waits.
- **C. Direct IC write.** The session runs a `hub ic` verb (`alert add`, `watchlist update-item`,
  `trade log` and so on). The hub resolves names, reads the before-state, applies the change,
  reads the after-state, logs an `ic_writes` row and posts an IC receipt (a receipt failure never
  undoes the write). `hub ic writes` lists them and `hub ic revert ID` undoes one while IC still
  matches what the hub left. `trade log` and any alert deactivation or removal need `--yes` after
  the operator confirms in chat. The next pack shows the receipt, and the session proposes any
  document revisions that follow.
- **D. Keeping docs current (write-back policy).** Revisions follow the three-tier write-back policy:
  facts (receipts, imported trades, pack-derived state) apply directly with provenance via the
  shared kit's `changes` log; interpretations (producer analyses) are drafted as proposed revisions
  or briefs settled in session; levels and sizes (rungs, ladders, earmarks) are always proposed.
  IC writes follow flow C's confirmation policy. Stale documents become sweep findings.
- **E. Interests.** The hub notices shifts in what the operator cares about (from what gets
  discussed and which asks get a Yes or Skip) and writes `beats_proposals`. It never edits the
  beats file directly.

## Phases

### P0 — Foundation and parity
- Generic `CLAUDE.md` in the repo; the operator's persona and preferences in `CLAUDE.local.md`.
- SQLite schema, plus a nightly `sqlite3 .backup` into `backups/` (7-day retention) that Time
  Machine picks up. A plain file copy can capture a SQLite DB mid-write.
- Import the claude.ai export (`import/`) into `documents` as revision 1, with
  `source_kind = import`.
- IC read adapter (read-only token) and Massive MCP. Retire the Google Drive retrieval.
- Session-open hook (flow A).
- **Exit:** an interactive session does everything the old claude.ai project did, without Drive,
  and a **backup restore has been tested**. After the 2026-09-11 local-data loss, the restore test
  is required. Document revisions written from sessions are P1 (flow D), so "everything the old
  project did" in the P0 exit is read against that placement.

### P1 — Reconciliation sweep (one time)
- Import custodian positions and transaction CSVs. PDF statements are the fallback, for example
  for a managed account.
- Reconcile the documents against the custodians. Resolve known contradictions: household totals,
  undocumented pending assets, documented errors in the holdings and employer-equity docs.
- Session document-revision write path: `hub doc revise` writing a revision with
  `source_kind = session` through `store.insert_document_revision` (body from a file or stdin,
  `source_ref` = the session handle), plus a read path `hub doc show <slug> [--revision N]` /
  `hub doc list`.
- Backfill IC's trade log with `hub ic trade log --yes`, one confirmed trade at a time. This is IC's top "walk" item.
- Verify every carried level against bars and attach provenance.
- Re-baseline IC's state: current deployment host, status of #262/#263/#265, open bugs.
- **Exit:** the documents, IC and the custodians agree, and every level has a named source.

### P2 — Producer integration and conversational surfaces
- **Producer adapters:** The hub reads producer outputs (first adapter: `mv-analyst` episode
  analyses, calls, attention; next: `week-ahead` briefs and ledger calls/beats; feed-condenser
  triage queue).
- **No duplicate pings or alerts:** The hub never re-announces a producer's new artifact (producers
  already post their own embeds) and never duplicates IC's price/watchlist alerts. Asks and
  surfaced items key strictly on meaning for the operator's book.
- **Daily pulse, low interruption:** A "Your book" section in the 07:45 morning brief, sized to the
  day (one line on quiet days, more when things moved, plus a "worth discussing" line). Written
  into the hub's own instance (`out/book/<date>.md`) so the morning brief job reads it if present
  and fresh without cross-repo writes.
- **Midweek letter:** A short midweek letter as living-desk's replacement (carrying forward
  living-desk's merit gate, rotation and 52-week scans, and deterministic visual into the hub).
- **Book-context export:** Producers can read a non-sensitive export from the hub (themes and
  exposure direction, no positions or values) to sharpen coverage. Interpreting what it means for
  the book stays with the hub.
- `beats_proposals` go live (flow E).

### P3 — Sweep in shadow mode
- **Deterministic stage 0 evaluation:** Every trigger is computable in stage 0 without a model
  (thesis-linked series moves, rung predicates, dated call resolutions bearing on the book, beats
  "would make it lead" conditions). Stage 1 drafts brief lines or ask prose only when a stage 0
  finding crosses threshold.
- **Shadow logging and tuning:** Findings are logged and nothing is sent. A weekly review of the
  would-have-pinged list tunes stage 0 thresholds against the high ask bar (separating items suited
  for the morning brief or midweek letter from true Herald asks).
- **Pace adaptation modeling:** Test the Yes/Skip throttling model and verify pause-and-resume
  behavior during quiet stretches or away periods (quiet stretches pause rather than ratchet down
  to silence).
- **Exit:** the operator would have tapped Yes on most of what the shadow log flagged as asks, and
  stage 0 triggers reliably filter noise.

### P4 — Asks go live and conversational layer
- **Purpose — "draw the operator in":** The hub starts conversations and keeps the daily pulse
  moving so the operator does not have to initiate every touchpoint. Two rooms, two temperaments:
  kitchen-table draws out (asks-heavy); the hub processes and surfaces (read-heavy, occasional
  conversation).
- **High-bar asks (Herald):** Herald asks are reserved strictly for:
  1. Thesis or position invalidation risk;
  2. A rung trigger firing or near;
  3. A dated call resolving that bears on the book;
  4. A beats "would make it lead" condition met.
  All other items route to the morning brief or midweek letter.
- **Ask notification payload:** Discord names the topic and why it matters; figures and numbers
  wait for the interactive conversation.
- **Shared kit components:** The bell (notification delivery) and seat launcher (cmux workspace plus
  Claude Code in remote-control mode with push notification) come from the shared kit. Herald
  ask-minting follows herald governance.
- **Adaptive pace with pause-and-resume:** Pace is throttled by the running Yes/Skip ratio, but
  quiet stretches and operator away periods pause the cadence rather than ratcheting down to silence.
- The Yes/Skip ratio stays visible as the running health metric.

### P5 — Absorb `~/code/investing`
- Move the MacroVoices fetch and related jobs in under live-automation retrofit rules. Plist paths
  are pinned, so each job moves supervised, one at a time.
- Retire the Cowork producer path that was lost in the account migration.

### Later
- Schwab API adapter to replace CSV imports.
- Template polish: adapter docs, setup guide, demo data, onboarding interview (carried over from
  the starter kit's `ONBOARDING.md`).

## Incubating (from advisor sessions)

Ideas raised by real sessions and not yet scheduled into a phase. Each one names the failure it
came from, a proposed shape, and the questions to sharpen before it becomes work. Ordered by how
directly it would have changed the session that raised it.

### I1. Ladders as data: every earmark carries a size (raised 2026-10-02)
- **Failure:** dry powder sat idle for months. The earmarked uses had trigger prices but no dollar
  or share sizes, so a printed level could never fire mechanically and every rung became a fresh
  judgment call. Two documents also carried contradictory share counts against the same
  percent-of-account anchor, which went unnoticed for weeks.
- **Shape:** a `ladders` / `rungs` table (subject, account, trigger series and level, size in
  shares or percent, confirmation rule, expiry, status). Session-open warns on any earmark with
  no size and on any cash balance not covered by sized rungs ("unclaimed cash"). A lint step
  recomputes shares from the percent anchor at the current price and flags drift between documents.
- **Phase fit:** P1. It is a prerequisite for stage 0 scoring, which needs sized rungs to say
  what a finding is worth.
- **Sharpen:** does the hub own rungs, or are they an IC concept (entry_zones) that the hub only
  annotates with sizes? Percent of account, or fixed shares re-derived at fire time? Rungs are
  implementation intentions (if trigger + confirmation predicate, then size), the same primitive as
  kitchen-table's commitments (if cue date, then action). Design the `rungs` schema so cue type
  (`date | predicate`), status, slip/expiry and follow-up are shared fields, with the predicate
  evaluated by stage 0.

### I2. Confirmation rules as deterministic predicates (raised 2026-10-02)
- **Failure:** a rung's rule was "wait for basing" with no definition. Twice, price spent weeks
  under the rung and nothing happened, because the condition was a judgment under time pressure,
  which is exactly what pre-committed rungs are meant to remove.
- **Shape:** a small predicate vocabulary evaluated on daily bars in stage 0, for example
  `no_new_low(n_sessions, floor)`, `closes_above(level, n)`, `reclaim(level)`,
  `higher_low(lookback)`. Each rung names its predicate. The sweep reports progress ("basing
  2 of 3") and fires a finding when the predicate completes. No model is involved.
- **Phase fit:** P3 (shadow), with the vocabulary designed in P1 alongside I1.
- **Sharpen:** what is the minimal vocabulary that covers the existing rules? Should a predicate
  reset be a finding in its own right ("the floor broke, the next rung is now live")? Shares the
  if-then implementation intention primitive with I1 and kitchen-table: confirmation predicates
  complete the trigger condition (`if trigger + confirmation predicate, then size`), with predicate
  evaluation performed deterministically in stage 0. Shared schema fields: cue type (`date | predicate`),
  status, slip/expiry, and follow-up.

### I3. Zone selectivity check before a level is trusted (raised 2026-10-02)
- **Failure:** an entry zone looked like a pullback level, but bars showed it had traded through
  in four separate months: the middle of a range, not a stretch. An alternative "N% drawdown from
  the high" criterion had silently been met for months because the high had moved.
- **Shape:** for every zone, compute from 52 weeks of bars the share of sessions that traded
  inside or through it, the months it printed, and where it sits in the trailing range. Then
  recompute any drawdown criterion against the current 52-week high. Session-open flags low-selectivity
  zones. This extends the existing standing rule ("confirm the level has not already printed")
  from a habit into a check.
- **Phase fit:** P1 (one-time audit) and then stage 0 (recurring).
- **Sharpen:** what selectivity threshold separates a real pullback zone from a mid-range level?
  Should the check apply to tripwires and invalidation levels too?

### I4. Earnings and event calendar with confidence (raised 2026-10-02)
- **Failure:** earnings dates came from web summaries, some estimated or not company-confirmed,
  and the proximity-window rule ("use the earlier date") had to be applied by hand per name. Resting
  orders need an expiry tied to the window.
- **Shape:** an `events` table (subject, date, kind, confidence `confirmed | estimated`, source,
  as-of). Session-open lists windows opening in the next 10 days, and resting rungs (I1) inherit an
  expiry from the next window. IC's event seeding is the upstream source where it exists.
- **Phase fit:** P1/P2.
- **Sharpen:** hub-owned, or pushed to IC with `hub ic event add` only once confirmed?

### I5. Handoff drafts in the store, not prose (raised 2026-10-02, superseded 2026-10-06)
- **Failure:** proposed IC edits were written as a table inside a knowledge document, so
  session-open reported "unapplied handoffs: 0" while about nine proposals were actually pending.
- **Resolution:** there are no handoffs to draft. The room applies the change itself with a
  `hub ic` verb, and session-open reports "recent IC writes (last 24h)" from the `ic_writes` log.

### I6. Task export to the operator's task manager (raised 2026-10-02)
- **Failure:** decisions ended a session as document prose. The operator asked for them as dated
  tasks in their own task manager (the reference instance uses Notion), which today is a manual,
  per-session step through a connector.
- **Shape:** a `tasks` adapter beside the notifier adapter. Decisions with an action and a date
  (I1 rungs, I4 windows, follow-up reviews) export as tasks with a stable external id. Reading
  back completion closes the loop: a task marked done prompts a `hub ic trade log` entry or a document
  revision at the next session-open. Notion is the reference adapter; a plain-file adapter is the
  template default.
- **Phase fit:** P2 for export, and alongside flow D for read-back.
- **Sharpen:** is the task manager a source of truth for "did I do it", or only a reminder layer
  with custodian exports as the truth? Should tasks also come from the decision journal?

### I7. Producers discoverable from session-open (raised 2026-10-02)
- **Failure:** a session went to the web for podcast analysis while the producer's own corpus
  (transcripts, per-episode analyses, theme indexes) sat on the same machine. The session didn't
  know it existed.
- **Shape:** pull a sliver of P2 forward. Session-open prints one line per configured producer
  (name, path, newest artifact and its date), from a `producers:` block in `config.yaml`. The
  advisor reads producers before any external lookup. P2 proper (structured reads, book-context
  export) is unchanged.
- **Phase fit:** P0.5 / P1. It's a few lines in session-open plus config.
- **Sharpen:** which producers get a line by default? Should a dated call in a producer's ledger
  that resolves this week (an FOMC, a refunding) surface as a session-open item?

### I8. Gate inputs from the pack, not the web (raised 2026-10-02)
- **Failure:** a sizing gate depended on a futures series that the market-data plan doesn't
  carry, and the IC token wasn't in the session, so the gate was checked from web summaries.
- **Shape:** session-open resolves every gate's input series from the cached pack, with as-of
  times, and warns when a gate input is older than a day or only web-sourced. Covered by the
  existing IC read path. The gap is listing gate series explicitly (I1's trigger-series field).
- **Phase fit:** P1, with I1.

### I9. Direct IC writes from the session, replacing handoff blocks (raised 2026-10-06, shipped 2026-10-06)
- **Failure:** every IC change went through a handoff block the operator pasted to IC's executor by
  hand. Approval already happened in the session, so the block was a copy step: proposals from 10/2
  sat unapplied for four days.
- **Shipped:** hub #32 with IC #381/#383. The session writes with `hub ic` verbs and an
  `advisor:write` token; every write is logged in `ic_writes` (pending, then applied, failed or
  unknown) with an IC receipt, and `hub ic revert` undoes one. `--yes` (after a yes in chat) gates
  trades, alert removal and deactivation, and every level change (thresholds, target prices, entry
  zones). Flow C and I5's draft queue are retired.
- **Open:**
  - IC's executor used to verify quotes before applying a change (its rule 2). A direct write skips
    that; decide whether `hub ic` should check a level against the latest quote before sending.
  - The write token sits in the instance's `.env.local`, which the session launcher resolves. Keep
    it out of unattended jobs (the planned sweep and pulse should hold a `pack:read` token only).
  - Whether a batch of related level changes can share one confirmation instead of one per write.

## IC-side changes (separate track, in `investing_companion`)

- **The advisor starter kit graduates.** `docs/advisor-starter-kit/` moves to this repo as its
  onboarding layer. IC keeps a README pointer to the hub.
- **IC becomes the only owner of the contract docs** (`docs/api/handoff-schema.md`,
  `docs/api/advisor-actions.md`). The hub reads them from IC at the deployed version instead of
  keeping copies, which ends the recurring version drift.
- **API token scopes** for the hub: `pack:read` for the pack, and `advisor:write` (IC #381) for
  exactly the routes the advisor-action vocabulary maps to, plus the reads that resolve a name.
- **Trade-log backfill** (P1).
- Hub dependencies already on IC's list: deploy #262, land #265 (macro seeding), and add an
  "unavailable" mode.

## Open questions

- **Beats ownership.** Leading option: beats stays with the producers as the interests file, the
  hub adds the personal meaning and writes only proposals.
- **Repo name.** `investing-hub` follows the local convention. `investing_hub` would pair with
  `investing_companion`, and IC receipts already use `source: "investing_hub"`.
- **How `~/code/investing` merges** (P5 details).
- **Doc-revision acceptance / write-back policy (settled 2026-10-03).** Replaced by Andrew's
  three-tier policy:
  1. *Facts that must stay current* (receipts, imported trades, pack-derived state) apply directly with provenance; the shared kit's `changes` log is the mechanism.
  2. *Interpretation* (what a producer analysis means for a thesis) is drafted as a proposed revision or a brief and settled in a session.
  3. *Anything that sets a level or a size* (rungs, ladders, earmarks) is always proposed, never applied silently.
  IC writes are direct (settled 2026-10-06) under flow C's confirmation policy.
- **Sweep cadence.** Pre-market daily plus post-close? Event-driven when a digest lands?
- **Where IC serves contract docs from:** an API endpoint, or raw files at the deployed commit.
- **Retention** for findings and asks.
- **Incubating items I1–I8** each carry their own sharpen questions (see above). I1 and I2 share
  the implementation intention primitive schema (`rungs` with cue type, status, slip/expiry, follow-up).

## Source material

`import/` (gitignored) holds the claude.ai export: project instructions, project memory, the 10
knowledge docs and the conversation index. The full conversations live in the complete export
outside this repo.
