# App-Integration Layer (OPTIONAL)

**Skip this whole folder unless you run the self-hosted Investing Companion app.** The advisor
operating-system (`PROJECT_INSTRUCTIONS.md`) and the portfolio docs (`docs/`) work perfectly
well on their own. This layer is the advanced add-on that connects a *live* data loop to the
advisor.

## What it does

It closes the loop between three roles:

```
advisor ──(handoff block)──▶ executor (Claude Code) ──(API calls)──▶ Investing Companion app
   ▲                                                                          │
   │                                                                          ▼
   └──────── context pack (prices, alerts, triggers, receipts) ◀──── GET /export/context-pack
```

- The **app** exports a *context pack*: live prices, alerts, entry zones, triggers, upcoming
  events, and execution receipts.
- The **advisor** (your hub session) reads the pack each session and, when it wants to
  change something, emits a *handoff block* — a plain-language action list.
- The **executor** (Claude Code, in your app repo) runs those actions against the app's API and
  posts a receipt, which shows up in the next pack. The loop closes itself.

## The doc here

- **`INVESTING_COMPANION.md`** — orientation: session-open discipline and the source-of-truth
  map. *Read-side behavior.* This is the one you most need to personalize to your app's
  watchlists and triggers.

## The contract docs

`GET /api/v1/export/contract-docs` on your Investing Companion instance serves the current read-side (handoff schema) and write-side (advisor actions) contract docs.

## Important

These docs describe a **protocol**, not your portfolio — they hold no personal data. The live
authority on what's supported is always the context pack's `unsupported_features` field, never a
hardcoded list in any doc.

Upload the two contract docs from that endpoint to the advisor verbatim. Personalize `INVESTING_COMPANION.md` with your own watchlists/triggers inventory.
