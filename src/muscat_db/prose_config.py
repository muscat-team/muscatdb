"""Frozen prose telescope configuration for production photometry (#101).

prose2 loads an explicit schema-1 configuration file read-only when
``PROSE_CONFIG_FILE`` is set, checks it against ``PROSE_CONFIG_SHA256``, and
exports both to its worker processes (jpdeleon/prose2#16). Production launches
the pinned engine with these variables pointing at the packaged snapshot, so
the engine never reads ``~/.prose``, development telescope symlinks or
downloads the Ballet model, and spawn/forkserver workers inherit the same file.
"""
from __future__ import annotations

import hashlib
from pathlib import Path

SNAPSHOT = Path(__file__).with_name("_prose_telescopes.yaml")
SNAPSHOT_SHA256 = "eea1de5458d32f1fff05f0ce35b24527d78d67bd327ea8dfa448a8d313bdd82d"


def snapshot_bytes(config_file: Path | None = None) -> bytes:
    data = (SNAPSHOT if config_file is None else config_file).read_bytes()
    if hashlib.sha256(data).hexdigest() != SNAPSHOT_SHA256:
        raise RuntimeError("frozen prose telescope snapshot checksum mismatch")
    return data


def frozen_env() -> dict[str, str]:
    """Variables that make prose load the packaged snapshot read-only.

    The checksum is verified here too, so a modified package fails before a
    job starts rather than inside the engine.
    """
    snapshot_bytes()
    return {"PROSE_CONFIG_FILE": str(SNAPSHOT), "PROSE_CONFIG_SHA256": SNAPSHOT_SHA256}
