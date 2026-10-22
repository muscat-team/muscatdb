from __future__ import annotations

import asyncio
import json
import os
import shutil
import subprocess
import time
from pathlib import Path

import pytest
import socketio

from muscat_db import chat
from muscat_db.database import (
    save_chat_message,
    get_recent_chat_messages,
    edit_chat_message,
    delete_chat_message,
    toggle_chat_reaction,
    get_known_chat_usernames,
)


@pytest.fixture
def chat_db(monkeypatch, tmp_path):
    """Fresh SQLite DB for chat persistence tests."""
    path = str(tmp_path / "chat.db")
    monkeypatch.setenv("MUSCAT_DB_PATH", path)
    # Force per-path schema ensure to run against this fresh file.
    import muscat_db.database as db

    db._chat_migrated_paths.discard(path)
    return path


# --------------------------------------------------------------------------
# Persistence (database layer)
# --------------------------------------------------------------------------
def test_save_and_get_recent(chat_db):
    a = save_chat_message("alice", "hello", mentions=["bob"])
    b = save_chat_message("bob", "hi @alice", mentions=["alice"])
    assert a["id"] < b["id"]
    msgs = get_recent_chat_messages(days=7)
    assert [m["user"] for m in msgs] == ["alice", "bob"]  # oldest first
    assert msgs[0]["mentions"] == ["bob"]
    assert msgs[0]["reactions"] == []
    assert msgs[0]["edited"] is False


def test_backfill_window_excludes_old_messages(chat_db):
    save_chat_message("alice", "ancient", created_at=time.time() - 10 * 86400)
    save_chat_message("alice", "recent", created_at=time.time() - 1 * 86400)
    texts = [m["text"] for m in get_recent_chat_messages(days=7)]
    assert texts == ["recent"]


def test_edit_is_author_only(chat_db):
    m = save_chat_message("alice", "typo", kind="user")
    assert edit_chat_message(m["id"], "mallory", "hacked") is None
    updated = edit_chat_message(m["id"], "alice", "fixed")
    assert updated["text"] == "fixed"
    assert updated["edited"] is True


def test_delete_is_author_only_and_hard(chat_db):
    m = save_chat_message("alice", "bye", kind="user")
    assert delete_chat_message(m["id"], "mallory") is False
    assert delete_chat_message(m["id"], "alice") is True
    assert get_recent_chat_messages(days=7) == []


def test_delete_removes_reactions(chat_db):
    m = save_chat_message("alice", "react me", kind="user")
    toggle_chat_reaction(m["id"], "bob", "👍")
    assert delete_chat_message(m["id"], "alice") is True
    # A new message reusing patterns should not inherit stale reactions.
    save_chat_message("alice", "again", kind="user")
    got = get_recent_chat_messages(days=7)
    assert got[0]["reactions"] == []


def test_toggle_reaction_add_and_remove(chat_db):
    m = save_chat_message("alice", "hi", kind="user")
    r = toggle_chat_reaction(m["id"], "alice", "👍")
    assert r["reactions"][0]["count"] == 1
    r = toggle_chat_reaction(m["id"], "bob", "👍")
    assert r["reactions"][0]["count"] == 2
    r = toggle_chat_reaction(m["id"], "alice", "👍")  # toggle off
    assert r["reactions"][0]["count"] == 1
    assert r["reactions"][0]["users"] == ["bob"]


def test_toggle_reaction_missing_message(chat_db):
    assert toggle_chat_reaction(99999, "alice", "👍") is None


def test_known_usernames_includes_chat_authors(chat_db):
    save_chat_message("zoe", "hi", kind="user")
    save_chat_message("amy", "yo", kind="user")
    assert get_known_chat_usernames() == ["amy", "zoe"]  # sorted, case-insensitive


def test_system_messages_are_not_editable_or_deletable(chat_db):
    m = save_chat_message("system", "job done", kind="system")
    assert edit_chat_message(m["id"], "system", "x") is None
    assert delete_chat_message(m["id"], "system") is False


# --------------------------------------------------------------------------
# Command / mention parsing (chat helpers)
# --------------------------------------------------------------------------
def test_test_prefix_matches_ephemeral():
    assert chat._TEST_PREFIX_RE.match("@test hello there")
    assert chat._TEST_PREFIX_RE.match("@TEST case-insensitive")
    assert not chat._TEST_PREFIX_RE.match("testing without at")
    assert not chat._TEST_PREFIX_RE.match("hello @test in middle")


def test_parse_mentions_resolves_known_and_skips_reserved(monkeypatch):
    monkeypatch.setattr(
        chat,
        "_known_cache",
        {"ts": time.time() + 9999, "map": {"alice": "alice", "bob": "Bob"}},
    )
    monkeypatch.setattr(chat, "_user_is_scoped", lambda _u: False)
    found = chat._parse_mentions("hey @alice and @Bob, ignore @here @test @nobody")
    assert found == ["alice", "Bob"]  # canonical casing, reserved/unknown dropped


def test_rate_limit_blocks_after_burst():
    sid = "rate-sid"
    chat._rate.pop(sid, None)
    allowed = sum(1 for _ in range(chat._RATE_MAX + 5) if chat._rate_ok(sid))
    assert allowed == chat._RATE_MAX
    chat._rate.pop(sid, None)


# --------------------------------------------------------------------------
# Identity extraction from the websocket handshake
# --------------------------------------------------------------------------
def test_extract_identity_trusts_loopback_forwarded_user(monkeypatch):
    monkeypatch.delenv("MUSCAT_PROXY_SECRET", raising=False)
    monkeypatch.setenv("MUSCAT_PROXY_SECRET_FILE", "/nonexistent-secret")
    env = {"HTTP_X_FORWARDED_USER": "alice", "asgi.scope": {"client": ("127.0.0.1", 5)}}
    assert chat._extract_identity(env) == "alice"


def test_extract_identity_rejects_non_loopback(monkeypatch):
    monkeypatch.delenv("MUSCAT_PROXY_SECRET", raising=False)
    monkeypatch.setenv("MUSCAT_PROXY_SECRET_FILE", "/nonexistent-secret")
    env = {"HTTP_X_FORWARDED_USER": "mallory", "asgi.scope": {"client": ("10.0.0.9", 5)}}
    assert chat._extract_identity(env) is None


def test_extract_identity_anonymous_when_no_header(monkeypatch):
    monkeypatch.delenv("MUSCAT_PROXY_SECRET", raising=False)
    monkeypatch.setenv("MUSCAT_PROXY_SECRET_FILE", "/nonexistent-secret")
    env = {"asgi.scope": {"client": ("127.0.0.1", 5)}}
    assert chat._extract_identity(env) is None


def test_connect_refuses_anonymous_when_auth_required(monkeypatch):
    """socket.io must fail closed like the HTTP middleware.

    /socket.io is served by an ASGI wrapper mounted in front of the app, so it
    never passes through the auth middleware. Without this gate a peer reaching
    the loopback port directly is handed the whole chat history and can post.
    """
    import asyncio

    monkeypatch.setenv("MUSCAT_REQUIRE_AUTH", "1")
    monkeypatch.delenv("MUSCAT_PROXY_SECRET", raising=False)
    monkeypatch.setenv("MUSCAT_PROXY_SECRET_FILE", "/nonexistent-secret")

    def must_not_read_history():
        raise AssertionError("chat history must not be loaded for a refused connection")

    monkeypatch.setattr(chat.db, "get_recent_chat_messages", must_not_read_history)
    env = {"asgi.scope": {"client": ("127.0.0.1", 5)}}  # no forwarded user
    with pytest.raises(socketio.exceptions.ConnectionRefusedError, match="authentication required"):
        asyncio.run(chat.connect("sid-anon", env))


def test_connect_allows_anonymous_when_auth_not_required(monkeypatch):
    """The default single-user/dev deployment keeps working unauthenticated."""
    import asyncio

    monkeypatch.delenv("MUSCAT_REQUIRE_AUTH", raising=False)
    monkeypatch.delenv("MUSCAT_PROXY_SECRET", raising=False)
    monkeypatch.setenv("MUSCAT_PROXY_SECRET_FILE", "/nonexistent-secret")
    monkeypatch.setattr(chat.db, "get_recent_chat_messages", lambda: [])

    emitted = []
    monkeypatch.setattr(chat.sio, "save_session", _async_noop)
    monkeypatch.setattr(chat.sio, "enter_room", _async_noop)
    monkeypatch.setattr(chat.sio, "emit", lambda *a, **k: _record_async(emitted, *a, **k))
    env = {"asgi.scope": {"client": ("127.0.0.1", 5)}}
    asyncio.run(chat.connect("sid-open", env))
    assert any(event == "history" for event, *_ in emitted)
    chat._users_by_sid.pop("sid-open", None)


async def _async_noop(*args, **kwargs):
    return None


def _record_async(sink, event, *args, **kwargs):
    sink.append((event, args, kwargs))

    async def _done():
        return None

    return _done()


# --------------------------------------------------------------------------
# Job-finished system message hook
# --------------------------------------------------------------------------
def test_on_job_finished_persists_system_message(chat_db, monkeypatch):
    monkeypatch.setattr(chat, "_LOOP", None)  # no event loop -> skip emit, just persist
    chat.on_job_finished(
        job_key="photometry:muscat3/2026-07-18/TOI-1234",
        type_="photometry",
        target="TOI-1234",
        inst="muscat3",
        date="2026-07-18",
        state="done",
    )
    msgs = get_recent_chat_messages(days=7)
    assert len(msgs) == 1
    assert msgs[0]["kind"] == "system"
    assert "TOI-1234" in msgs[0]["text"]
    assert "finished" in msgs[0]["text"]
    assert "/photometry?inst=muscat3&date=2026-07-18&target=TOI-1234" in msgs[0]["text"]
    assert "localhost" not in msgs[0]["text"]


def _job_finished_text(**info) -> str:
    chat.on_job_finished(job_key=info.pop("job_key", "k"), **info)
    return get_recent_chat_messages(days=7)[-1]["text"]


def test_job_finished_links_transit_fit_run_page(chat_db, monkeypatch):
    monkeypatch.setattr(chat, "_LOOP", None)
    text = _job_finished_text(
        type_="transit_fit", target="TOI 1234", inst="muscat2",
        date="2026-07-18", state="error", run_id="r1",
    )
    assert text == (
        "Transit fit for TOI 1234 (muscat2 2026-07-18) failed "
        "/transit-fit?inst=muscat2&date=2026-07-18&target=TOI1234&run=r1"
    )


def test_archive_job_finished_links_archive_page_not_a_fake_target(chat_db, monkeypatch):
    """Regression: an archive download linked /target?name=LCO%20archive."""
    monkeypatch.setattr(chat, "_LOOP", None)
    text = _job_finished_text(type_="archive", state="done")
    assert text == "LCO archive download finished /lco/archive"


def test_archive_job_finished_prefers_its_photometry_link(chat_db, monkeypatch):
    monkeypatch.setattr(chat, "_LOOP", None)
    text = _job_finished_text(
        type_="archive", target="TOI-1234", inst="sinistro", date="2026-07-18",
        state="done", link="/photometry?inst=sinistro&date=2026-07-18&target=TOI-1234",
    )
    assert text == (
        "LCO archive download for TOI-1234 (sinistro 2026-07-18) finished "
        "/photometry?inst=sinistro&date=2026-07-18&target=TOI-1234"
    )


def test_job_finished_explicit_owner_skips_jobs_table_lookup(chat_db, sio_harness, monkeypatch):
    monkeypatch.setattr(chat, "_valid_loop", lambda: object())
    monkeypatch.setattr(chat, "_job_user_name", lambda _key: None)
    monkeypatch.setattr(asyncio, "run_coroutine_threadsafe", lambda coro, _loop: asyncio.run(coro))
    chat.on_job_finished(job_key="lco_archive_download:x", type_="archive", state="done", owner="andrew")
    assert [kw.get("room") for _e, _p, kw in sio_harness.emits] == ["user:andrew"]


def test_archive_notify_reports_frames_owner_and_no_placeholders(monkeypatch):
    from muscat_db import jobs, lco

    fired = {}
    monkeypatch.setattr(jobs, "fire_job_finished", lambda **kw: fired.update(kw))
    monkeypatch.setattr(
        lco, "frame_destination",
        lambda f: ("sinistro", "2026-07-18", lco.Path("/x/sinistro/2026-07-18/f.fits")),
    )
    lco._notify_archive_download_finished({
        "job_id": "abc", "state": "done", "user_name": "andrew",
        "frames": [{"OBJECT": "TOI-1234"}], "frames_total": 1, "results": [],
        "photometry_url": "", "started_at": 0.0,
    })
    assert fired["target"] == "TOI-1234"
    assert (fired["inst"], fired["date"]) == ("sinistro", "2026-07-18")
    assert fired["owner"] == "andrew"
    assert fired["link"] == ""


def test_set_event_loop_and_valid_loop():
    loop = asyncio.new_event_loop()
    try:
        chat.set_event_loop(loop)
        assert chat._LOOP is loop
        assert chat._valid_loop() is loop
        # Close the loop — _valid_loop should detect and clear the stale ref
        loop.close()
        assert chat._valid_loop() is None
        assert chat._LOOP is None
    finally:
        chat._LOOP = None


# --------------------------------------------------------------------------
# @bot: private, per-user, in-memory conversation context
# --------------------------------------------------------------------------
@pytest.fixture(autouse=True)
def _clear_agent_ctx():
    """The @bot context lives in a module global; isolate each test."""
    chat._agent_ctx.clear()
    yield
    chat._agent_ctx.clear()


def test_agent_ctx_roundtrips_user_and_assistant_turns():
    key = chat._ctx_key("alice", "sid1")
    assert chat._agent_ctx_get(key) == []
    chat._agent_ctx_add(
        key, {"role": "user", "content": "alice: q1"}, {"role": "assistant", "content": "a1"}
    )
    turns = chat._agent_ctx_get(key)
    assert turns == [
        {"role": "user", "content": "alice: q1"},
        {"role": "assistant", "content": "a1"},
    ]


def test_agent_ctx_caps_at_history_turns():
    key = chat._ctx_key("alice", "sid1")
    for i in range(20):
        chat._agent_ctx_add(
            key, {"role": "user", "content": f"q{i}"}, {"role": "assistant", "content": f"a{i}"}
        )
    # The deque keeps only the most recent _AGENT_HISTORY_TURNS entries.
    assert len(chat._agent_ctx_get(key)) == chat._AGENT_HISTORY_TURNS


def test_agent_ctx_drops_after_idle_ttl(monkeypatch):
    monkeypatch.setenv("MUSCAT_AGENT_HISTORY_TTL_S", "600")
    key = chat._ctx_key("alice", "sid1")
    chat._agent_ctx_add(
        key, {"role": "user", "content": "q"}, {"role": "assistant", "content": "a"}
    )
    # Age the last-activity stamp past the TTL; the next read resets it.
    _, turns = chat._agent_ctx[key]
    chat._agent_ctx[key] = (time.time() - 1000, turns)
    assert chat._agent_ctx_get(key) == []
    assert key not in chat._agent_ctx  # auto-cleared


def test_agent_ctx_reset_clears_only_that_conversation():
    a = chat._ctx_key("alice", "s1")
    b = chat._ctx_key("bob", "s2")
    for k in (a, b):
        chat._agent_ctx_add(
            k, {"role": "user", "content": "q"}, {"role": "assistant", "content": "a"}
        )
    chat._agent_ctx_clear(a)
    assert chat._agent_ctx_get(a) == []
    assert chat._agent_ctx_get(b) != []  # bob's private thread is untouched


def test_private_target_routes_to_user_room_or_sid():
    # Authenticated → the user's room (reaches every tab + the pop-out window).
    assert chat._private_target("alice", "sidX") == {"room": "user:alice"}
    # Anonymous → just this one connection.
    assert chat._private_target(None, "sidX") == {"to": "sidX"}


def test_private_agent_payload_is_unsaved_and_flagged():
    p = chat._private_agent_payload("hello")
    assert p["id"] is None  # no edit/delete/react controls client-side
    assert p["private"] is True  # drives the 'only you' badge
    assert p["kind"] == "agent"


@pytest.mark.parametrize(
    "text, matches",
    [
        ("/me observing TOI-1234", True),
        ("  /ME waves", True),
        ("/media query", False),  # word boundary: not the /me command
        ("hi /me", False),  # only at the start of the message
    ],
)
def test_me_command_prefix(text, matches):
    assert bool(chat._ME_PREFIX_RE.match(text)) is matches


@pytest.mark.parametrize(
    "text, matches",
    [
        ("heads up @everyone", True),
        ("@all please check", True),
        ("posting to @channel", True),
        ("email me at a@everyone.com", False),  # not preceded by word/@ char
        ("ping @allen", False),  # word boundary: @allen is a name
        ("no ping here", False),
    ],
)
def test_broadcast_mention_detection(text, matches):
    assert bool(chat._BROADCAST_MENTION_RE.search(text)) is matches


def test_online_usernames_excludes_anonymous(monkeypatch):
    monkeypatch.setattr(
        chat, "_users_by_sid", {"s1": "alice", "s2": "alice", "s3": None, "s4": "bob"}
    )
    assert chat._online_usernames() == {"alice", "bob"}  # de-duped, no anonymous


# --------------------------------------------------------------------------
# socket.io handlers
#
# The tests above cover the database layer beneath these handlers. The handlers
# themselves are what receive untrusted client input, so they need their own
# coverage: they are where malformed payloads are rejected, where identity is
# resolved, and where author-only edit/delete is enforced before the DB is
# reached at all.
# --------------------------------------------------------------------------
class _FakeSio:
    """Records emits so a handler can be driven without a socket.io server."""

    def __init__(self):
        self.emits = []

    async def emit(self, event, payload=None, **kwargs):
        self.emits.append((event, payload, kwargs))

    def events(self):
        return [event for event, _payload, _kw in self.emits]


@pytest.fixture
def sio_harness(monkeypatch):
    fake = _FakeSio()
    monkeypatch.setattr(chat, "sio", fake)
    monkeypatch.setattr(chat, "_rate", {})
    monkeypatch.setattr(chat, "_users_by_sid", {})

    async def _not_scoped(_sid):
        return False

    monkeypatch.setattr(chat, "_session_scoped", _not_scoped)
    return fake


def _as_user(monkeypatch, name):
    async def _session_user(_sid):
        return name

    monkeypatch.setattr(chat, "_session_user", _session_user)


@pytest.mark.parametrize("payload", [None, "text", 42, ["a"], {"text": 123}, {"text": "   "}])
def test_message_ignores_malformed_payloads(sio_harness, monkeypatch, payload):
    """Malformed input must be dropped silently, never persisted."""
    import asyncio

    _as_user(monkeypatch, "alice")
    monkeypatch.setattr(
        chat.db,
        "save_chat_message",
        lambda *a, **k: pytest.fail("malformed payload must not be persisted"),
    )
    asyncio.run(chat.message("sid-1", payload))
    assert sio_harness.emits == []


def test_message_rate_limit_drops_the_burst(sio_harness, monkeypatch):
    import asyncio

    _as_user(monkeypatch, "alice")
    saved = []
    monkeypatch.setattr(
        chat.db,
        "save_chat_message",
        lambda user, text, **k: (
            saved.append(text) or {"id": len(saved), "user": user, "text": text}
        ),
    )
    for i in range(chat._RATE_MAX + 5):
        asyncio.run(chat.message("sid-1", {"text": f"m{i}"}))
    assert len(saved) == chat._RATE_MAX, "rate limit did not cap the burst"


def test_test_prefixed_message_is_ephemeral_and_never_persisted(sio_harness, monkeypatch):
    """@test broadcasts live but must never reach the database."""
    import asyncio

    _as_user(monkeypatch, "alice")
    monkeypatch.setattr(
        chat.db,
        "save_chat_message",
        lambda *a, **k: pytest.fail("an @test message must not be persisted"),
    )
    asyncio.run(chat.message("sid-1", {"text": "@test hello"}))

    assert sio_harness.events() == ["message"]
    _event, payload, _kw = sio_harness.emits[0]
    assert payload["ephemeral"] is True
    assert payload["id"] is None
    assert payload["text"] == "hello"


def test_message_notifies_each_mentioned_user(sio_harness, monkeypatch):
    import asyncio

    _as_user(monkeypatch, "alice")
    monkeypatch.setattr(chat, "_known_users_map", lambda: {"bob": "bob", "carol": "carol"})
    monkeypatch.setattr(
        chat.db,
        "save_chat_message",
        lambda user, text, **k: {"id": 1, "user": user, "text": text},
    )
    asyncio.run(chat.message("sid-1", {"text": "@bob @carol look at this"}))

    rooms = [kw.get("room") for event, _p, kw in sio_harness.emits if event == "mention"]
    assert sorted(rooms) == ["user:bob", "user:carol"]


def test_edit_and_delete_require_an_identity(sio_harness, monkeypatch):
    """Anonymous connections must not reach the database at all."""
    import asyncio

    _as_user(monkeypatch, None)
    monkeypatch.setattr(
        chat.db,
        "edit_chat_message",
        lambda *a, **k: pytest.fail("anonymous edit must not reach the database"),
    )
    monkeypatch.setattr(
        chat.db,
        "delete_chat_message",
        lambda *a, **k: pytest.fail("anonymous delete must not reach the database"),
    )
    asyncio.run(chat.edit_message("sid-1", {"id": 1, "text": "hi"}))
    asyncio.run(chat.delete_message("sid-1", {"id": 1}))
    assert sio_harness.emits == []


def test_edit_rejected_for_another_users_message(sio_harness, monkeypatch):
    """The DB returns None when the editor is not the author; the handler must
    report that back rather than broadcasting an edit."""
    import asyncio

    _as_user(monkeypatch, "mallory")
    monkeypatch.setattr(chat.db, "edit_chat_message", lambda *a, **k: None)
    asyncio.run(chat.edit_message("sid-1", {"id": 7, "text": "changed"}))

    assert sio_harness.events() == ["chat_error"]
    assert "not allowed" in sio_harness.emits[0][1]["error"]


def test_delete_rejected_for_another_users_message(sio_harness, monkeypatch):
    import asyncio

    _as_user(monkeypatch, "mallory")
    monkeypatch.setattr(chat.db, "delete_chat_message", lambda *a, **k: False)
    asyncio.run(chat.delete_message("sid-1", {"id": 7}))

    assert sio_harness.events() == ["chat_error"]


def test_successful_edit_and_delete_broadcast(sio_harness, monkeypatch):
    import asyncio

    _as_user(monkeypatch, "alice")
    monkeypatch.setattr(chat.db, "get_chat_message", lambda mid: None)
    monkeypatch.setattr(
        chat.db, "edit_chat_message", lambda mid, u, t, mentions=None: {"id": mid, "text": t}
    )
    monkeypatch.setattr(chat.db, "delete_chat_message", lambda mid, u: True)
    asyncio.run(chat.edit_message("sid-1", {"id": 3, "text": "fixed"}))
    asyncio.run(chat.delete_message("sid-1", {"id": 3}))

    assert sio_harness.events() == ["message_edited", "message_deleted"]


def test_reaction_requires_identity_and_valid_emoji(sio_harness, monkeypatch):
    import asyncio

    monkeypatch.setattr(
        chat.db,
        "toggle_chat_reaction",
        lambda *a, **k: pytest.fail("must not reach the database"),
    )
    _as_user(monkeypatch, None)
    asyncio.run(chat.toggle_reaction("sid-1", {"id": 1, "emoji": "👍"}))
    _as_user(monkeypatch, "alice")
    asyncio.run(chat.toggle_reaction("sid-1", {"id": 1, "emoji": "   "}))
    asyncio.run(chat.toggle_reaction("sid-1", {"id": "notanint", "emoji": "👍"}))
    assert sio_harness.emits == []


# --------------------------------------------------------------------------
# Scoped viewers (issue #202): no part in the shared room
# --------------------------------------------------------------------------
def test_scoped_viewer_cannot_post_to_the_shared_room(sio_harness, monkeypatch):
    import asyncio

    _as_user(monkeypatch, "andrew")

    async def _scoped(_sid):
        return True

    monkeypatch.setattr(chat, "_session_scoped", _scoped)
    monkeypatch.setattr(
        chat.db,
        "save_chat_message",
        lambda *a, **k: pytest.fail("a scoped viewer's post must not be persisted"),
    )
    asyncio.run(chat.message("sid-1", {"text": "hello room"}))
    assert sio_harness.events() == ["chat_error"]


def test_scoped_viewer_cannot_edit_delete_react_or_type(sio_harness, monkeypatch):
    import asyncio

    _as_user(monkeypatch, "andrew")

    async def _scoped(_sid):
        return True

    monkeypatch.setattr(chat, "_session_scoped", _scoped)
    for name in ("edit_chat_message", "delete_chat_message", "toggle_chat_reaction"):
        monkeypatch.setattr(
            chat.db, name, lambda *a, **k: pytest.fail("scoped viewer reached the DB")
        )
    asyncio.run(chat.edit_message("s", {"id": 1, "text": "x"}))
    asyncio.run(chat.delete_message("s", {"id": 1}))
    asyncio.run(chat.toggle_reaction("s", {"id": 1, "emoji": "👍"}))
    asyncio.run(chat.typing("s", {"typing": True}))
    assert sio_harness.emits == []


def test_shared_room_broadcasts_go_to_the_public_room_only(sio_harness, monkeypatch):
    import asyncio

    _as_user(monkeypatch, "alice")
    monkeypatch.setattr(
        chat.db, "save_chat_message", lambda user, text, **k: {"id": 1, "user": user, "text": text}
    )
    monkeypatch.setattr(chat, "_parse_mentions", lambda text: [])
    asyncio.run(chat.message("sid-1", {"text": "hello room"}))
    broadcasts = [kw for event, _p, kw in sio_harness.emits if event == "message"]
    assert broadcasts and all(kw.get("room") == chat._PUBLIC_ROOM for kw in broadcasts)


def test_mentions_and_broadcast_pings_skip_scoped_users(sio_harness, monkeypatch):
    """A mention carries the shared-room text, so a scoped user must not get it
    (review of #230)."""
    import asyncio

    _as_user(monkeypatch, "alice")
    monkeypatch.setattr(chat, "_user_is_scoped", lambda name: name == "andrew")
    monkeypatch.setattr(chat, "_known_users_map", lambda: {"bob": "bob", "andrew": "andrew"})
    monkeypatch.setattr(chat, "_users_by_sid", {"s1": "andrew", "s2": "bob", "s3": "alice"})
    monkeypatch.setattr(
        chat.db, "save_chat_message", lambda user, text, **k: {"id": 1, "user": user, "text": text}
    )
    asyncio.run(chat.message("sid-1", {"text": "@andrew @bob HIDDENTGT looks odd"}))
    asyncio.run(chat.message("sid-1", {"text": "@all HIDDENTGT again"}))
    asyncio.run(chat.message("sid-1", {"text": "/me checks HIDDENTGT @andrew"}))
    rooms = [kw.get("room") for event, _p, kw in sio_harness.emits if event == "mention"]
    assert "user:andrew" not in rooms
    assert "user:bob" in rooms


def test_scoped_viewer_cannot_reach_the_public_room_with_test_and_bot(sio_harness, monkeypatch):
    import asyncio

    _as_user(monkeypatch, "andrew")

    async def _scoped(_sid):
        return True

    monkeypatch.setattr(chat, "_session_scoped", _scoped)
    asyncio.run(chat.message("sid-1", {"text": "@test @bot HIDDENTGT"}))
    assert sio_harness.events() == ["chat_error"]
    assert not [kw for e, _p, kw in sio_harness.emits if kw.get("room") == chat._PUBLIC_ROOM]


def test_addressed_only_history_drops_public_messages(chat_db):
    save_chat_message("alice", "public note", kind="user")
    save_chat_message("system", "Photometry for X done", kind="system", visible_to="andrew")
    save_chat_message("system", "someone else's job", kind="system", visible_to="bob")
    texts = [
        m["text"] for m in get_recent_chat_messages(days=7, user="andrew", addressed_only=True)
    ]
    assert texts == ["Photometry for X done"]
    everyone = [m["text"] for m in get_recent_chat_messages(days=7, user="andrew")]
    assert "public note" in everyone and "someone else's job" not in everyone


def _run_job_finished(monkeypatch, sio_harness, owner):
    """Drive on_job_finished with a loop stand-in that runs the emit inline."""
    import asyncio

    monkeypatch.setattr(chat, "_valid_loop", lambda: object())
    monkeypatch.setattr(chat, "_job_user_name", lambda _key: owner)
    monkeypatch.setattr(asyncio, "run_coroutine_threadsafe", lambda coro, _loop: asyncio.run(coro))
    chat.on_job_finished(
        job_key="photometry:sinistro/2026-07-18/HIDDENTGT",
        type_="photometry",
        target="HIDDENTGT",
        inst="sinistro",
        date="2026-07-18",
        state="done",
    )
    return [kw.get("room") for _e, _p, kw in sio_harness.emits]


def test_job_finished_with_owner_reaches_only_the_owner_room(chat_db, sio_harness, monkeypatch):
    """A restricted target named in a job-finished message must not be
    broadcast to the shared room when the job has an owner (#144 chat item)."""
    rooms = _run_job_finished(monkeypatch, sio_harness, "andrew")
    assert rooms == ["user:andrew"]


def test_ownerless_job_finished_is_public_only_and_hidden_from_scoped_history(
    chat_db, sio_harness, monkeypatch
):
    """With no owner the message goes to the shared room, which a scoped viewer
    never joins, and the addressed-only backfill drops it."""
    rooms = _run_job_finished(monkeypatch, sio_harness, None)
    assert rooms == [chat._PUBLIC_ROOM]
    scoped = get_recent_chat_messages(days=7, user="andrew", addressed_only=True)
    assert all("HIDDENTGT" not in m["text"] for m in scoped)
    assert any("HIDDENTGT" in m["text"] for m in get_recent_chat_messages(days=7))


# --------------------------------------------------------------------------
# Job-finished dedup, /me edit/delete, mentions refreshed on edit
# --------------------------------------------------------------------------
@pytest.fixture
def fresh_hooks(monkeypatch):
    from muscat_db import jobs

    calls = []
    monkeypatch.setattr(jobs, "_notified_job_keys", {})
    monkeypatch.setattr(jobs, "_JOB_FINISHED_HOOKS", [lambda **kw: calls.append(kw["state"])])
    return jobs, calls


def test_rerun_of_the_same_run_notifies_again(fresh_hooks):
    """Regression: the dedup key was the job key alone, so a re-run of the same
    inst/date/target/run finished silently until the server restarted."""
    jobs, calls = fresh_hooks
    key = "photometry:muscat3/2026-07-18/TOI-1234/default"
    jobs.fire_job_finished(key, state="done", started_at=100.0)
    jobs.fire_job_finished(key, state="done", started_at=100.0)  # next sync pass
    jobs.fire_job_finished(key, state="error", started_at=200.0)  # the re-run
    assert calls == ["done", "error"]


def test_notified_run_memory_is_bounded(fresh_hooks, monkeypatch):
    jobs, calls = fresh_hooks
    monkeypatch.setattr(jobs, "_NOTIFIED_MAX", 3)
    for i in range(5):
        jobs.fire_job_finished(f"k{i}", state="done", started_at=float(i))
    assert len(jobs._notified_job_keys) == 3
    assert len(calls) == 5


def test_me_actions_are_editable_and_deletable_by_their_author(chat_db):
    m = save_chat_message("alice", "is observing", kind="action")
    assert edit_chat_message(m["id"], "bob", "x") is None
    updated = edit_chat_message(m["id"], "alice", "is reducing")
    assert updated["text"] == "is reducing" and updated["edited"] and updated["kind"] == "action"
    assert delete_chat_message(m["id"], "bob") is False
    assert delete_chat_message(m["id"], "alice") is True


def test_edit_replaces_stored_mentions(chat_db):
    m = save_chat_message("alice", "hi @bob", mentions=["bob"], kind="user")
    updated = edit_chat_message(m["id"], "alice", "hi @carol", mentions=["carol"])
    assert updated["mentions"] == ["carol"]
    assert get_recent_chat_messages(days=7)[-1]["mentions"] == ["carol"]


def test_edit_nudges_only_newly_mentioned_users(chat_db, sio_harness, monkeypatch):
    _as_user(monkeypatch, "alice")
    monkeypatch.setattr(
        chat, "_known_users_map", lambda: {"alice": "alice", "bob": "bob", "carol": "carol"}
    )
    monkeypatch.setattr(chat, "_user_is_scoped", lambda _u: False)
    m = save_chat_message("alice", "hi @bob", mentions=["bob"], kind="user")
    asyncio.run(chat.edit_message("sid-1", {"id": m["id"], "text": "hi @bob @carol @alice"}))
    mention_rooms = [kw.get("room") for e, _p, kw in sio_harness.emits if e == "mention"]
    assert mention_rooms == ["user:carol"]
    edited = [p for e, p, _kw in sio_harness.emits if e == "message_edited"]
    assert edited and edited[0]["mentions"] == ["alice", "bob", "carol"]


# --------------------------------------------------------------------------
# Regressions: sender self-nudge, @bot without a captured loop, @bot context
# ordering, and scoped usernames in stored mentions.
# --------------------------------------------------------------------------
def test_message_does_not_nudge_the_sender(sio_harness, monkeypatch):
    _as_user(monkeypatch, "alice")
    monkeypatch.setattr(chat, "_known_users_map", lambda: {"alice": "alice", "bob": "bob"})
    monkeypatch.setattr(chat, "_user_is_scoped", lambda _u: False)
    monkeypatch.setattr(
        chat.db, "save_chat_message", lambda user, text, **k: {"id": 1, "user": user, "text": text}
    )
    asyncio.run(chat.message("sid-1", {"text": "@alice @bob look"}))
    asyncio.run(chat.message("sid-1", {"text": "/me waves at @alice"}))
    rooms = [kw.get("room") for e, _p, kw in sio_harness.emits if e == "mention"]
    assert rooms == ["user:bob"]


def test_bot_question_answers_without_a_captured_event_loop(sio_harness, monkeypatch):
    """A @bot question must be answered whenever the handler runs; the loop is
    the handler's own running loop, not a global captured earlier."""
    _as_user(monkeypatch, "alice")
    monkeypatch.setattr(chat, "_LOOP", None)
    monkeypatch.setattr(chat.chat_agent, "reserve", lambda: True)
    monkeypatch.setattr(chat.chat_agent, "release", lambda: None)

    async def fake_answer(question, history):
        return f"answer to {question}"

    monkeypatch.setattr(chat.chat_agent, "answer", fake_answer)

    async def run():
        await chat.message("sid-1", {"text": "@bot what is photometry"})
        await asyncio.gather(*list(chat._agent_tasks))

    asyncio.run(run())
    texts = [p["text"] for e, p, _kw in sio_harness.emits if e == "message" and p.get("kind") == "agent"]
    assert texts == ["answer to what is photometry"]


def test_concurrent_bot_questions_see_each_others_turns_in_order(sio_harness, monkeypatch):
    """Two questions from one user must not both start from the same context:
    the second one waits and is answered with the first exchange in history."""
    monkeypatch.setattr(chat.chat_agent, "reserve", lambda: True)
    monkeypatch.setattr(chat.chat_agent, "release", lambda: None)
    calls = []

    async def fake_answer(question, history):
        calls.append((question, len(history)))
        await asyncio.sleep(0.01)
        return f"re:{question}"

    monkeypatch.setattr(chat.chat_agent, "answer", fake_answer)

    async def run():
        await asyncio.gather(
            chat._run_agent("sid-1", "alice", "first"),
            chat._run_agent("sid-2", "alice", "second"),
        )

    asyncio.run(run())
    assert calls == [("first", 0), ("second", 2)]


def test_scoped_usernames_are_not_stored_as_mentions(monkeypatch):
    monkeypatch.setattr(
        chat, "_known_cache", {"ts": time.time() + 9999, "map": {"alice": "alice", "andrew": "andrew"}}
    )
    monkeypatch.setattr(chat, "_user_is_scoped", lambda name: name == "andrew")
    assert chat._parse_mentions("@andrew and @alice") == ["alice"]


_CHAT_JS = Path(__file__).resolve().parents[1] / "src" / "muscat_db" / "static" / "js" / "chat.js"

# Runs the real chat.js against a minimal DOM stub and reports whether a /me
# line addressed to alice carries the mention highlight through render and edit.
_CHAT_JS_HARNESS = r"""
const fs = require("fs");
const src = fs.readFileSync(process.env.CHAT_JS, "utf8");
class Text { constructor(t) { this.nodeType = 3; this.textContent = String(t); this.parentNode = null; } }
class El {
  constructor(tag = "div") {
    this.tagName = tag.toUpperCase(); this.id = ""; this.children = []; this.parentNode = null;
    this.dataset = {}; this.style = {}; this.value = ""; this.classes = new Set();
    const self = this;
    this.classList = {
      add: (...c) => c.forEach((x) => self.classes.add(x)),
      remove: (...c) => c.forEach((x) => self.classes.delete(x)),
      toggle: (c, f) => { const on = f === undefined ? !self.classes.has(c) : !!f; on ? self.classes.add(c) : self.classes.delete(c); return on; },
      contains: (c) => self.classes.has(c),
    };
  }
  get className() { return [...this.classes].join(" "); }
  set className(v) { this.classes = new Set(String(v).split(/\s+/).filter(Boolean)); }
  get textContent() { return this.children.map((c) => c.textContent).join(""); }
  set textContent(v) { this.children = []; if (String(v) !== "") this.children.push(new Text(v)); }
  appendChild(c) { c.parentNode = this; this.children.push(c); return c; }
  append(...nodes) { nodes.forEach((n) => this.appendChild(typeof n === "string" ? new Text(n) : n)); }
  remove() { if (this.parentNode) this.parentNode.children = this.parentNode.children.filter((c) => c !== this); }
  replaceWith(n) { const p = this.parentNode; if (!p) return; p.children[p.children.indexOf(this)] = n; n.parentNode = p; this.parentNode = null; }
  querySelectorAll(sel) {
    const last = sel.split(",")[0].trim().split(/\s+/).pop();
    const m = last.match(/\.([\w-]+)$/);
    const out = [];
    if (!m) return out;
    const walk = (n) => { for (const c of n.children) { if (c instanceof El) { if (c.classes.has(m[1])) out.push(c); walk(c); } } };
    walk(this);
    return out;
  }
  querySelector(sel) { return this.querySelectorAll(sel)[0] || null; }
  setAttribute() {} getAttribute() { return null; } addEventListener() {} removeEventListener() {}
  focus() {} blur() {} setSelectionRange() {} contains() { return false; }
}
const byId = {};
const el = (id, cls = "") => { const e = new El(); e.id = id; e.classes = new Set(cls.split(" ").filter(Boolean)); byId[id] = e; return e; };
el("chat-window"); byId["chat-window"].dataset.currentUser = "alice";
el("chat-messages"); el("message-input"); el("send-button"); el("chat-header");
el("chat-status-dot"); el("chat-online"); el("chat-unread"); el("chat-typing"); el("chat-autocomplete", "hidden");
el("minimize-chat");
const listeners = {};
const document = {
  addEventListener(ev, fn) { (listeners[ev] = listeners[ev] || []).push(fn); },
  removeEventListener() {},
  getElementById(id) { return byId[id] || null; },
  createElement(t) { return new El(t); },
  createTextNode(t) { return new Text(t); },
  querySelector() { return null; },
  body: new El("body"), documentElement: new El("html"), title: "", hasFocus() { return true; },
};
const window = {
  location: { href: "http://x/", origin: "http://x" }, innerWidth: 1024,
  addEventListener() {}, localStorage: { getItem: () => null, setItem() {} },
};
let SOCKET = null;
const io = () => { SOCKET = { active: true, h: {}, on(ev, fn) { this.h[ev] = fn; }, emit() {} }; return SOCKET; };
const ctx = {
  document, window, io, console,
  fetch: () => Promise.resolve({ ok: false }),
  localStorage: window.localStorage,
  setTimeout: () => 0, clearTimeout() {}, setInterval: () => 0, clearInterval() {},
};
new Function(...Object.keys(ctx), src)(...Object.values(ctx));
listeners.DOMContentLoaded.forEach((f) => f());

const chatMessages = byId["chat-messages"];
const last = () => chatMessages.children[chatMessages.children.length - 1];
const ts = Date.now() / 1000;
const base = { id: 7, kind: "action", user: "bob", ts, reactions: [], edited: false };
const out = {};
SOCKET.h.history({ messages: [{ ...base, text: "waves at @alice", mentions: ["alice"] }] });
out.rendered = last().classes.has("mention-me");
SOCKET.h.message_edited({ ...base, text: "waves at nobody", mentions: [], edited: true });
out.afterRemoval = last().classes.has("mention-me");
SOCKET.h.message_edited({ ...base, text: "waves at @alice", mentions: ["alice"], edited: true });
out.afterAddition = last().classes.has("mention-me");
process.stdout.write(JSON.stringify(out));
"""


@pytest.mark.skipif(shutil.which("node") is None, reason="node not installed")
def test_me_line_mention_highlight_follows_render_and_edits():
    """A /me line that names you is highlighted like a message, and the highlight
    tracks edits that add or remove the mention."""
    proc = subprocess.run(
        [shutil.which("node"), "-e", _CHAT_JS_HARNESS],
        env={**os.environ, "CHAT_JS": str(_CHAT_JS)},
        capture_output=True, text=True, timeout=60,
    )
    assert proc.returncode == 0, proc.stderr
    assert json.loads(proc.stdout) == {
        "rendered": True,
        "afterRemoval": False,
        "afterAddition": True,
    }
