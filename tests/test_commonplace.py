"""Contract + behaviour tests for the Commonplace memory provider.

Run inside the Hermes dev environment from the hermes-agent checkout:

    PYTHONPATH=<plugin-dir>:$PYTHONPATH python -m pytest <plugin-dir>/tests -q

The provider package lives in ../commonplace relative to this file's parent.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from commonplace import CommonplaceProvider, register  # noqa: E402
from agent.memory_provider import MemoryProvider  # noqa: E402


def make_provider(home: Path, session_id: str = "s-1") -> CommonplaceProvider:
    provider = CommonplaceProvider()
    provider.initialize(session_id, hermes_home=str(home), platform="cli")
    return provider


@pytest.fixture()
def home(tmp_path: Path) -> Path:
    return tmp_path / "hermes-home"


def test_satisfies_abc(home: Path) -> None:
    provider = make_provider(home)
    assert isinstance(provider, MemoryProvider)
    assert provider.name == "commonplace"
    assert provider.is_available() is True
    assert provider.pre_compress_checkpoint_api_version == 2


def test_store_layout_and_rules(home: Path) -> None:
    provider = make_provider(home)
    root = home / "commonplace"
    assert (root / "entries").is_dir()
    assert (root / "journal").is_dir()
    assert (root / "archive").is_dir()
    assert (root / "RULES.md").exists()
    block = provider.system_prompt_block()
    assert "Standing rules" in block
    assert "commonplace_remember" in block


def test_remember_recall_round_trip(home: Path) -> None:
    provider = make_provider(home)
    out = json.loads(provider.handle_tool_call("commonplace_remember", {
        "action": "add", "kind": "decision",
        "title": "Deploy on Fridays is banned",
        "content": "We never deploy on Fridays; the deploy freeze was decided after the March outage.",
    }))
    assert "stored" in out
    hits = json.loads(provider.handle_tool_call("commonplace_recall", {"query": "deploy friday freeze"}))
    assert hits["results"] and hits["results"][0]["kind"] == "decision"
    # Entries are plain files the user owns.
    files = list((home / "commonplace" / "entries").glob("*.md"))
    assert len(files) == 1
    assert "Deploy on Fridays" in files[0].read_text()


def test_prefetch_and_trivial_prompt(home: Path) -> None:
    provider = make_provider(home)
    provider.handle_tool_call("commonplace_remember", {
        "action": "add", "kind": "fact", "title": "Brake job cost",
        "content": "Cal logged a $600 brake job on the truck.",
    })
    assert provider.prefetch("thanks") == ""
    block = provider.prefetch("what did the brake job cost?")
    assert "Brake job cost" in block
    status = provider.recall_status()
    assert status is not None and status.count == 1


def test_sync_turn_journals(home: Path) -> None:
    provider = make_provider(home)
    provider.sync_turn("hello there", "general kenobi", session_id="s-1")
    provider.shutdown()  # joins the sync thread
    journals = list((home / "commonplace" / "journal").glob("*.md"))
    assert len(journals) == 1
    text = journals[0].read_text()
    assert "hello there" in text and "general kenobi" in text


def test_checkpoint_archives_and_is_idempotent(home: Path) -> None:
    provider = make_provider(home)
    messages = [
        {"role": "user", "content": "Remember: the vault code is in the blue notebook."},
        {"role": "assistant", "content": "Noted — blue notebook."},
        {"role": "user", "content": "And we ship on Tuesday."},
    ]
    first = provider.on_pre_compress(messages, require_checkpoint=True)
    assert first.startswith("checkpoint: commonplace archive")
    archives = list((home / "commonplace" / "archive").glob("*.md"))
    assert len(archives) == 1
    assert "blue notebook" in archives[0].read_text()
    second = provider.on_pre_compress(messages, require_checkpoint=True)
    assert second == first
    assert len(list((home / "commonplace" / "archive").glob("*.md"))) == 1


def test_checkpoint_fails_closed_when_archive_unwritable(home: Path) -> None:
    provider = make_provider(home)
    archive_dir = home / "commonplace" / "archive"
    # Replace the archive directory with a regular file: writes must fail.
    for child in archive_dir.iterdir():
        child.unlink()
    archive_dir.rmdir()
    archive_dir.write_text("blocked")
    with pytest.raises(Exception):
        provider.on_pre_compress(
            [{"role": "user", "content": "important"}], require_checkpoint=True
        )


def test_session_end_writes_handover(home: Path) -> None:
    provider = make_provider(home)
    provider.handle_tool_call("commonplace_remember", {
        "action": "add", "kind": "open", "title": "Renew the domain",
        "content": "slopcore.news renewal is due next month.",
    })
    provider.handle_tool_call("commonplace_remember", {
        "action": "add", "kind": "decision", "title": "Pika is the avatar lane",
        "content": "All Chris-likeness shots go through Pika; other lanes invent faces.",
    })
    provider.on_session_end([])
    card = (home / "commonplace" / "HANDOVER.md").read_text()
    assert "Renew the domain" in card
    assert "Pika is the avatar lane" in card
    assert "Standing rules (verbatim)" in card


def test_builtin_memory_write_mirror(home: Path) -> None:
    provider = make_provider(home)
    provider.on_memory_write("add", "memory", "The user prefers tea over coffee.")
    hits = json.loads(provider.handle_tool_call("commonplace_recall", {"query": "tea coffee"}))
    assert hits["results"]
    entry_id = hits["results"][0]["id"]
    provider.on_memory_write(
        "replace", "memory", "The user prefers coffee over tea.",
        {"previous_content": "The user prefers tea over coffee."},
    )
    hits = json.loads(provider.handle_tool_call("commonplace_recall", {"query": "coffee"}))
    assert hits["results"][0]["id"] == entry_id
    assert "coffee over tea" in hits["results"][0]["content"]
    provider.on_memory_write(
        "remove", "memory", "", {"previous_content": "The user prefers coffee over tea."}
    )
    hits = json.loads(provider.handle_tool_call("commonplace_recall", {"query": "coffee"}))
    assert hits["results"] == []


def test_config_round_trip(home: Path) -> None:
    provider = CommonplaceProvider()
    schema = provider.get_config_schema()
    keys = {field["key"] for field in schema}
    assert keys == {"recall_max_chars", "handover_decisions"}
    provider.save_config({"recall_max_chars": 1234, "handover_decisions": 3}, str(home))
    fresh = make_provider(home)
    status = json.loads(fresh.handle_tool_call("commonplace_status", {}))
    assert status["recall_max_chars"] == 1234


def test_profile_isolation(tmp_path: Path) -> None:
    one = make_provider(tmp_path / "home-a")
    two = make_provider(tmp_path / "home-b")
    one.handle_tool_call("commonplace_remember", {
        "action": "add", "kind": "fact", "title": "Only in A",
        "content": "profile A secret garden",
    })
    hits = json.loads(two.handle_tool_call("commonplace_recall", {"query": "secret garden"}))
    assert hits["results"] == []


def test_memory_manager_checkpoint_gate(home: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The host-level fail-closed contract: with require_checkpoint on, the
    manager must surface our checkpoint — and must raise when we cannot write."""
    from agent.memory_manager import MemoryManager

    monkeypatch.setenv("HERMES_HOME", str(home))
    provider = CommonplaceProvider()
    mgr = MemoryManager()
    mgr.add_provider(provider)
    mgr.initialize_all(session_id="gate-1", platform="cli", hermes_home=str(home))
    messages = [{"role": "user", "content": "The launch code phrase is purple anvil."}]
    result = mgr.on_pre_compress(messages, require_checkpoint=True)
    assert "checkpoint: commonplace archive" in result
    # Sabotage the archive: the same call must now raise, not forget quietly.
    archive_dir = home / "commonplace" / "archive"
    for child in archive_dir.iterdir():
        child.unlink()
    archive_dir.rmdir()
    archive_dir.write_text("blocked")
    with pytest.raises(Exception):
        mgr.on_pre_compress(
            [{"role": "user", "content": "different words entirely"}],
            require_checkpoint=True,
        )
    mgr.shutdown_all()


def test_register_constructs_fresh_instances() -> None:
    class FakeCtx:
        def __init__(self) -> None:
            self.providers = []
            self.skills = []

        def register_memory_provider(self, provider) -> None:
            self.providers.append(provider)

        def register_skill(self, name, path, description) -> None:
            self.skills.append((name, path))

    ctx_a, ctx_b = FakeCtx(), FakeCtx()
    register(ctx_a)
    register(ctx_b)
    assert isinstance(ctx_a.providers[0], CommonplaceProvider)
    assert ctx_a.providers[0] is not ctx_b.providers[0]  # never a shared singleton
    assert ctx_a.skills and ctx_a.skills[0][0] == "maintenance"
    assert ctx_a.skills[0][1].exists()


def test_memory_manager_end_to_end(home: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from agent.memory_manager import MemoryManager

    # The manager only runs lifecycle hooks from the profile scope the provider
    # was initialized for; the repo's own tests bind that scope via HERMES_HOME.
    monkeypatch.setenv("HERMES_HOME", str(home))
    provider = CommonplaceProvider()
    mgr = MemoryManager()
    mgr.add_provider(provider)
    mgr.initialize_all(session_id="mgr-1", platform="cli", hermes_home=str(home))
    out = mgr.handle_tool_call("commonplace_remember", {
        "action": "add", "kind": "fact", "title": "Manager routed",
        "content": "The MemoryManager routed this write.",
    })
    assert "stored" in out
    mgr.sync_all("a user turn", "an assistant turn")
    mgr.on_session_end([])
    mgr.shutdown_all()
    assert (home / "commonplace" / "HANDOVER.md").exists()
