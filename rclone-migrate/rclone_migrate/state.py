"""Central job state DB: ~/.local/share/rclone-migrate/<job>/state.db.

Holds:
  - meta: timestamps, check_signature, negotiated hash algorithm
  - remote_hash_cache: only populated when a remote backend cannot supply
    hashes natively (rare; e.g. some GDrive paths or backends with empty
    Hashes feature list)
  - events: one row per rmig invocation (audit trail), with stdout captured
    in <state_dir>/<job>/runs/*.log
  - file_events: per-file changes/anomalies (copied/deleted/missing/failed)
    referencing events.id; matched-OK files are NOT recorded (implicit)
"""
from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Tuple


def open_db(state_dir: Path) -> sqlite3.Connection:
    state_dir.mkdir(parents=True, exist_ok=True)
    # check_same_thread=False: manifest._refresh_remote dispatches hashing
    # to a ThreadPoolExecutor and each worker thread flushes its batch to
    # remote_hash_cache. The callers serialize these writes via an explicit
    # lock (manifest.py rlock), so SQLite's thread-safety check would only
    # produce false positives.
    conn = sqlite3.connect(str(state_dir / "state.db"), check_same_thread=False)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("""
        CREATE TABLE IF NOT EXISTS meta (
            key TEXT PRIMARY KEY,
            val TEXT
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS remote_hash_cache (
            side       TEXT NOT NULL,
            path       TEXT NOT NULL,
            algorithm  TEXT NOT NULL,
            hash       TEXT NOT NULL,
            size       INTEGER NOT NULL,
            modtime    REAL,
            refreshed  REAL NOT NULL,
            PRIMARY KEY (side, path, algorithm)
        )
    """)
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_rhc_hash "
        "ON remote_hash_cache(side, algorithm, hash)"
    )
    # Audit log: one row per operation invocation
    conn.execute("""
        CREATE TABLE IF NOT EXISTS events (
            id          INTEGER PRIMARY KEY,
            started_ts  REAL    NOT NULL,
            ended_ts    REAL,
            op          TEXT    NOT NULL,
            result      TEXT,
            algo        TEXT,
            signature   TEXT,
            src_count   INTEGER,
            dst_count   INTEGER,
            affected    INTEGER,
            log_path    TEXT,
            pid         INTEGER,
            hostname    TEXT,
            notes       TEXT
        )
    """)
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_events_ts ON events(started_ts DESC)"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_events_op ON events(op, result)"
    )
    # Per-file outcomes worth auditing (changes + anomalies only — not OKs)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS file_events (
            event_id  INTEGER NOT NULL REFERENCES events(id),
            side      TEXT    NOT NULL,
            path      TEXT    NOT NULL,
            hash      TEXT,
            outcome   TEXT    NOT NULL,
            detail    TEXT,
            PRIMARY KEY (event_id, side, path, outcome)
        )
    """)
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_fe_path ON file_events(side, path)"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_fe_outcome ON file_events(outcome)"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_fe_hash ON file_events(hash)"
    )
    conn.commit()
    return conn


# --- events / file_events ---

def event_start(
    conn: sqlite3.Connection,
    *,
    op: str,
    log_path: Optional[str],
    pid: int,
    hostname: str,
    started_ts: Optional[float] = None,
) -> int:
    """Insert an in-progress event row; return its id."""
    import time as _time
    ts = started_ts if started_ts is not None else _time.time()
    cur = conn.execute(
        "INSERT INTO events (started_ts, op, log_path, pid, hostname) "
        "VALUES (?, ?, ?, ?, ?)",
        (ts, op, log_path, pid, hostname),
    )
    conn.commit()
    return int(cur.lastrowid)


def event_finish(
    conn: sqlite3.Connection,
    event_id: int,
    *,
    result: str,
    algo: Optional[str] = None,
    signature: Optional[str] = None,
    src_count: Optional[int] = None,
    dst_count: Optional[int] = None,
    affected: Optional[int] = None,
    notes: Optional[str] = None,
    ended_ts: Optional[float] = None,
) -> None:
    import time as _time
    ts = ended_ts if ended_ts is not None else _time.time()
    conn.execute(
        "UPDATE events SET ended_ts=?, result=?, algo=?, signature=?, "
        "src_count=?, dst_count=?, affected=?, notes=? WHERE id=?",
        (ts, result, algo, signature, src_count, dst_count, affected,
         notes, event_id),
    )
    conn.commit()


def detect_orphans(conn: sqlite3.Connection) -> List[int]:
    """Find started-but-not-finished events; mark them 'crashed' and return ids."""
    rows = conn.execute(
        "SELECT id FROM events WHERE ended_ts IS NULL AND result IS NULL"
    ).fetchall()
    ids = [row[0] for row in rows]
    if ids:
        import time as _time
        ts = _time.time()
        conn.executemany(
            "UPDATE events SET ended_ts=?, result='crashed' WHERE id=?",
            [(ts, i) for i in ids],
        )
        conn.commit()
    return ids


def record_file_event(
    conn: sqlite3.Connection,
    event_id: int,
    *,
    side: str,
    path: str,
    outcome: str,
    hash: Optional[str] = None,
    detail: Optional[str] = None,
) -> None:
    conn.execute(
        "INSERT OR REPLACE INTO file_events "
        "(event_id, side, path, hash, outcome, detail) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        (event_id, side, path, hash, outcome, detail),
    )
    conn.commit()


def record_file_events_batch(
    conn: sqlite3.Connection,
    event_id: int,
    rows: Iterable[dict],
) -> None:
    """Bulk insert. Each row dict: side, path, outcome; optional hash, detail."""
    payload = [
        (event_id, r["side"], r["path"], r.get("hash"), r["outcome"],
         r.get("detail"))
        for r in rows
    ]
    if not payload:
        return
    conn.executemany(
        "INSERT OR REPLACE INTO file_events "
        "(event_id, side, path, hash, outcome, detail) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        payload,
    )
    conn.commit()


def query_events(
    conn: sqlite3.Connection,
    *,
    op: Optional[str] = None,
    result: Optional[str] = None,
    limit: int = 50,
) -> List[dict]:
    sql = "SELECT * FROM events"
    clauses, args = [], []
    if op:
        clauses.append("op = ?")
        args.append(op)
    if result:
        clauses.append("result = ?")
        args.append(result)
    if clauses:
        sql += " WHERE " + " AND ".join(clauses)
    sql += " ORDER BY started_ts DESC LIMIT ?"
    args.append(int(limit))
    cur = conn.execute(sql, args)
    cols = [d[0] for d in cur.description]
    return [dict(zip(cols, row)) for row in cur.fetchall()]


def query_file_events(
    conn: sqlite3.Connection,
    *,
    side: Optional[str] = None,
    path: Optional[str] = None,
    hash: Optional[str] = None,
    limit: int = 50,
) -> List[dict]:
    sql = (
        "SELECT fe.event_id, fe.side, fe.path, fe.hash, fe.outcome, "
        "fe.detail, e.started_ts, e.op, e.result "
        "FROM file_events fe JOIN events e ON e.id = fe.event_id"
    )
    clauses, args = [], []
    if side:
        clauses.append("fe.side = ?")
        args.append(side)
    if path:
        clauses.append("fe.path = ?")
        args.append(path)
    if hash:
        clauses.append("fe.hash = ?")
        args.append(hash)
    if clauses:
        sql += " WHERE " + " AND ".join(clauses)
    sql += " ORDER BY e.started_ts DESC LIMIT ?"
    args.append(int(limit))
    cur = conn.execute(sql, args)
    cols = [d[0] for d in cur.description]
    return [dict(zip(cols, row)) for row in cur.fetchall()]


# --- meta ---

def meta_get(conn: sqlite3.Connection, key: str) -> Optional[str]:
    row = conn.execute("SELECT val FROM meta WHERE key = ?", (key,)).fetchone()
    return row[0] if row else None


def meta_set(conn: sqlite3.Connection, key: str, val: str) -> None:
    conn.execute(
        "INSERT INTO meta (key, val) VALUES (?, ?) "
        "ON CONFLICT(key) DO UPDATE SET val = excluded.val",
        (key, val),
    )
    conn.commit()


def meta_clear(conn: sqlite3.Connection, key: str) -> None:
    conn.execute("DELETE FROM meta WHERE key = ?", (key,))
    conn.commit()


# --- remote_hash_cache (used only when a remote backend has no native hash) ---

@dataclass
class RemoteCacheEntry:
    side: str
    path: str
    algorithm: str
    hash: str
    size: int
    modtime: Optional[float]


def rhc_load(
    conn: sqlite3.Connection, side: str, algorithm: str
) -> Dict[str, RemoteCacheEntry]:
    rows = conn.execute(
        "SELECT path, hash, size, modtime FROM remote_hash_cache "
        "WHERE side = ? AND algorithm = ?",
        (side, algorithm),
    ).fetchall()
    return {
        row[0]: RemoteCacheEntry(
            side=side, path=row[0], algorithm=algorithm,
            hash=row[1], size=row[2], modtime=row[3],
        )
        for row in rows
    }


def rhc_upsert(
    conn: sqlite3.Connection,
    entries: Iterable[RemoteCacheEntry],
    refreshed: float,
) -> None:
    payload = [
        (e.side, e.path, e.algorithm, e.hash, e.size, e.modtime, refreshed)
        for e in entries
    ]
    if not payload:
        return
    conn.executemany(
        "INSERT OR REPLACE INTO remote_hash_cache "
        "(side, path, algorithm, hash, size, modtime, refreshed) "
        "VALUES (?, ?, ?, ?, ?, ?, ?)",
        payload,
    )
    conn.commit()


def rhc_delete(
    conn: sqlite3.Connection, side: str, paths: Iterable[str], algorithm: str
) -> None:
    payload = [(side, p, algorithm) for p in paths]
    if not payload:
        return
    conn.executemany(
        "DELETE FROM remote_hash_cache WHERE side = ? AND path = ? AND algorithm = ?",
        payload,
    )
    conn.commit()
