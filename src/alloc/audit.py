"""Hash-chained, append-only audit log.

Each entry stores the hash of the previous entry. Changing any past entry breaks every hash after it,
so `verify()` detects tampering even by someone with direct database access. The database also refuses
UPDATE/DELETE on audit_log (triggers in schema.sql) – two independent layers.
"""
from __future__ import annotations

import hashlib
import json
import sqlite3
from datetime import datetime, timezone

GENESIS = "0" * 64


def _canonical(ts: str, actor: str, action: str, entity: str, payload_json: str, prev_hash: str) -> bytes:
    return json.dumps({"ts": ts, "actor": actor, "action": action, "entity": entity,
                       "payload": payload_json, "prev_hash": prev_hash},
                      sort_keys=True, separators=(",", ":")).encode()


def append(con: sqlite3.Connection, actor: str, action: str, entity: str, payload: dict) -> str:
    row = con.execute("SELECT hash FROM audit_log ORDER BY seq DESC LIMIT 1").fetchone()
    prev = row[0] if row else GENESIS
    ts = datetime.now(timezone.utc).isoformat(timespec="microseconds")
    payload_json = json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str)
    h = hashlib.sha256(_canonical(ts, actor, action, entity, payload_json, prev)).hexdigest()
    con.execute("INSERT INTO audit_log (ts, actor, action, entity, payload, prev_hash, hash) VALUES (?,?,?,?,?,?,?)",
                (ts, actor, action, entity, payload_json, prev, h))
    return h


def verify(con: sqlite3.Connection) -> tuple[bool, int | None, int]:
    """Return (ok, first_bad_seq, entries_checked)."""
    prev, n = GENESIS, 0
    for seq, ts, actor, action, entity, payload, prev_hash, h in con.execute(
            "SELECT seq, ts, actor, action, entity, payload, prev_hash, hash FROM audit_log ORDER BY seq"):
        n += 1
        if prev_hash != prev or hashlib.sha256(_canonical(ts, actor, action, entity, payload, prev_hash)).hexdigest() != h:
            return False, seq, n
        prev = h
    return True, None, n


def trail(con: sqlite3.Connection, entity: str) -> list[tuple]:
    return con.execute("SELECT seq, ts, actor, action, payload FROM audit_log WHERE entity=? ORDER BY seq",
                       (entity,)).fetchall()
