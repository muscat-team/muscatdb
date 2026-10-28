"""Admin-only account and access APIs; no system privileges or external API calls."""
from dataclasses import asdict
import logging
import re
import sqlite3
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, ConfigDict, Field

from muscat_db import access, access_groups as groups, auth
from muscat_db.database import db_path, get_conn
from muscat_db.instruments import INSTRUMENTS

router = APIRouter(prefix="/api/admin", tags=["administration"])
logger = logging.getLogger(__name__)


def require_admin(request: Request) -> str:
    user = auth.request_user(request)
    if not user or not access.is_admin_user(db_path(), user):
        raise HTTPException(403, "Administrator access required")
    if request.method != "GET" and not auth.is_same_origin(request):
        raise HTTPException(403, "Same-origin request required")
    return user


class Change(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    action: Literal["account", "clear_email", "scope", "group", "join", "leave", "grant", "revoke"]
    username: str = Field(default="", max_length=128)
    email: str = Field(default="", max_length=254)
    group: str = Field(default="", max_length=140)
    scoped: bool = True
    proposal_id: str | None = Field(default=None, max_length=128)
    instrument: str | None = None
    target: str | None = Field(default=None, max_length=256)
    date_from: str | None = None
    date_to: str | None = None
    note: str = Field(default="", max_length=1000)
    grant_id: int = Field(default=0, ge=0)


@router.get("/state")
def state(actor: str = Depends(require_admin)):
    with get_conn(db_path()) as conn:
        users = [dict(zip(("username", "email", "admin", "last_login", "scoped"), row))
                 for row in conn.execute(
                     "SELECT username, email, is_admin, last_login, "
                     "EXISTS(SELECT 1 FROM group_exclusions e WHERE e.username=users.username "
                     "AND e.groupname='everyone') FROM users ORDER BY username")]
    return {"users": users, "groups": groups.list_groups(db_path()), "instruments": sorted(INSTRUMENTS)}


@router.post("/change")
def change(body: Change, actor: str = Depends(require_admin)):
    try:
        with get_conn(db_path()) as conn:
            conn.execute("BEGIN IMMEDIATE")
            if not conn.execute("SELECT 1 FROM users WHERE username=? AND is_admin=1", (actor,)).fetchone():
                raise HTTPException(403, "Administrator access required")
            groups.ensure_seeded(conn)
            user = conn.execute("SELECT is_admin FROM users WHERE username=?", (body.username,)).fetchone()
            if body.action in {"account", "clear_email", "scope", "join", "leave"}:
                if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,127}", body.username):
                    raise ValueError("Username must contain letters, digits, dots, underscores or hyphens")
                if body.action != "account" and not user:
                    raise ValueError("Unknown account")
                if user and user[0]:
                    raise ValueError("Admin accounts must be managed through the CLI")
            if body.action == "account":
                email = auth.normalize_email(body.email)
                if email is None:
                    raise ValueError("Enter a valid email address")
                owner = conn.execute("SELECT username FROM users WHERE lower(email)=?", (email,)).fetchone()
                if owner and owner[0] != body.username:
                    raise ValueError("Email already belongs to another account")
                conn.execute("INSERT OR IGNORE INTO users (username, display_name) VALUES (?, ?)",
                             (body.username, body.username))
                conn.execute("UPDATE users SET email=? WHERE username=?", (email, body.username))
                if not user:
                    groups.set_excluded(conn, groups.EVERYONE, body.username, True)
                    conn.execute("UPDATE users SET scoped=1 WHERE username=?", (body.username,))
            elif body.action == "clear_email":
                conn.execute("UPDATE users SET email=NULL WHERE username=?", (body.username,))
            elif body.action == "scope":
                groups.set_excluded(conn, groups.EVERYONE, body.username, body.scoped)
                conn.execute("UPDATE users SET scoped=? WHERE username=?", (body.scoped, body.username))
            elif body.action == "group":
                problem = groups.check_group_name(body.group)
                if problem:
                    raise ValueError(problem)
                if not groups.ensure_group(conn, body.group, body.note):
                    raise ValueError("Group already exists")
            elif body.action in {"join", "leave"}:
                if groups.check_group_name(body.group) or not groups.group_exists(conn, body.group):
                    raise ValueError("Select an existing custom group")
                if body.action == "join":
                    groups.add_member(conn, body.group, body.username, actor)
                else:
                    groups.remove_member(conn, body.group, body.username)
            elif body.action == "grant":
                if body.group.lower() == groups.ADMIN:
                    raise ValueError("Admins already see all data")
                if body.group.startswith(groups.USER_PREFIX):
                    username = body.group[len(groups.USER_PREFIX):]
                    if not conn.execute("SELECT 1 FROM users WHERE username=?", (username,)).fetchone():
                        raise ValueError("Unknown account")
                    groups.ensure_group(conn, body.group, builtin=True)
                    groups.add_member(conn, body.group, username, actor)
                elif not groups.group_exists(conn, body.group):
                    raise ValueError("Unknown group")
                selector = groups.check_selector(
                    proposal_id=body.proposal_id, instrument=body.instrument, target=body.target,
                    date=None, date_from=body.date_from or None, date_to=body.date_to or None,
                    instruments=INSTRUMENTS)
                if not groups.add_grant(conn, body.group, **asdict(selector), note=body.note, granted_by=actor):
                    raise ValueError("Equivalent grant already exists")
            elif body.action == "revoke":
                if groups.revoke_grant(conn, body.grant_id, actor) is None:
                    raise ValueError("Grant no longer exists; refresh the dashboard")
            conn.commit()
    except (ValueError, sqlite3.IntegrityError) as exc:
        raise HTTPException(400, str(exc)) from exc
    logger.info("admin change actor=%s action=%s user=%s group=%s grant=%s",
                actor, body.action, body.username, body.group, body.grant_id)
    return {"ok": True}
