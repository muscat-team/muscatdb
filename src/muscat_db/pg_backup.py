"""Nightly ``pg_dump`` of the Postgres control plane (issue #51 step C).

``muscat.db`` gets a verified nightly snapshot from :mod:`muscat_db.db_backup`
before every rebuild. The control plane (``muscatdb_prod``: the ``jobs`` and
``job_concurrency_slots`` tables a worker fleet shares) had none. This writes a
custom-format dump of it next to those snapshots, under the same directory and
retention (``MUSCAT_DB_BACKUP_DIR``, ``MUSCAT_DB_BACKUP_KEEP``), as
``<dbname>.nightly-<stamp>.dump``.

The dump goes to a ``.part`` file and is renamed into place only after
``pg_restore --list`` reads it back, so a truncated archive never replaces a
good one in the retention window. Only files this module names are pruned.

The password is handed to the tools through ``PGPASSWORD``, never on the
command line, where any user on the host could read it with ``ps``. Restore
with ``pg_restore --clean --if-exists -d <dbname> <file>``; see
notes/DEPLOYMENT.md, PostgreSQL control plane on ut2.
"""

from __future__ import annotations

import datetime
import logging
import os
import subprocess
from pathlib import Path
from urllib.parse import quote, unquote

from muscat_db.db_backup import _NIGHTLY_TAG, backup_dir, backup_keep

logger = logging.getLogger(__name__)

_SUFFIX = ".dump"
# Generous: the control plane is a few MB, but the backup disk is shared with
# the RAID that made a muscat.db snapshot take 3 h on 2026-10-20.
_TOOL_TIMEOUT_S = 3600
_URI_SCHEMES = ("postgresql://", "postgres://")


class ControlPlaneBackupError(RuntimeError):
    """The control-plane dump could not be written or verified."""


def _split_uri(dsn: str) -> tuple[str, str, str]:
    """:func:`_split_dsn` for the ``postgresql://`` form. Only the password
    is cut out; host list, port and query parameters go to libpq untouched."""
    scheme, rest = dsn.split("://", 1)
    rest, _, query = rest.partition("?")
    netloc, _, path = rest.partition("/")
    userinfo, at, hostspec = netloc.rpartition("@")
    password = ""
    if at:
        user, _, encoded_password = userinfo.partition(":")
        password = unquote(encoded_password)
        userinfo = user + "@"
    dbname = unquote(path)
    kept: list[str] = []
    for item in query.split("&") if query else []:
        name, _, value = item.partition("=")
        if unquote(name) == "password":
            password = unquote(value)
            continue
        if unquote(name) == "dbname":
            dbname = unquote(value)
        kept.append(item)
    conninfo = f"{scheme}://{userinfo}{hostspec}/{path}"
    if kept:
        conninfo += "?" + "&".join(kept)
    return conninfo, password, dbname


def _parse_keywords(dsn: str) -> list[tuple[str, str]]:
    """``key = value`` pairs of a libpq keyword/value conninfo: values are
    bare (backslash escapes) or single-quoted (``\\'`` and ``\\\\``)."""
    pairs: list[tuple[str, str]] = []
    i, n = 0, len(dsn)
    while True:
        while i < n and dsn[i].isspace():
            i += 1
        if i >= n:
            return pairs
        start = i
        while i < n and dsn[i] != "=" and not dsn[i].isspace():
            i += 1
        key = dsn[start:i]
        while i < n and dsn[i].isspace():
            i += 1
        if not key or i >= n or dsn[i] != "=":
            raise ValueError(f"missing '=' after {key!r}")
        i += 1
        while i < n and dsn[i].isspace():
            i += 1
        quoted = i < n and dsn[i] == "'"
        i += quoted
        value: list[str] = []
        while i < n:
            ch = dsn[i]
            if ch == "\\" and i + 1 < n:
                value.append(dsn[i + 1])
                i += 2
                continue
            if (quoted and ch == "'") or (not quoted and ch.isspace()):
                break
            value.append(ch)
            i += 1
        if quoted:
            if i >= n:
                raise ValueError(f"unterminated quoted value for {key!r}")
            i += 1
        pairs.append((key, "".join(value)))


def _split_keywords(dsn: str) -> tuple[str, str, str]:
    """:func:`_split_dsn` for the ``key=value`` form."""
    try:
        pairs = _parse_keywords(dsn)
    except ValueError as exc:
        raise ControlPlaneBackupError(f"MUSCAT_POSTGRES_DSN is not a valid connection string: {exc}") from None
    params = dict(pairs)
    password = params.pop("password", "")
    conninfo = " ".join(
        "{}='{}'".format(k, v.replace("\\", "\\\\").replace("'", "\\'")) for k, v in params.items()
    )
    return conninfo, password, params.get("dbname", "")


def _split_dsn(dsn: str) -> tuple[str, str, str]:
    """``(conninfo without password, password, dbname)`` for *dsn*, in
    either libpq form. Parsed here rather than with psycopg so the backup
    needs no 'cluster' extra: it only shells out to pg_dump."""
    if dsn.startswith(_URI_SCHEMES):
        conninfo, password, dbname = _split_uri(dsn)
    else:
        conninfo, password, dbname = _split_keywords(dsn)
    if not dbname:
        raise ControlPlaneBackupError("MUSCAT_POSTGRES_DSN names no database to dump")
    return conninfo, password, dbname


def _run(argv: list[str], env: dict[str, str], password: str) -> None:
    try:
        res = subprocess.run(
            argv, env=env, capture_output=True, text=True, timeout=_TOOL_TIMEOUT_S,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise ControlPlaneBackupError(f"{argv[0]} could not run: {exc}") from exc
    if res.returncode != 0:
        detail = (res.stderr or res.stdout or "").strip()
        if password:
            # Both spellings: a URI DSN carries the password percent-encoded.
            for secret in (password, quote(password, safe="")):
                detail = detail.replace(secret, "***")
        raise ControlPlaneBackupError(f"{argv[0]} exited {res.returncode}: {detail[-500:]}")


def prune_dumps(dbname: str, dest_dir: Path, keep: int) -> list[Path]:
    """Delete all but the newest *keep* nightly dumps of *dbname* in *dest_dir*.
    The stamp is fixed-width, so name order is chronological order."""
    prefix = dbname + _NIGHTLY_TAG
    dumps = sorted(p for p in dest_dir.glob(f"{prefix}*{_SUFFIX}") if p.is_file())
    removed: list[Path] = []
    for p in dumps[:-keep] if len(dumps) > keep else []:
        try:
            p.unlink()
        except OSError as exc:
            logger.warning("could not prune stale control-plane dump %s: %s", p, exc)
        else:
            removed.append(p)
    return removed


def dump_control_plane(
    dsn: str | None = None,
    *,
    dest_dir: Path | None = None,
    keep: int | None = None,
    now: datetime.datetime | None = None,
) -> Path:
    """Dump the control-plane database to *dest_dir*, verify it, prune old
    dumps, and return the new file's path. *dsn* defaults to
    ``MUSCAT_POSTGRES_DSN``. Raises :class:`ControlPlaneBackupError` on any
    failure, leaving no partial file behind."""
    dsn = dsn or os.environ.get("MUSCAT_POSTGRES_DSN", "")
    if not dsn:
        raise ControlPlaneBackupError("MUSCAT_POSTGRES_DSN is not set; nothing to back up")
    conninfo, password, dbname = _split_dsn(dsn)
    dest_dir = dest_dir if dest_dir is not None else backup_dir()
    keep = keep if keep is not None else backup_keep()
    dest_dir.mkdir(parents=True, exist_ok=True)

    stamp = (now or datetime.datetime.now()).strftime("%Y%m%d-%H%M%S")
    dest = dest_dir / f"{dbname}{_NIGHTLY_TAG}{stamp}{_SUFFIX}"
    part = dest.with_name(dest.name + ".part")
    env = {**os.environ, "PGPASSWORD": password} if password else dict(os.environ)
    try:
        _run(["pg_dump", "--format=custom", "--file", str(part), "--dbname", conninfo], env, password)
        _run(["pg_restore", "--list", str(part)], env, password)
        # Job params carry user inputs (e.g. TTV csv_content); the backup
        # directory is shared, so the dump is readable by its owner only.
        os.chmod(part, 0o600)
        os.replace(part, dest)
    except BaseException:
        part.unlink(missing_ok=True)
        raise
    logger.info("control-plane dump of %s written to %s", dbname, dest)
    prune_dumps(dbname, dest_dir, keep)
    return dest
