# Commonplace — local-first curated memory for Hermes Agent

**The memory that refuses to let your agent forget.**

Commonplace is a memory provider plugin that keeps your agent's long-term
memory in plain Markdown files on your own machine — no cloud, no API keys,
no embeddings service, no third-party Python dependencies.

## What it does

- **Standing rules, verbatim.** `RULES.md` is injected into every session
  exactly as you wrote it. Your words, never paraphrased by a model.
- **A curated store, not a transcript dump.** Facts, decisions, open items
  and profile notes live one-per-file in `entries/`, written by the agent
  through tools (`commonplace_remember` / `commonplace_recall`) and kept
  honest by the bundled `commonplace:maintenance` skill: curation is a
  judgment act, so the raw turn journal stays separate until something
  earns promotion.
- **A fail-closed compression checkpoint.** Commonplace advertises
  pre-compress checkpoint API v2. Before Hermes may lossily compress a
  conversation, the transcript evidence is durably archived to
  `archive/<digest>.md` (content-addressed, idempotent). If the archive
  cannot be written and the operator has enabled
  `compression.checkpoint_required`, compression is **blocked** and the
  uncompressed transcript is kept — the agent never silently forgets.
- **A handover card.** `HANDOVER.md` — rules, open items, latest decisions —
  regenerated at every session end and on demand (`commonplace_handover`),
  so a fresh session starts from a card you can read, not a summary you
  have to trust.
- **Lexical recall.** Per-turn recall is keyword scoring over the curated
  store: instant, offline, and explainable. On a store small enough for a
  human to own, it beats a vector index you can't inspect.

## Install

Copy the `commonplace/` directory into `$HERMES_HOME/plugins/`, then select
it in `config.yaml`:

```yaml
memory:
  provider: commonplace
```

Optionally require checkpoints before any lossy compression:

```yaml
compression:
  checkpoint_required: true
```

Run `hermes memory setup` to tune the two settings (recall budget, handover
size), `hermes plugins doctor` to verify the install, and
`hermes commonplace status|handover|recall <query>` to inspect the store
from the shell.

## Store layout (all inside `$HERMES_HOME/commonplace/`)

| Path | What it is |
| --- | --- |
| `RULES.md` | Your standing rules, verbatim. You edit this; the agent doesn't. |
| `entries/` | Curated entries, one Markdown file each, with kind/title/date frontmatter. |
| `journal/` | Append-only per-session turn journal (automatic). |
| `archive/` | Compression checkpoints, named by transcript digest. |
| `HANDOVER.md` | The generated handover card. |

Everything is human-readable, diff-able, and yours: back it up, grep it,
put it in git, or delete it — the provider rebuilds its index from the
files on every start.

## Disclosures

- **Network:** none. Commonplace makes no network calls of any kind.
- **Data:** reads and writes only inside `$HERMES_HOME/commonplace/` and
  its config sidecar `$HERMES_HOME/commonplace.json`. It stores the
  conversations you have with the agent (journal + checkpoints) in those
  plain files — that is the product. Turn off journaling by not using the
  provider; delete the folder to erase it.
- **Credentials:** none required, none read, none stored.
- **Shell/subprocesses:** none. Pure Python standard library.
- **Telemetry:** none.
- **Core modifications:** none — the provider extends Hermes only through
  the public `MemoryProvider` ABC and `register(ctx)` surfaces.

## Why "Commonplace"?

A commonplace book is the centuries-old practice of keeping a curated
notebook of things worth remembering — quoted exactly, organized by the
keeper, reread often. This is that, for an agent.
