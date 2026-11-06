"""Commonplace — local-first curated memory for Hermes Agent.

A memory provider that keeps everything in plain files the user owns:

- ``RULES.md`` — standing rules, kept verbatim, injected into every session.
- ``entries/`` — curated facts, decisions and open items (one Markdown file each).
- ``journal/`` — an append-only per-session turn journal.
- ``archive/`` — pre-compression checkpoints. Commonplace advertises checkpoint
  API v2: before Hermes may lossily compress a conversation, the transcript
  evidence is durably archived to disk, and if that write fails while the
  operator requires checkpoints, compression is blocked instead of forgetting.
- ``HANDOVER.md`` — a compact card (rules + open items + latest decisions)
  regenerated at session end and on demand.

No cloud, no API keys, no network, no third-party dependencies — Python
standard library only. Recall is lexical (term overlap over the curated
store), which is the right tool for a store a human can read and edit.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

from agent.memory_provider import (
    PRE_COMPRESS_CHECKPOINT_API_VERSION,
    MemoryProvider,
    RecallStatus,
    is_trivial_prompt,
    spawn_context_thread,
)

logger = logging.getLogger(__name__)

PROVIDER_NAME = "commonplace"
ENTRY_KINDS = ("fact", "decision", "open", "profile")
DEFAULT_RECALL_MAX_CHARS = 4000
DEFAULT_HANDOVER_DECISIONS = 10
RULES_PROMPT_CAP = 6000

_TOKEN_RE = re.compile(r"[a-z0-9]+")
_STOPWORDS = frozenset(
    "a an and are as at be by do does for from has have how i in is it its me my "
    "of on or our so that the this to was we what when where who why will with you your".split()
)

RULES_STARTER = """\
# Standing rules (kept verbatim — Commonplace injects this file into every session)

<!-- Write rules here exactly as you want the agent to read them, one per line or
     in short paragraphs. Edit freely; changes apply from the next session. -->
"""


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _tokens(text: str) -> list[str]:
    return [t for t in _TOKEN_RE.findall((text or "").lower()) if len(t) > 1]


def _slug(text: str, limit: int = 40) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", (text or "").lower()).strip("-")
    return (slug[:limit].rstrip("-") or "entry")


def _atomic_write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.tmp-{os.getpid()}-{threading.get_ident()}")
    tmp.write_text(text, encoding="utf-8")
    os.replace(tmp, path)


def _message_text(message: dict[str, Any]) -> str:
    content = message.get("content")
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for part in content:
            if isinstance(part, dict) and part.get("type") == "text":
                parts.append(str(part.get("text", "")))
            elif isinstance(part, str):
                parts.append(part)
        return "\n".join(p for p in parts if p)
    return ""


class _Entry:
    __slots__ = ("id", "kind", "title", "created", "session", "source", "body", "path")

    def __init__(self, **kw: Any) -> None:
        for field in self.__slots__:
            setattr(self, field, kw.get(field, ""))


def _render_entry(entry: _Entry) -> str:
    return (
        "---\n"
        f"id: {entry.id}\n"
        f"kind: {entry.kind}\n"
        f"title: {entry.title}\n"
        f"created: {entry.created}\n"
        f"session: {entry.session}\n"
        f"source: {entry.source}\n"
        "---\n\n"
        f"{entry.body.strip()}\n"
    )


def _parse_entry(path: Path) -> Optional[_Entry]:
    try:
        raw = path.read_text(encoding="utf-8")
    except OSError:
        return None
    if not raw.startswith("---"):
        return None
    head, _, body = raw[3:].partition("\n---")
    meta: dict[str, str] = {}
    for line in head.splitlines():
        key, _, value = line.partition(":")
        if value:
            meta[key.strip()] = value.strip()
    if "id" not in meta:
        return None
    return _Entry(
        id=meta["id"],
        kind=meta.get("kind", "fact"),
        title=meta.get("title", ""),
        created=meta.get("created", ""),
        session=meta.get("session", ""),
        source=meta.get("source", ""),
        body=body.strip(),
        path=str(path),
    )


class CommonplaceProvider(MemoryProvider):
    """Curated, plain-file memory with a fail-closed compression checkpoint."""

    pre_compress_checkpoint_api_version = PRE_COMPRESS_CHECKPOINT_API_VERSION

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._home: Optional[Path] = None
        self._root: Optional[Path] = None
        self._session_id = ""
        self._agent_context = "primary"
        self._rules_snapshot = ""
        self._entries: dict[str, _Entry] = {}
        self._recall_max_chars = DEFAULT_RECALL_MAX_CHARS
        self._handover_decisions = DEFAULT_HANDOVER_DECISIONS
        self._prefetch_cache: dict[str, tuple[str, int]] = {}
        self._last_recall: Optional[RecallStatus] = None
        self._sync_thread: Optional[threading.Thread] = None
        self._last_checkpoint = ""

    # -- identity / availability -------------------------------------------

    @property
    def name(self) -> str:
        return PROVIDER_NAME

    def is_available(self) -> bool:
        # No credentials, no network, no dependencies: always available.
        return True

    # -- lifecycle ----------------------------------------------------------

    def initialize(self, session_id: str, **kwargs: Any) -> None:
        hermes_home = kwargs.get("hermes_home")
        if not hermes_home:
            raise ValueError("Commonplace requires the hermes_home kwarg")
        self._home = Path(hermes_home)
        self._root = self._home / PROVIDER_NAME
        self._session_id = session_id
        self._agent_context = str(kwargs.get("agent_context") or "primary")
        for sub in ("entries", "journal", "archive"):
            (self._root / sub).mkdir(parents=True, exist_ok=True)
        self._load_config()
        self._load_rules()
        self._reload_entries()

    def shutdown(self) -> None:
        if self._sync_thread and self._sync_thread.is_alive():
            self._sync_thread.join(timeout=5.0)

    def on_session_switch(
        self, new_session_id: str, *, parent_session_id: str = "", reset: bool = False,
        rewound: bool = False, **kwargs: Any,
    ) -> None:
        self._session_id = new_session_id
        self._prefetch_cache.clear()

    # -- config -------------------------------------------------------------

    def _config_path(self) -> Path:
        assert self._home is not None
        return self._home / f"{PROVIDER_NAME}.json"

    def _load_config(self) -> None:
        try:
            data = json.loads(self._config_path().read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return
        if isinstance(data, dict):
            self._recall_max_chars = int(data.get("recall_max_chars", self._recall_max_chars))
            self._handover_decisions = int(data.get("handover_decisions", self._handover_decisions))

    def get_config_schema(self) -> list[dict[str, Any]]:
        return [
            {
                "key": "recall_max_chars",
                "description": "Maximum characters of recalled memory injected per turn",
                "type": "integer",
                "default": DEFAULT_RECALL_MAX_CHARS,
                "minimum": 500,
                "maximum": 20000,
            },
            {
                "key": "handover_decisions",
                "description": "How many recent decisions the handover card lists",
                "type": "integer",
                "default": DEFAULT_HANDOVER_DECISIONS,
                "minimum": 1,
                "maximum": 50,
            },
        ]

    def save_config(self, values: dict[str, Any], hermes_home: str) -> None:
        path = Path(hermes_home) / f"{PROVIDER_NAME}.json"
        clean = {
            "recall_max_chars": int(values.get("recall_max_chars", DEFAULT_RECALL_MAX_CHARS)),
            "handover_decisions": int(values.get("handover_decisions", DEFAULT_HANDOVER_DECISIONS)),
        }
        _atomic_write(path, json.dumps(clean, indent=2) + "\n")

    # -- store --------------------------------------------------------------

    def _rules_path(self) -> Path:
        assert self._root is not None
        return self._root / "RULES.md"

    def _load_rules(self) -> None:
        path = self._rules_path()
        if not path.exists():
            _atomic_write(path, RULES_STARTER)
        try:
            self._rules_snapshot = path.read_text(encoding="utf-8").strip()
        except OSError:
            self._rules_snapshot = ""

    def _reload_entries(self) -> None:
        entries: dict[str, _Entry] = {}
        entries_dir = self._root / "entries" if self._root else None
        if entries_dir and entries_dir.is_dir():
            for path in sorted(entries_dir.glob("*.md")):
                entry = _parse_entry(path)
                if entry is not None:
                    entries[entry.id] = entry
        with self._lock:
            self._entries = entries

    def _store_entry(self, entry: _Entry) -> None:
        assert self._root is not None
        path = self._root / "entries" / f"{entry.id}.md"
        _atomic_write(path, _render_entry(entry))
        entry.path = str(path)
        with self._lock:
            self._entries[entry.id] = entry

    def _new_entry(self, kind: str, title: str, body: str, source: str) -> _Entry:
        created = _now()
        digest = hashlib.sha256(f"{kind}|{title}|{body}|{created}".encode()).hexdigest()[:10]
        return _Entry(
            id=f"{_slug(title)}-{digest}",
            kind=kind,
            title=title,
            created=created,
            session=self._session_id,
            source=source,
            body=body,
        )

    # -- recall -------------------------------------------------------------

    def _recall(self, query: str, limit: int = 6) -> list[tuple[int, _Entry]]:
        terms = {t for t in _tokens(query) if t not in _STOPWORDS}
        if not terms:
            return []
        with self._lock:
            entries = list(self._entries.values())
        scored: list[tuple[int, str, _Entry]] = []
        for entry in entries:
            title_terms = set(_tokens(entry.title))
            body_terms = set(_tokens(entry.body))
            score = 3 * len(terms & title_terms) + len(terms & body_terms)
            if score:
                scored.append((score, entry.created, entry))
        scored.sort(key=lambda item: (item[0], item[1]), reverse=True)
        return [(score, entry) for score, _, entry in scored[:limit]]

    def _format_recall(self, hits: list[tuple[int, _Entry]]) -> str:
        if not hits:
            return ""
        lines = ["## Commonplace recall (curated memory, plain files)"]
        used = len(lines[0])
        for _, entry in hits:
            block = f"\n### [{entry.kind}] {entry.title}\n{entry.body}"
            if used + len(block) > self._recall_max_chars:
                break
            lines.append(block)
            used += len(block)
        return "\n".join(lines) if len(lines) > 1 else ""

    def queue_prefetch(self, query: str, *, session_id: str = "") -> None:
        if is_trivial_prompt(query):
            return
        text = self._format_recall(self._recall(query))
        with self._lock:
            self._prefetch_cache[session_id or self._session_id] = (
                text,
                text.count("### ["),
            )

    def prefetch(self, query: str, *, session_id: str = "") -> str:
        if is_trivial_prompt(query):
            self._last_recall = None
            return ""
        key = session_id or self._session_id
        with self._lock:
            cached = self._prefetch_cache.pop(key, None)
        if cached is not None:
            text, count = cached
        else:
            hits = self._recall(query)
            text = self._format_recall(hits)
            count = text.count("### [")
        self._last_recall = RecallStatus("Commonplace", count) if text else None
        return text

    def recall_status(self) -> Optional[RecallStatus]:
        return self._last_recall

    # -- system prompt --------------------------------------------------------

    def system_prompt_block(self) -> str:
        rules = self._rules_snapshot
        if len(rules) > RULES_PROMPT_CAP:
            rules = rules[:RULES_PROMPT_CAP] + "\n[truncated — full rules in RULES.md]"
        with self._lock:
            count = len(self._entries)
        return (
            "## Commonplace memory\n"
            f"The user's standing rules below are kept verbatim in plain files "
            f"(RULES.md) and outrank convenience. Curated memory holds {count} "
            "entries; use commonplace_recall to search it and "
            "commonplace_remember to add facts, decisions and open items worth "
            "keeping. A handover card lives at commonplace/HANDOVER.md.\n\n"
            f"{rules}"
        )

    # -- turn capture ---------------------------------------------------------

    def sync_turn(
        self, user_content: str, assistant_content: str, *,
        session_id: str = "", messages: Optional[list[dict[str, Any]]] = None,
        turn_author: Optional[dict[str, Any]] = None,
    ) -> None:
        if self._agent_context != "primary" or self._root is None:
            return
        sid = session_id or self._session_id

        def _sync() -> None:
            try:
                path = self._root / "journal" / f"{_slug(sid, 60)}.md"
                block = (
                    f"\n## {_now()} · session {sid}\n\n"
                    f"**User:** {user_content}\n\n"
                    f"**Assistant:** {assistant_content}\n"
                )
                with self._lock:
                    path.parent.mkdir(parents=True, exist_ok=True)
                    with path.open("a", encoding="utf-8") as fh:
                        fh.write(block)
            except Exception as exc:  # journaling must never break a turn
                logger.warning("Commonplace journal write failed: %s", exc)

        if self._sync_thread and self._sync_thread.is_alive():
            self._sync_thread.join(timeout=5.0)
        self._sync_thread = spawn_context_thread(_sync, name="commonplace-sync")
        self._sync_thread.start()

    def on_memory_write(
        self, action: str, target: str, content: str,
        metadata: Optional[dict[str, Any]] = None,
    ) -> None:
        if self._root is None:
            return
        kind = "profile" if target == "user" else "fact"
        previous = (metadata or {}).get("previous_content")
        try:
            if action == "add":
                title = content.strip().splitlines()[0][:80] if content.strip() else "Built-in memory"
                self._store_entry(self._new_entry(kind, title, content, "builtin-memory"))
            elif action in ("replace", "remove"):
                if previous is None:
                    return  # cannot identify the entry safely; skip (per provider guidance)
                with self._lock:
                    match = next(
                        (e for e in self._entries.values() if e.body == previous.strip()),
                        None,
                    )
                if match is None:
                    return
                if action == "remove":
                    Path(match.path).unlink(missing_ok=True)
                    with self._lock:
                        self._entries.pop(match.id, None)
                else:
                    match.body = content
                    self._store_entry(match)
        except Exception as exc:
            logger.warning("Commonplace mirror of built-in memory write failed: %s", exc)

    # -- checkpoint + handover ------------------------------------------------

    def on_pre_compress(
        self, messages: list[dict[str, Any]], *, require_checkpoint: bool = False,
    ) -> str:
        if self._root is None:
            raise RuntimeError("Commonplace is not initialized; cannot checkpoint")
        rows: list[str] = []
        for message in messages or []:
            role = message.get("role")
            if role not in ("user", "assistant"):
                continue
            text = _message_text(message)
            if text.strip():
                rows.append(f"## {role}\n\n{text.strip()}")
        digest = hashlib.sha256("\n\n".join(rows).encode("utf-8")).hexdigest()
        path = self._root / "archive" / f"{digest}.md"
        if not path.exists():
            header = (
                f"# Compression checkpoint\n\n"
                f"- archived: {_now()}\n"
                f"- session: {self._session_id}\n"
                f"- messages: {len(rows)}\n"
                f"- digest: {digest}\n\n"
            )
            _atomic_write(path, header + "\n\n".join(rows) + "\n")
        self._last_checkpoint = digest
        return (
            f"checkpoint: commonplace archive {digest[:12]} "
            f"({len(rows)} messages preserved verbatim in plain files)"
        )

    def _handover_text(self) -> str:
        with self._lock:
            entries = sorted(
                self._entries.values(), key=lambda e: e.created, reverse=True
            )
        opens = [e for e in entries if e.kind == "open"]
        decisions = [e for e in entries if e.kind == "decision"][: self._handover_decisions]
        lines = [
            "# Handover card — Commonplace",
            "",
            f"Generated {_now()} · {len(entries)} curated entries · "
            f"last checkpoint {self._last_checkpoint[:12] or 'none yet'}",
            "",
            "## Standing rules (verbatim)",
            "",
            self._rules_snapshot or "(none set)",
            "",
            "## Open items",
            "",
        ]
        lines += [f"- **{e.title}** — {e.body}" for e in opens] or ["(none)"]
        lines += ["", "## Latest decisions", ""]
        lines += [f"- **{e.title}** — {e.body}" for e in decisions] or ["(none)"]
        lines += [
            "",
            "Full store: entries/ (curated), journal/ (turns), archive/ (checkpoints).",
            "",
        ]
        return "\n".join(lines)

    def on_session_end(self, messages: list[dict[str, Any]]) -> None:
        if self._root is None:
            return
        if self._sync_thread and self._sync_thread.is_alive():
            self._sync_thread.join(timeout=5.0)
        _atomic_write(self._root / "HANDOVER.md", self._handover_text())

    # -- tools ----------------------------------------------------------------

    def get_tool_schemas(self) -> list[dict[str, Any]]:
        return [
            {
                "name": "commonplace_remember",
                "description": (
                    "Add, replace or remove a curated Commonplace entry "
                    "(kind: fact, decision, open, profile). Entries are plain "
                    "Markdown files the user owns and can edit."
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "action": {"type": "string", "enum": ["add", "replace", "remove"]},
                        "kind": {"type": "string", "enum": list(ENTRY_KINDS)},
                        "title": {"type": "string"},
                        "content": {"type": "string"},
                        "entry_id": {"type": "string"},
                    },
                    "required": ["action"],
                },
            },
            {
                "name": "commonplace_recall",
                "description": "Search the curated Commonplace store by keywords.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "query": {"type": "string"},
                        "kind": {"type": "string", "enum": list(ENTRY_KINDS)},
                        "limit": {"type": "integer"},
                    },
                    "required": ["query"],
                },
            },
            {
                "name": "commonplace_handover",
                "description": "Return the handover card: verbatim rules, open items, latest decisions.",
                "parameters": {"type": "object", "properties": {}},
            },
            {
                "name": "commonplace_status",
                "description": "Store statistics: entry counts by kind, journal sessions, checkpoints.",
                "parameters": {"type": "object", "properties": {}},
            },
        ]

    def handle_tool_call(self, tool_name: str, args: dict[str, Any], **kwargs: Any) -> str:
        try:
            if tool_name == "commonplace_remember":
                return self._tool_remember(args)
            if tool_name == "commonplace_recall":
                return self._tool_recall(args)
            if tool_name == "commonplace_handover":
                return json.dumps({"handover": self._handover_text()})
            if tool_name == "commonplace_status":
                return json.dumps(self._status())
            return json.dumps({"error": f"Unknown tool: {tool_name}"})
        except Exception as exc:
            return json.dumps({"error": f"{tool_name} failed: {exc}"})

    def _tool_remember(self, args: dict[str, Any]) -> str:
        action = str(args.get("action", "add"))
        if action == "add":
            kind = str(args.get("kind", "fact"))
            if kind not in ENTRY_KINDS:
                return json.dumps({"error": f"kind must be one of {ENTRY_KINDS}"})
            title = str(args.get("title", "")).strip()
            content = str(args.get("content", "")).strip()
            if not title or not content:
                return json.dumps({"error": "add requires title and content"})
            entry = self._new_entry(kind, title, content, "tool")
            self._store_entry(entry)
            return json.dumps({"stored": entry.id, "kind": kind, "title": title})
        entry_id = str(args.get("entry_id", ""))
        with self._lock:
            entry = self._entries.get(entry_id)
        if entry is None:
            return json.dumps({"error": f"no such entry: {entry_id}"})
        if action == "remove":
            Path(entry.path).unlink(missing_ok=True)
            with self._lock:
                self._entries.pop(entry_id, None)
            return json.dumps({"removed": entry_id})
        if action == "replace":
            content = str(args.get("content", "")).strip()
            if not content:
                return json.dumps({"error": "replace requires content"})
            entry.body = content
            if args.get("title"):
                entry.title = str(args["title"])
            self._store_entry(entry)
            return json.dumps({"replaced": entry_id})
        return json.dumps({"error": f"unknown action: {action}"})

    def _tool_recall(self, args: dict[str, Any]) -> str:
        query = str(args.get("query", ""))
        kind = args.get("kind")
        limit = int(args.get("limit", 6))
        hits = self._recall(query, limit=limit * 2)
        results = []
        for score, entry in hits:
            if kind and entry.kind != kind:
                continue
            results.append(
                {
                    "id": entry.id,
                    "kind": entry.kind,
                    "title": entry.title,
                    "created": entry.created,
                    "content": entry.body,
                    "score": score,
                }
            )
            if len(results) >= limit:
                break
        return json.dumps({"results": results})

    def _status(self) -> dict[str, Any]:
        assert self._root is not None
        with self._lock:
            entries = list(self._entries.values())
        by_kind: dict[str, int] = {}
        for entry in entries:
            by_kind[entry.kind] = by_kind.get(entry.kind, 0) + 1
        journals = list((self._root / "journal").glob("*.md"))
        archives = list((self._root / "archive").glob("*.md"))
        return {
            "provider": PROVIDER_NAME,
            "store": str(self._root),
            "entries": len(entries),
            "by_kind": by_kind,
            "journal_sessions": len(journals),
            "checkpoints": len(archives),
            "last_checkpoint": self._last_checkpoint[:12],
            "recall_max_chars": self._recall_max_chars,
        }


_SKILLS_DIR = Path(__file__).parent / "skills"


def register(ctx: Any) -> None:
    """Called by the memory plugin discovery system."""
    ctx.register_memory_provider(CommonplaceProvider())
    ctx.register_skill(
        "maintenance",
        _SKILLS_DIR / "maintenance" / "SKILL.md",
        "Maintain the Commonplace store: curate entries, prune stale items, refresh the handover card",
    )
