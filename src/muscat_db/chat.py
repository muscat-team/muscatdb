"""Real-time team chat: presence, @-mentions, edit/delete, reactions, and
job-finished system messages.

Message *history* is persisted (see database.save_chat_message and the
chat_messages table), but rendering/escaping happens client-side (DOM text
nodes), so text is stored and relayed verbatim. Ephemeral ``@test`` messages
are broadcast live and never written to the database.

Identity is the nginx-authenticated user, extracted from the websocket
handshake headers (which nginx now forwards for /socket.io) and validated with
the same trust check as HTTP requests. It is the sole basis for message
attribution and for edit/delete/react authorization — the client cannot forge
who it is.
"""

from __future__ import annotations

import asyncio
import logging
import os
import re
import urllib.parse
import time
from collections import deque

from . import access
from . import chat_agent
from . import jobs
from .auth import (
    PROXY_SECRET_HEADER,
    ADMIN_EMAIL,
    UNKNOWN_EMAIL,
    authentication_required,
    resolve_forwarded_identity,
)
from . import database as db
from .job_store import get_job_store
from .web import sio
# python-socketio forwards the reason to the client only for its own refusal
# class; the builtin ConnectionRefusedError reaches the browser as a generic
# "Connection refused by server".
from socketio.exceptions import ConnectionRefusedError as SocketRefusedError

logger = logging.getLogger(__name__)

# ----- limits & tokens -----------------------------------------------------
_MAX_TEXT_LEN = 2000
_MAX_EMOJI_LEN = 16
_RATE_MAX = 12  # messages ...
_RATE_WINDOW_S = 10.0  # ... per this many seconds, per user (anonymous: per connection)
# @-tokens that are commands or noise, never real usernames to notify. The
# codebase assistant's name is reserved too: @bot triggers the agent (below) and
# must never be resolved as a person to nudge.
_RESERVED_MENTIONS = {"here", "test", "everyone", "channel", "all", chat_agent.AGENT_NAME}
_MENTION_RE = re.compile(r"(?<![\w@])@([A-Za-z0-9._-]{1,64})")
_TEST_PREFIX_RE = re.compile(r"^\s*@test\b\s*", re.IGNORECASE)
_ME_PREFIX_RE = re.compile(r"^\s*/me\b", re.IGNORECASE)
# @all / @everyone / @channel: nudge everyone currently online (a team heads-up).
_BROADCAST_MENTION_RE = re.compile(r"(?<![\w@])@(?:all|everyone|channel)\b", re.IGNORECASE)

# ----- live (in-memory) connection state -----------------------------------
# Single-worker deployment: these live in one process. A multi-worker/multi-host
# rollout would move presence + fan-out to the socket.io Redis manager.
_users_by_sid: dict[str, str | None] = {}  # sid -> username (None = anonymous)
_rate: dict[str, deque] = {}
_LOOP: asyncio.AbstractEventLoop | None = None
# Everything the whole team sees is emitted to this room. A scoped viewer
# (issue #202) never joins it, so the shared room's free text -- which can name
# any target -- cannot reach them; they use explicit group rooms and @bot.
_PUBLIC_ROOM = "chat:public"
_rooms_by_sid: dict[str, set[str]] = {}
_known_cache: dict = {"ts": 0.0, "map": {}}


def set_event_loop(loop: asyncio.AbstractEventLoop) -> None:
    """Capture the server event loop so background threads (job finish hooks)
    can schedule broadcasts onto it."""
    global _LOOP
    _LOOP = loop


def _valid_loop() -> asyncio.AbstractEventLoop | None:
    """Return ``_LOOP`` if it is still open, otherwise clear the stale ref."""
    global _LOOP
    if _LOOP is not None and _LOOP.is_closed():
        _LOOP = None
    return _LOOP


# ----- helpers -------------------------------------------------------------
def _resolve_identity(environ: dict) -> tuple[str | None, str]:
    """``(username, outcome)`` for a websocket handshake.

    python-socketio's ASGI layer exposes handshake headers as WSGI-style
    ``HTTP_*`` keys and the peer address via the ASGI scope / ``REMOTE_ADDR``.
    We reuse the exact HTTP trust check (loopback peer + shared secret).
    """
    forwarded = environ.get("HTTP_X_FORWARDED_USER")
    secret_key = "HTTP_" + PROXY_SECRET_HEADER.upper().replace("-", "_")
    presented_secret = environ.get(secret_key)
    client_host = None
    scope = environ.get("asgi.scope")
    if isinstance(scope, dict) and scope.get("client"):
        client_host = scope["client"][0]
    if client_host is None:
        client_host = environ.get("REMOTE_ADDR")
    return resolve_forwarded_identity(forwarded, client_host, presented_secret)


def _extract_identity(environ: dict) -> str | None:
    """Return the trusted nginx user for a websocket handshake, or None."""
    return _resolve_identity(environ)[0]


def _display(user: str | None) -> str:
    return user or "Anonymous"


def _private_target(user: str | None, sid: str) -> dict:
    """socket.io emit kwargs that reach only the asker: their user room (every
    tab + the pop-out window) when authenticated, else just this connection."""
    return {"room": f"user:{user}"} if user else {"to": sid}


def _online_users() -> list[str]:
    return sorted({_display(u) for u in _users_by_sid.values()}, key=str.lower)


def _online_usernames() -> set[str]:
    """Distinct signed-in users with at least one live connection (anonymous
    connections have no room to nudge, so they are excluded)."""
    return {u for u in _users_by_sid.values() if u}


def _allowed_rooms(user: str | None) -> set[str]:
    """Group membership (including admins), plus public for unscoped viewers."""
    scoped = _user_is_scoped(user)
    rooms = set() if scoped else {_PUBLIC_ROOM}
    if not user:
        return rooms
    try:
        with db.get_conn(db.db_path()) as conn:
            db._ensure_chat_schema(conn, db.db_path())
            groups = conn.execute("SELECT name FROM groups WHERE builtin = 0").fetchall()
            memberships = {r[0] for r in conn.execute(
                "SELECT groupname FROM group_members WHERE username = ?", (user,)
            )}
            excluded = {r[0] for r in conn.execute(
                "SELECT groupname FROM group_exclusions WHERE username = ?", (user,)
            )}
        rooms.update(
            f"group:{name}" for (name,) in groups
            if name not in {"everyone", "admin"} and not name.startswith("user:")
            and name in memberships and name not in excluded
        )
    except Exception:
        logger.exception("failed to load chat groups")
    return rooms


def _room_users(names, room: str) -> list[str]:
    if room == _PUBLIC_ROOM:
        return _unscoped(names)
    return [name for name in names if room in _allowed_rooms(name)]


async def _requested_room(sid, data) -> str | None:
    room = data.get("room", _PUBLIC_ROOM) if isinstance(data, dict) else _PUBLIC_ROOM
    if not isinstance(room, str):
        return None
    if room == _PUBLIC_ROOM:
        return None if (await _session_scoped(sid) or _user_is_scoped(await _session_user(sid))) else room
    return room if room in _allowed_rooms(await _session_user(sid)) else None


async def _message_room(sid, msg_id) -> str | None:
    # Check authorization before edit/delete/react, including guessed IDs.
    before = db.get_chat_message(msg_id)
    if not before or before.get("visible_to"):
        return None
    return await _requested_room(sid, {"room": before.get("room") or _PUBLIC_ROOM})


async def _refresh_room_memberships() -> None:
    # Existing sockets must stop receiving a room immediately after access is
    # revoked, without waiting for the browser to reconnect.
    for sid, old in list(_rooms_by_sid.items()):
        if sid not in _users_by_sid:
            continue
        new = _allowed_rooms(_users_by_sid[sid])
        if new == old:
            continue
        for room in old - new:
            await sio.leave_room(sid, room)
        for room in new - old:
            await sio.enter_room(sid, room)
        if sid not in _users_by_sid:
            continue
        _rooms_by_sid[sid] = new
        await sio.emit("chat_rooms", {"rooms": sorted(new)}, to=sid)


async def _emit_room(event, payload, *, room, **kwargs):
    await _refresh_room_memberships()
    await sio.emit(event, payload, room=room, **kwargs)


async def _broadcast_presence() -> None:
    await _refresh_room_memberships()
    rooms = {_PUBLIC_ROOM} | set().union(*_rooms_by_sid.values())
    for room in rooms:
        users = sorted({_display(u) for sid, u in _users_by_sid.items()
                        if room in _rooms_by_sid.get(sid, {_PUBLIC_ROOM})}, key=str.lower)
        await sio.emit("presence", {"count": len(users), "users": users, "room": room}, room=room)


def _known_users_map() -> dict[str, str]:
    """Lower-cased -> canonical username, cached briefly to avoid a DB hit per
    message."""
    now = time.time()
    if now - _known_cache["ts"] > 30:
        try:
            names = db.get_known_chat_usernames()
        except Exception:  # never let mention resolution break messaging
            logger.debug("get_known_chat_usernames failed", exc_info=True)
            names = []
        _known_cache["map"] = {n.lower(): n for n in names}
        _known_cache["ts"] = now
    return _known_cache["map"]


def _parse_mentions(text: str, room: str = _PUBLIC_ROOM) -> list[str]:
    """Canonical mentions authorized for this room, excluding command tokens."""
    known = _known_users_map()
    found: list[str] = []
    for raw in _MENTION_RE.findall(text):
        low = raw.lower()
        if low in _RESERVED_MENTIONS:
            continue
        canon = known.get(low)
        if canon and canon not in found:
            found.append(canon)
    return _room_users(found, room)


def _nudge_targets(names, sender: str | None, room: str = _PUBLIC_ROOM) -> list[str]:
    """Notify only other users authorized for this room."""
    return _room_users((n for n in names if n != sender), room)


def _rate_key(sid: str, user: str | None) -> str:
    """A signed-in user's budget follows them across sockets, so reconnecting
    does not reset it (#273); an anonymous one is per connection."""
    return f"user:{user}" if user else f"sid:{sid}"


def _rate_ok(key: str) -> bool:
    dq = _rate.setdefault(key, deque())
    now = time.time()
    while dq and now - dq[0] > _RATE_WINDOW_S:
        dq.popleft()
    if len(dq) >= _RATE_MAX:
        return False
    dq.append(now)
    return True


async def _session_user(sid: str) -> str | None:
    try:
        session = await sio.get_session(sid)
        return (session or {}).get("user")
    except KeyError:
        return None


async def _session_scoped(sid: str) -> bool:
    try:
        session = await sio.get_session(sid)
        return bool((session or {}).get("scoped"))
    except KeyError:
        return False


def _user_is_scoped(user: str | None) -> bool:
    """Fail closed: when the lookup errors, treat the user as scoped."""
    if not user:
        return False
    try:
        return access.is_scoped(db.db_path(), user)
    except Exception:
        logger.exception("scope lookup failed for %s; treating as scoped", user)
        return True


def _unscoped(names) -> list[str]:
    """*names* minus scoped viewers: a mention carries the shared-room text, so
    it must never reach a user who has no part in that room."""
    return [n for n in names if not _user_is_scoped(n)]


# ----- connection lifecycle ------------------------------------------------
@sio.event
async def connect(sid, environ):
    global _LOOP
    if _LOOP is None:
        _LOOP = asyncio.get_running_loop()
    user, identity = _resolve_identity(environ)
    if identity == UNKNOWN_EMAIL:
        logger.warning("rejected chat connection %s: unmapped Access identity", sid)
        raise SocketRefusedError("account not provisioned")
    if identity == ADMIN_EMAIL:
        logger.warning("rejected chat connection %s: Access identity of an admin", sid)
        raise SocketRefusedError("admin accounts sign in over SSH")
    # Fail closed exactly like the HTTP middleware. socket.io is mounted as an
    # ASGI wrapper *in front of* the app, so /socket.io never passes through
    # that middleware: without this, a peer bypassing nginx (straight to the
    # loopback port) is handed the whole persisted chat history and can post as
    # "Anonymous", even with MUSCAT_REQUIRE_AUTH set.
    if user is None and authentication_required():
        logger.warning("rejected unauthenticated chat connection %s", sid)
        raise SocketRefusedError("authentication required")
    scoped = _user_is_scoped(user)
    await sio.save_session(sid, {"user": user, "scoped": scoped})
    _users_by_sid[sid] = user
    if user:
        await sio.enter_room(sid, f"user:{user}")
    rooms = _allowed_rooms(user)
    _rooms_by_sid[sid] = rooms
    for room in sorted(rooms):
        await sio.enter_room(sid, room)
    await sio.emit("chat_rooms", {"rooms": sorted(rooms)}, to=sid)
    # Backfill recent history to this client only (never persisted here).
    # When the user is authenticated, scoped messages (e.g. job-finished
    # notifications) are included only for their owner.
    until = time.time()
    since = until - db.CHAT_BACKFILL_DAYS * 86400
    payload = {"messages": []}
    try:
        # Read one extra row to distinguish a complete window from truncation.
        history = db.get_recent_chat_messages(
            user=user, addressed_only=_PUBLIC_ROOM not in rooms,
            limit=501, since=since, until=until,
            rooms=sorted(rooms - {_PUBLIC_ROOM}),
        )
        window = {"since": since, "until": until}
        if len(history) > 500:
            history = history[-500:]
            window.update(since=history[0]["ts"], first_id=history[0]["id"])
        payload = {"messages": history, "window": window}
    except Exception:
        logger.exception("failed to load chat history")
        # No authoritative window on failure: the client must keep its view.
    await sio.emit("history", payload, to=sid)
    await _broadcast_presence()


@sio.event
async def disconnect(sid):
    _users_by_sid.pop(sid, None)
    _rooms_by_sid.pop(sid, None)
    # Only the anonymous budget goes; a user's outlives the socket on purpose.
    _rate.pop(_rate_key(sid, None), None)
    # Anonymous @bot context is per-connection; drop it so it can't leak. A
    # signed-in user's context is keyed by name (shared across tabs) and kept
    # until the idle-gap TTL expires, so a reload doesn't wipe their thread.
    _agent_ctx.pop(f"sid:{sid}", None)
    await _cancel_agent_requests(f"sid:{sid}")
    _agent_locks.pop(f"sid:{sid}", None)
    await _broadcast_presence()


# ----- messaging -----------------------------------------------------------
@sio.on("message")
async def message(sid, data):
    if not isinstance(data, dict):
        return
    user = await _session_user(sid)
    if not _rate_ok(_rate_key(sid, user)):
        return
    text = data.get("text")
    if not isinstance(text, str):
        return
    text = text.strip()
    if not text:
        return
    text = text[:_MAX_TEXT_LEN]

    display = _display(user)

    # Public and group posts require membership. The private @bot exchange
    # remains available even when the user has no shared room.
    # ``@test`` is checked first because it broadcasts to the selected room
    # even when the text also contains ``@bot``.
    room = await _requested_room(sid, data)
    if room is None and (
        _TEST_PREFIX_RE.match(text) or not chat_agent.addressed(text)
    ):
        await sio.emit(
            "chat_error", {"error": "the shared chat is not available for this account"}, to=sid
        )
        return

    # @test → ephemeral: broadcast live, never persisted, not editable.
    if _TEST_PREFIX_RE.match(text):
        body = _TEST_PREFIX_RE.sub("", text, count=1) or "(test message)"
        await _emit_room(
            "message",
            {
                "id": None,
                "user": display,
                "text": body,
                "kind": "ephemeral",
                "room": None if room == _PUBLIC_ROOM else room,
                "ephemeral": True,
                "ts": time.time(),
                "sid": sid,
                "mentions": [],
                "reactions": [],
                "edited": False,
            },
            room=room,
        )
        return

    # @bot → private exchange: neither the question nor the reply is persisted or
    # broadcast; both are delivered only to the asker (all their tabs + pop-out).
    if chat_agent.addressed(text):
        await sio.emit(
            "message",
            {
                "id": None,
                "user": display,
                "text": text,
                "kind": "user",
                "ephemeral": False,
                "private": True,
                "ts": time.time(),
                "sid": sid,
                "mentions": [],
                "reactions": [],
                "edited": False,
            },
            **_private_target(user, sid),
        )
        _spawn_agent(sid, user, chat_agent.extract_question(text))
        return

    # /me <action> → IRC-style status line, attributed and broadcast.
    m = _ME_PREFIX_RE.match(text)
    if m:
        action = text[m.end() :].strip()
        if not action:
            return
        mentions = _parse_mentions(action, room)
        try:
            msg = db.save_chat_message(
                display, action, mentions=mentions, kind="action",
                room=None if room == _PUBLIC_ROOM else room,
            )
        except Exception:
            logger.exception("failed to persist /me action")
            await sio.emit("chat_error", {"error": "message could not be saved"}, to=sid)
            return
        payload = {**msg, "sid": sid}
        await _emit_room("message", payload, room=room)
        for name in _nudge_targets(mentions, user, room):
            await sio.emit("mention", {"from": display, "message": payload}, room=f"user:{name}")
        return

    mentions = _parse_mentions(text, room)
    try:
        msg = db.save_chat_message(
            display, text, mentions=mentions, kind="user",
            room=None if room == _PUBLIC_ROOM else room,
        )
    except Exception:
        logger.exception("failed to persist chat message")
        await sio.emit("chat_error", {"error": "message could not be saved"}, to=sid)
        return

    payload = {**msg, "sid": sid}
    await _emit_room("message", payload, room=room)
    # Nudge each mentioned user (on any page/tab) so they notice.
    for name in _nudge_targets(mentions, user, room):
        await sio.emit("mention", {"from": display, "message": payload}, room=f"user:{name}")
    # @all / @everyone / @channel → ping every other online teammate too.
    if _BROADCAST_MENTION_RE.search(text):
        for name in _room_users(_online_usernames() - {user} - set(mentions), room):
            await sio.emit("mention", {"from": display, "message": payload}, room=f"user:{name}")


@sio.on("edit_message")
async def edit_message(sid, data):
    if not isinstance(data, dict):
        return
    user = await _session_user(sid)
    if not user:
        return
    msg_id = data.get("id")
    text = data.get("text")
    if not isinstance(msg_id, int) or not isinstance(text, str):
        return
    text = text.strip()[:_MAX_TEXT_LEN]
    if not text:
        return
    room = await _message_room(sid, msg_id)
    if room is None:
        return
    before = db.get_chat_message(msg_id)
    mentions = _parse_mentions(text, room)
    updated = db.edit_chat_message(msg_id, user, text, mentions=mentions)
    if updated is None:
        await sio.emit("chat_error", {"error": "not allowed to edit that message"}, to=sid)
        return
    await _emit_room("message_edited", updated, room=room)
    # Nudge only names the edit added; anyone already mentioned was notified
    # when the message was first sent.
    already = set((before or {}).get("mentions") or [])
    payload = {**updated, "sid": sid}
    for name in _nudge_targets((m for m in mentions if m not in already), user, room):
        await sio.emit("mention", {"from": user, "message": payload}, room=f"user:{name}")


@sio.on("delete_message")
async def delete_message(sid, data):
    if not isinstance(data, dict):
        return
    user = await _session_user(sid)
    if not user:
        return
    msg_id = data.get("id")
    if not isinstance(msg_id, int):
        return
    room = await _message_room(sid, msg_id)
    if room is None:
        return
    if db.delete_chat_message(msg_id, user):
        await _emit_room("message_deleted", {"id": msg_id}, room=room)
    else:
        await sio.emit("chat_error", {"error": "not allowed to delete that message"}, to=sid)


@sio.on("toggle_reaction")
async def toggle_reaction(sid, data):
    if not isinstance(data, dict):
        return
    user = await _session_user(sid)
    if not user:  # reactions require an identity
        return
    msg_id = data.get("id")
    emoji = data.get("emoji")
    if not isinstance(msg_id, int) or not isinstance(emoji, str):
        return
    emoji = emoji.strip()[:_MAX_EMOJI_LEN]
    if not emoji:
        return
    room = await _message_room(sid, msg_id)
    if room is None:
        return
    result = db.toggle_chat_reaction(msg_id, user, emoji)
    if result is not None:
        await _emit_room("reaction_updated", result, room=room)


@sio.on("typing")
async def typing(sid, data):
    room = await _requested_room(sid, data)
    if room is None:
        return
    user = await _session_user(sid)
    is_typing = bool(isinstance(data, dict) and data.get("typing"))
    # Relay to everyone except the sender; ephemeral, never stored.
    await _emit_room(
        "typing", {"user": _display(user), "typing": is_typing, "room": room}, room=room, skip_sid=sid
    )


# ----- codebase assistant (@bot): private, per-user, in-memory --------------
# The @bot exchange is private to the asker: neither the question nor the reply
# is persisted or broadcast — both are delivered only to the asker's own
# connections (every tab + the pop-out window). Conversation context is
# therefore held in memory per user, never written to the shared chat history.
#
# Recent turns handed to the model for follow-up context.
_AGENT_HISTORY_TURNS = 8
# Idle-gap auto-clear: a conversation untouched for this many seconds is dropped,
# so an idle gap resets @bot on its own. 0/negative disables the time cutoff.
_DEFAULT_HISTORY_TTL_S = 900.0
# Re-emit the "typing" indicator this often while the model works, so it does
# not hit the client's ~4s auto-clear during a long (multi-second) generation.
_AGENT_TYPING_HEARTBEAT_S = 2.0
# Keep a strong reference to in-flight agent tasks so they are not garbage
# collected before completion (asyncio only holds a weak reference).
_agent_tasks: set[asyncio.Task] = set()
# Per-conversation context for the private @bot exchange, keyed by ``_ctx_key``
# (the nginx user, or the socket id for anonymous sessions). Each value is a
# ``(last_activity_ts, deque-of-turns)`` pair; the deque caps its own length.
_agent_ctx: dict[str, tuple[float, deque]] = {}
# One in-flight exchange per conversation: a second question from the same user
# waits for the first, so its context includes the first answer.
_agent_locks: dict[str, asyncio.Lock] = {}
# Accepted requests, including those waiting for their conversation lock.
_agent_requests: dict[str, set[asyncio.Task]] = {}


def _forget_agent_request(key: str, task: asyncio.Task) -> None:
    requests = _agent_requests.get(key)
    if requests is not None:
        requests.discard(task)
        if not requests:
            _agent_requests.pop(key, None)


async def _cancel_agent_requests(key: str) -> None:
    tasks = list(_agent_requests.pop(key, ()))
    for task in tasks:
        task.cancel()
    if tasks:
        # Wait for capacity reservations to be released before accepting a
        # fresh question after reset. Cancelled answers cannot restore context.
        await asyncio.gather(*tasks, return_exceptions=True)


def _ctx_key(user: str | None, sid: str) -> str:
    """A stable conversation key: the nginx user (shared across their tabs and
    surviving reconnects within the TTL) or the socket id when anonymous."""
    return f"user:{user}" if user else f"sid:{sid}"


def _history_ttl_s() -> float:
    try:
        return float(os.environ.get("MUSCAT_AGENT_HISTORY_TTL_S", _DEFAULT_HISTORY_TTL_S))
    except (TypeError, ValueError):
        return _DEFAULT_HISTORY_TTL_S


def _agent_ctx_get(key: str) -> list[dict]:
    """This conversation's recent turns, or ``[]`` after an idle-gap TTL reset."""
    entry = _agent_ctx.get(key)
    if entry is None:
        return []
    last, turns = entry
    ttl = _history_ttl_s()
    if ttl > 0 and time.time() - last > ttl:
        _agent_ctx.pop(key, None)
        return []
    return list(turns)


def _agent_ctx_add(key: str, user_turn: dict, assistant_turn: dict) -> None:
    entry = _agent_ctx.get(key)
    turns = entry[1] if entry is not None else deque(maxlen=_AGENT_HISTORY_TURNS)
    turns.append(user_turn)
    turns.append(assistant_turn)
    _agent_ctx[key] = (time.time(), turns)


def _agent_ctx_clear(key: str) -> None:
    _agent_ctx.pop(key, None)


def _spawn_agent(sid: str, user: str | None, question: str) -> None:
    """Answer in the background so the message handler returns at once (the model
    call can take many seconds). Called from a socket.io handler, so there is
    always a running loop to schedule onto."""
    task = asyncio.create_task(_run_agent(sid, user, question))
    _agent_tasks.add(task)
    task.add_done_callback(_agent_tasks.discard)
    if question and not chat_agent.is_reset_command(question):
        # Register immediately: reset may arrive before this task first runs.
        key = _ctx_key(user, sid)
        _agent_requests.setdefault(key, set()).add(task)
        task.add_done_callback(lambda done: _forget_agent_request(key, done))


def _private_agent_payload(text: str) -> dict:
    """A live-only, private @bot message (never persisted, shown only to the
    asker): the answer plus intro/busy/error notes. ``id`` is None so the client
    attaches no edit/delete/react controls; ``private`` drives the 'only you'
    badge."""
    return {
        "id": None,
        "user": chat_agent.DISPLAY_NAME,
        "text": text,
        "kind": "agent",
        "ephemeral": False,
        "private": True,
        "ts": time.time(),
        "sid": None,
        "mentions": [],
        "reactions": [],
        "edited": False,
    }


async def _typing_heartbeat(target: dict) -> None:
    """Keep @bot's typing indicator alive (only for the asker) until cancelled."""
    try:
        while True:
            await sio.emit("typing", {"user": chat_agent.DISPLAY_NAME, "typing": True}, **target)
            await asyncio.sleep(_AGENT_TYPING_HEARTBEAT_S)
    except asyncio.CancelledError:
        pass


async def _run_agent(sid: str, user: str | None, question: str) -> None:
    target = _private_target(user, sid)
    key = _ctx_key(user, sid)
    if not question:  # bare "@bot": short intro, no model call
        intro = (
            f"Hi! I'm @{chat_agent.AGENT_NAME}, the muscat-db codebase assistant. "
            "This chat is private to you — nobody else sees it. Ask me things like "
            f'"@{chat_agent.AGENT_NAME} how does the photometry job lifecycle work?" '
            f'("@{chat_agent.AGENT_NAME} /reset" starts a fresh conversation.)'
        )
        await sio.emit("message", _private_agent_payload(intro), **target)
        return
    if chat_agent.is_reset_command(question):  # "@bot /reset": clear context, no model call
        _agent_ctx_clear(key)
        await _cancel_agent_requests(key)
        await sio.emit(
            "message",
            _private_agent_payload("🧹 Cleared our conversation context. Starting fresh."),
            **target,
        )
        return
    # Reserve before waiting: queued exchanges count against capacity too.
    if not chat_agent.reserve():
        await sio.emit(
            "message",
            _private_agent_payload(
                "I'm still answering an earlier question — give me a moment and ask again."
            ),
            **target,
        )
        return
    task = asyncio.current_task()
    _agent_requests.setdefault(key, set()).add(task)
    heartbeat = asyncio.create_task(_typing_heartbeat(target))
    try:
        lock = _agent_locks.setdefault(key, asyncio.Lock())
        async with lock:
            try:
                reply = await chat_agent.answer(question, _agent_ctx_get(key))
                _agent_ctx_add(
                    key,
                    {"role": "user", "content": f"{_display(user)}: {question}"},
                    {"role": "assistant", "content": reply},
                )
                await sio.emit("message", _private_agent_payload(reply), **target)
            except Exception as e:
                logger.warning("chat agent request failed: %s", e, exc_info=True)
                await sio.emit(
                    "message",
                    _private_agent_payload(
                        "⚠️ Sorry, I couldn't reach the model right now. Please try again shortly."
                    ),
                    **target,
                )
    finally:
        chat_agent.release()
        _forget_agent_request(key, task)
        heartbeat.cancel()
        await asyncio.gather(heartbeat, return_exceptions=True)
        await sio.emit("typing", {"user": chat_agent.DISPLAY_NAME, "typing": False}, **target)


# ----- job-finished system messages ----------------------------------------
def _job_user_name(job_key: str) -> str | None:
    """Look up the user_name stored for a job key: "" for an anonymous job's
    row, None when the store holds no row or the lookup fails.

    Reads through the job store rather than the SQLite jobs table, so the
    owner is found under MUSCAT_CONTROL_PLANE=postgres too."""
    try:
        row = get_job_store().get(job_key)
    except Exception:
        logger.warning("failed to look up job user_name for %s", job_key, exc_info=True)
        return None
    return None if row is None else str(row.get("user_name") or "")


_JOB_LABELS = {
    "photometry": "Photometry",
    "transit_fit": "Transit fit",
    "archive": "LCO archive download",
}
# Result page each job type links to; the page itself offers "To Target".
_JOB_RESULT_PATHS = {"photometry": "/photometry", "transit_fit": "/transit-fit"}
_ARCHIVE_PAGE = "/lco/archive"


def _job_result_link(
    type_: str, target: str, inst: str, date: str, run_id: str, link: str
) -> str:
    """Relative URL of the page showing this job's outcome, or ""."""
    if type_ == "archive":
        return link if link.startswith("/") else _ARCHIVE_PAGE
    path = _JOB_RESULT_PATHS.get(type_)
    if not path or not (inst and date and target):
        return f"/target?name={urllib.parse.quote(target)}" if target else ""
    # Both pages canonicalize the target with spaces removed (307 otherwise).
    query = {"inst": inst, "date": date, "target": target.replace(" ", "")}
    if run_id:
        query["run"] = run_id
    return f"{path}?{urllib.parse.urlencode(query)}"


def on_job_finished(
    job_key: str,
    type_: str = "",
    target: str = "",
    inst: str = "",
    date: str = "",
    state: str = "",
    run_id: str = "",
    owner: str | None = None,
    link: str = "",
    **_,
) -> None:
    """jobs.fire_job_finished hook (runs in the job-sync thread). Persists a
    system message scoped to the job's owner and schedules its delivery.

    ``owner`` overrides the job-store lookup for jobs not persisted under
    ``job_key`` (LCO archive downloads), where "" means a known-anonymous job.
    Left as None, the owner comes from the job's row; an anonymous row goes to
    the public room, while a missing row or failed lookup drops the message
    rather than announce a possibly restricted target publicly. ``link`` is an
    archive job's own result URL (its photometry page once the dataset is
    ingested)."""
    label = _JOB_LABELS.get(type_, type_ or "Job")
    verb = {"done": "finished", "error": "failed", "cancelled": "was cancelled"}.get(state, state)
    where = " ".join(p for p in (inst, date) if p)
    parts = [f"{label} for {target}" if target else label, f"({where})" if where else "", verb]
    text = " ".join(p for p in parts if p)
    result_link = _job_result_link(type_, target, inst, date, run_id or "", link or "")
    if result_link:
        text += f" {result_link}"
    if owner is None:
        owner = _job_user_name(job_key)
        if owner is None:
            logger.warning("no owner found for job %s; dropping its chat notification", job_key)
            return
    _deliver_system_message(text, owner)


def _deliver_system_message(text: str, owner: str) -> None:
    """Persist a system message visible to *owner* (the public room when
    blank) and schedule its delivery."""
    try:
        msg = db.save_chat_message("system", text, kind="system", visible_to=owner or None)
    except Exception:
        logger.exception("failed to persist system chat message")
        return
    loop = _valid_loop()
    if loop is None:
        return
    payload = {**msg, "sid": None}
    if owner:
        asyncio.run_coroutine_threadsafe(sio.emit("message", payload, room=f"user:{owner}"), loop)
    else:
        asyncio.run_coroutine_threadsafe(
            _emit_room("message", payload, room=_PUBLIC_ROOM), loop
        )


_RUN_LABELS = {**_JOB_LABELS, "ttv_fit": "TTV fit"}
_RUN_VERBS = {
    "overwrite": "overwrote", "overwrite_queued": "queued an overwrite of",
    "delete": "deleted", "cancel": "cancelled",
}


def notify_run_changed(
    *, owner: str, actor: str | None, action: str, level: str,
    target: str, inst: str = "", date: str = "", run_id: str = "",
) -> None:
    """Tell a run's owner, privately, that someone else overwrote, deleted or
    cancelled it (issue #307). Nothing is sent for the owner's own change or a
    run with no owner, which has nobody to tell."""
    owner = (owner or "").strip()
    actor = (actor or "").strip()
    if not owner or owner.casefold() == actor.casefold():
        return
    label = _RUN_LABELS.get(level, level)
    where = " ".join(p for p in (inst, date) if p)
    text = (
        f"{actor or 'Someone'} {_RUN_VERBS.get(action, action)} your {label} run "
        f"'{run_id or 'default'}' for {target}" + (f" ({where})" if where else "")
    )
    # A deleted run has no page of its own left to open.
    link = _job_result_link(level, target, inst, date, "" if action == "delete" else run_id, "")
    if link:
        text += f" {link}"
    _deliver_system_message(text, owner)


# Register once at import so both pipelines' sync loops notify chat.
jobs.register_job_finished_hook(on_job_finished)
