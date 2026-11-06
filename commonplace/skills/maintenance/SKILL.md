---
name: maintenance
description: Maintain the Commonplace memory store — curate entries, prune stale items, and refresh the handover card.
---

# Commonplace maintenance

Commonplace is the user's curated, plain-file memory. Curation is a judgment
act, not a scrape: this skill is how you keep the store worth recalling from.

## The store

- `RULES.md` — standing rules, verbatim. Never rewrite the user's wording;
  only the user edits this file.
- `entries/` — one Markdown file per curated entry. Kinds: `fact` (durable
  truths), `decision` (choices and their rationale), `open` (unfinished
  commitments), `profile` (who the user is).
- `journal/` — automatic turn log. Raw material, not curated memory.
- `archive/` — pre-compression checkpoints. Never delete or edit these.
- `HANDOVER.md` — generated card: rules + open items + latest decisions.

## When to curate

- At the end of a work session, or when the user says "remember this",
  "wrap up", or asks what you remember.
- When recall starts surfacing stale or contradictory entries.

## How

1. Read the recent journal for the active project/session.
2. Promote only what will matter in a future session: decisions with their
   reasons, facts that were expensive to learn, open items with an owner and
   a next step. Use `commonplace_remember` (action `add`).
3. When a newer entry supersedes an older one, `replace` or `remove` the old
   one — a store that never forgets trivia stops being trusted. Decisions
   that were reversed get replaced, with the reversal stated in the body.
4. Close `open` entries that resolved; record the outcome as a `decision`.
5. Call `commonplace_handover` and show the user the card when wrapping up.

## Rules of thumb

- One idea per entry; title says what it is, body says why it matters.
- Quote the user's own words for decisions and rules — never paraphrase a
  commitment into something softer.
- If you are unsure whether something is durable, leave it in the journal.
  The journal forgets nothing; the store should only hold what earned it.
