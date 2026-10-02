# Investing Hub — Roadmap

**Status:** foundation commands are implemented: database init, migration, backup and restore
checks; claude.ai export import; IC pack pull, docs and show; document list, show and revise;
custodian import and list; and session-open checks. The scheduled scoring pass, notifier, session launcher and
handoff approval loop remain planned. This file is public-safe: no portfolio data, account
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

1. **Tightly integrated, loosely coupled.** The hub reads IC through its API. It writes to IC only
   through handoff blocks, which IC's executor applies after the operator approves them.
2. **Public template, private instance.** The repo is only machinery. Personal data lives in local
   SQLite and gitignored local files and is never committed.
3. **Each repo writes only its own files; any repo may read the others.** This is the same
   flywheel rule week-ahead and mv-analyst already follow.
4. **Deterministic scoring before generation.** The sweep's scoring stage has no model in it, so
   every ping can say why it fired and no level can be hallucinated.
5. **The hub never executes trades.** Destructive IC actions stay approval-gated, as in IC's own
   contract.
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
 │   receipts          handoff block        ADVISOR SESSION (interactive)
 └── executor ◀──── (operator approves) ◀── doc revisions · decisions · handoffs
                                                     │
                                                     ▼
                                          SQLite knowledge store (local)
```

| Component | Role |
|---|---|
| Advisor seat | Interactive Claude Code sessions in this repo, local or reached from a phone through remote control |
| Knowledge store | Local SQLite: versioned documents plus the hub's own state (see Data model) |
| IC adapter | Reads the pack from `GET /api/v1/export/context-pack` with a read-only token. Writes are handoff files only |
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
| `handoffs` | Handoff blocks: draft → approved → applied, linked to the IC receipt |
| `decisions` | Decision journal: subject, decision, rationale, links |
| `calls` | The operator's own dated calls and how they resolved. Guests' calls stay in mv-analyst and are referenced, not copied |
| `beats_proposals` | Proposed edits to the producers' shared interests file |

Live portfolio positions belong in IC's trade log, not here. The hub keeps only what IC doesn't
model, such as off-book assets and account metadata.

## Interaction flows

- **A. Session open.** A SessionStart hook pulls the pack, runs freshness and completeness checks
  and a contract-version check, then lists pending briefs, unapplied handoffs and stale documents.
- **B. Sweep → ask → session.** The sweep scores, a finding crosses the threshold, a brief is
  drafted and the ask is pitched. On Yes, the seat launcher opens a workspace. If the operator is
  away, the brief waits.
- **C. Handoff.** A session writes a block, the operator approves it, and the IC executor applies it
  and posts a receipt. The next pack shows the receipt, the hub marks the handoff applied, and
  proposes any document revisions that follow from it.
- **D. Keeping docs current (P1).** Receipts, imported trades, new digests and finished sessions
  produce *proposed* revisions, which get accepted in a session. A stale document becomes a sweep
  finding.
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
- Backfill IC's trade log through approval-gated `LOG_TRADE` handoffs. This is IC's top "walk" item.
- Verify every carried level against bars and attach provenance.
- Re-baseline IC's state: current deployment host, status of #262/#263/#265, open bugs.
- **Exit:** the documents, IC and the custodians agree, and every level has a named source.

### P2 — Producer integration
- The hub reads producer outputs (week-ahead briefs, mv-analyst analyses and index).
- Producers can read a non-sensitive **book-context export** from the hub (themes and exposure
  direction, no positions or values) to sharpen coverage. Interpreting what it means for the book
  stays with the hub.
- `beats_proposals` go live (flow E).

### P3 — Sweep in shadow mode
- Findings are logged and nothing is sent.
- A weekly review of the would-have-pinged list tunes the stage 0 thresholds.
- **Exit:** the operator would have tapped Yes on most of what the shadow log flagged.

### P4 — Asks go live
- herald ask-minting brief. This is an authorization change under herald's own governance.
- Seat launcher (cmux plus remote control) and push notification.
- The Yes/Skip ratio stays visible as the running health metric.

### P5 — Absorb `~/code/investing`
- Move the MacroVoices fetch and related jobs in under live-automation retrofit rules. Plist paths
  are pinned, so each job moves supervised, one at a time.
- Retire the Cowork producer path that was lost in the account migration.

### Later
- Handoff approval as a herald ask ("Apply block N?").
- Schwab API adapter to replace CSV imports.
- Template polish: adapter docs, setup guide, demo data, onboarding interview (carried over from
  the starter kit's `ONBOARDING.md`).

## IC-side changes (separate track, in `investing_companion`)

- **The advisor starter kit graduates.** `docs/advisor-starter-kit/` moves to this repo as its
  onboarding layer. IC keeps a README pointer to the hub.
- **IC becomes the only owner of the contract docs** (`docs/api/handoff-schema.md`,
  `docs/api/advisor-actions.md`). The hub reads them from IC at the deployed version instead of
  keeping copies, which ends the recurring version drift.
- **Read-only API token scope** for the hub: pack reads without write access.
- **Trade-log backfill** (P1).
- Hub dependencies already on IC's list: deploy #262, land #265 (macro seeding), and add an
  "unavailable" mode.

## Open questions

- **Beats ownership.** Leading option: beats stays with the producers as the interests file, the
  hub adds the personal meaning and writes only proposals.
- **Repo name.** `investing-hub` follows the local convention. `investing_hub` would pair with
  `investing_companion`, and IC receipts already use `source: "investing_hub"`.
- **How `~/code/investing` merges** (P5 details).
- **Doc-revision acceptance.** Auto-apply trivial revisions (a receipt that confirms an alert
  re-level), or always propose?
- **Sweep cadence.** Pre-market daily plus post-close? Event-driven when a digest lands?
- **Where IC serves contract docs from:** an API endpoint, or raw files at the deployed commit.
- **Retention** for findings and asks.

## Source material

`import/` (gitignored) holds the claude.ai export: project instructions, project memory, the 10
knowledge docs and the conversation index. The full conversations live in the complete export
outside this repo.
