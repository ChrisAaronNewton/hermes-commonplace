"""CLI for Commonplace: `hermes commonplace <status|handover|recall>`.

Reads the plain-file store directly, so it works without a running agent.
Only appears when commonplace is the active memory provider.
"""

from __future__ import annotations

import os
from pathlib import Path


def _store_root() -> Path:
    home = os.environ.get("HERMES_HOME")
    if not home:
        try:
            from hermes_constants import get_hermes_home

            home = str(get_hermes_home())
        except Exception:
            home = str(Path("~/.hermes").expanduser())
    return Path(home) / "commonplace"


def _cmd(args) -> None:
    root = _store_root()
    sub = getattr(args, "commonplace_command", None)
    if sub == "status":
        entries = list((root / "entries").glob("*.md")) if (root / "entries").is_dir() else []
        archives = list((root / "archive").glob("*.md")) if (root / "archive").is_dir() else []
        journals = list((root / "journal").glob("*.md")) if (root / "journal").is_dir() else []
        print(f"Commonplace store: {root}")
        print(f"entries: {len(entries)} · journal sessions: {len(journals)} · checkpoints: {len(archives)}")
    elif sub == "handover":
        card = root / "HANDOVER.md"
        print(card.read_text(encoding="utf-8") if card.exists() else "No handover card yet.")
    elif sub == "recall":
        query_terms = {t for t in args.query.lower().split() if len(t) > 1}
        hits = []
        entries_dir = root / "entries"
        if entries_dir.is_dir():
            for path in sorted(entries_dir.glob("*.md")):
                text = path.read_text(encoding="utf-8")
                score = sum(text.lower().count(term) for term in query_terms)
                if score:
                    hits.append((score, path.name, text))
        for _, name, text in sorted(hits, reverse=True)[:5]:
            print(f"--- {name}\n{text}")
        if not hits:
            print("No matches.")
    else:
        print("Usage: hermes commonplace <status|handover|recall <query>>")


def register_cli(subparser) -> None:
    subs = subparser.add_subparsers(dest="commonplace_command")
    subs.add_parser("status", help="Show Commonplace store statistics")
    subs.add_parser("handover", help="Print the handover card")
    recall = subs.add_parser("recall", help="Search the curated store")
    recall.add_argument("query", help="Keywords to search for")
    subparser.set_defaults(func=_cmd)
