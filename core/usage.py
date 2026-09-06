"""Private, best-effort upstream generation POST ledger (never quota enforcement).

Only attempted generation POSTs count; metadata GETs do not. Daily calls and RPM
share a UTC cutoff within 30 seconds of the collector's clock. Tokens are the
latest known cumulative usage of those attempts, not usage at historical time.
SQLite stores no keys, prompts, models, response bodies, or network addresses.
No daemon: 31 UTC days of aggregates and at most 100000 recent attempt records.
"""
import hashlib
import json
import math
import os
import sqlite3
import threading
import time
import uuid
from datetime import datetime, timezone
from contextlib import contextmanager
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit

from core.paths import DATA_DIR

_ledger = None  # Explicitly enabled by real service / management entry points.
WRITE_BUSY_TIMEOUT = 0.025  # Accounting must not hold up upstream POSTs / SSE.


def credential_id(base_url, key):
    parsed = urlsplit(base_url.strip())
    host = (parsed.hostname or "").lower()
    if ":" in host:
        host = "[" + host + "]"
    port = parsed.port
    if port and not ((parsed.scheme.lower() == "https" and port == 443) or (parsed.scheme.lower() == "http" and port == 80)):
        host += ":" + str(port)
    canonical = urlunsplit((parsed.scheme.lower(), host, parsed.path.rstrip("/"), parsed.query, ""))
    return hashlib.sha256((canonical + "\0" + key).encode("utf-8")).hexdigest()


def _day(timestamp):
    return datetime.fromtimestamp(timestamp, timezone.utc).strftime("%Y-%m-%d")


def _valid_tokens(payload):
    raw = payload.get("usage") if isinstance(payload, dict) else None
    total = raw.get("total_tokens") if isinstance(raw, dict) else None
    return total if isinstance(total, int) and not isinstance(total, bool) and 0 <= total <= 2**63 - 1 else None


class Attempt:
    """A request-local handle keeps late stream usage tied to the original day/key.

    After recent records expire, its cumulative maximum still makes repeated SSE
    usage idempotent. Handles never cross processes or get reconstructed by ID.
    """
    def __init__(self, ledger, attempt_id, provider, identity, day):
        self.ledger = ledger
        self.id = attempt_id
        self.provider = provider
        self.credential_id = identity
        self.day = day
        self.tokens = None
        self.lock = threading.Lock()

    def observe(self, payload):
        tokens = _valid_tokens(payload)
        if tokens is None:
            return
        with self.lock:
            if self.tokens is not None and tokens <= self.tokens:
                return
            try:
                with self.ledger._connect() as db:
                    db.execute("BEGIN IMMEDIATE")
                    stored = db.execute("SELECT tokens FROM attempts WHERE id=?", (self.id,)).fetchone()
                    previous = stored["tokens"] if stored is not None else self.tokens
                    if previous is None or tokens > previous:
                        db.execute("UPDATE daily SET tokens=tokens+?, unknown_tokens=unknown_tokens-? WHERE day=? AND provider=? AND credential_id=?",
                                   (tokens - (previous or 0), int(previous is None), self.day, self.provider, self.credential_id))
                        db.execute("UPDATE attempts SET tokens=? WHERE id=?", (tokens, self.id))
                    else:
                        tokens = previous
                self.tokens = tokens
            except (OSError, sqlite3.Error, OverflowError):
                self.ledger.mark_gap()


class UsageLedger:
    def __init__(self, directory=None, clock=time.time, max_recent=100000):
        self.directory = Path(directory or os.path.join(DATA_DIR, "usage"))
        self.db_path = str(self.directory / "usage.sqlite3")
        self.clock = clock
        self.max_recent = max_recent
        self._gap = None

    @contextmanager
    def _connect(self, readonly=False):
        if readonly:
            db = sqlite3.connect(Path(self.db_path).as_uri() + "?mode=ro", uri=True, timeout=5)
        else:
            # A removed database must not silently recreate an empty ledger.
            db = sqlite3.connect(Path(self.db_path).as_uri() + "?mode=rw", uri=True, timeout=WRITE_BUSY_TIMEOUT)
        db.row_factory = sqlite3.Row
        try:
            with db:
                yield db
        finally:
            db.close()

    def initialize(self):
        """Initialize at an instrumented service's first attempt, never on reads."""
        import fcntl
        self.directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        os.chmod(self.directory, 0o700)
        fd = os.open(str(self.directory / "initialize.lock"), os.O_CREAT | os.O_RDWR, 0o600)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            initialized = self.directory / "initialized"
            if initialized.exists():
                with self._connect() as db:
                    db.execute("SELECT value FROM meta WHERE key='coverage_start'").fetchone()
                return
            # A stable identity survives process and gateway restarts, without
            # making a copied/missing database look like a fresh complete day.
            identity_path = self.directory / "instance_id"
            if identity_path.exists():
                identity = str(uuid.UUID(identity_path.read_text().strip()))
            else:
                identity = str(uuid.uuid4())
                ident_fd = os.open(str(identity_path), os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
                with os.fdopen(ident_fd, "w") as out:
                    out.write(identity)
            db_fd = os.open(self.db_path, os.O_CREAT | os.O_RDWR, 0o600)
            os.close(db_fd)
            with self._connect() as db:
                db.execute("PRAGMA journal_mode=WAL")
                db.execute("CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT NOT NULL)")
                db.execute("CREATE TABLE IF NOT EXISTS daily (day TEXT, provider TEXT, credential_id TEXT, calls INTEGER NOT NULL, tokens INTEGER NOT NULL, unknown_tokens INTEGER NOT NULL, PRIMARY KEY(day,provider,credential_id))")
                db.execute("CREATE TABLE IF NOT EXISTS attempts (id TEXT PRIMARY KEY, at REAL NOT NULL, day TEXT NOT NULL, provider TEXT NOT NULL, credential_id TEXT NOT NULL, tokens INTEGER)")
                db.execute("CREATE INDEX IF NOT EXISTS attempts_at ON attempts(at)")
                db.executemany("INSERT OR IGNORE INTO meta VALUES (?,?)", [("coverage_start", str(self.clock())), ("instance_id", identity), ("detail_floor", "0")])
            marker_fd = os.open(str(initialized), os.O_CREAT | os.O_WRONLY, 0o600)
            os.close(marker_fd)
        finally:
            os.close(fd)

    def mark_gap(self):
        """Keep the gateway working; persist conservative coverage loss if possible."""
        now = self.clock()
        self._gap = (min(self._gap[0], now), max(self._gap[1], now)) if self._gap else (now, now)
        try:
            import fcntl
            import tempfile
            self.directory.mkdir(parents=True, exist_ok=True, mode=0o700)
            lock_fd = os.open(str(self.directory / "gap.lock"), os.O_CREAT | os.O_RDWR, 0o600)
            try:
                fcntl.flock(lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                path = self.directory / "gap.json"
                if path.exists():
                    old = json.loads(path.read_text())
                    self._gap = (min(old[0], self._gap[0]), max(old[1], self._gap[1]))
                fd, temporary = tempfile.mkstemp(prefix=".gap-", dir=self.directory)
                try:
                    with os.fdopen(fd, "w") as out:
                        json.dump(self._gap, out)
                        out.flush()
                        os.fsync(out.fileno())
                    os.replace(temporary, path)
                finally:
                    if os.path.exists(temporary):
                        os.unlink(temporary)
            finally:
                os.close(lock_fd)
        except (OSError, ValueError, TypeError, IndexError):
            # No waiting for a competing marker writer. One atomic fixed sentinel
            # preserves unknown coverage across processes and restarts, even if
            # gap.json could not be updated. Its interval is deliberately unknown;
            # snapshots remain conservative while this exceptional marker exists.
            try:
                fd = os.open(str(self.directory / "coverage-gap"), os.O_CREAT | os.O_WRONLY, 0o600)
                os.close(fd)
            except OSError:
                pass  # Unwritable storage can only retain the in-process gap.

    def begin(self, provider, base_url, key):
        try:
            if not (self.directory / "initialized").exists():
                self.initialize()
            now = self.clock()
            day = _day(now)
            identity = credential_id(base_url, key)
            attempt_id = uuid.uuid4().hex
            with self._connect() as db:
                db.execute("BEGIN IMMEDIATE")
                db.execute("INSERT INTO attempts VALUES (?,?,?,?,?,NULL)", (attempt_id, now, day, provider, identity))
                db.execute("INSERT INTO daily VALUES (?,?,?,1,0,1) ON CONFLICT(day,provider,credential_id) DO UPDATE SET calls=calls+1, unknown_tokens=unknown_tokens+1", (day, provider, identity))
                self._prune(db, now)
            return Attempt(self, attempt_id, provider, identity, day)
        except (OSError, sqlite3.Error, ValueError, OverflowError):
            self.mark_gap()
            return None

    def _prune(self, db, now):
        oldest = db.execute("SELECT MAX(at) FROM attempts WHERE at < ?", (now - 120,)).fetchone()[0]
        db.execute("DELETE FROM attempts WHERE at < ?", (now - 120,))
        excess = db.execute("SELECT COUNT(*) FROM attempts").fetchone()[0] - self.max_recent
        if excess > 0:
            removed = db.execute("SELECT MAX(at) FROM (SELECT at FROM attempts ORDER BY at,id LIMIT ?)", (excess,)).fetchone()[0]
            oldest = max(oldest or 0, removed)
            db.execute("DELETE FROM attempts WHERE id IN (SELECT id FROM attempts ORDER BY at,id LIMIT ?)", (excess,))
        if oldest is not None:
            db.execute("UPDATE meta SET value=? WHERE key='detail_floor' AND CAST(value AS REAL) < ?", (str(oldest), oldest))
        db.execute("DELETE FROM daily WHERE day < ?", (_day(now - 30 * 86400),))

    def snapshot(self, at, configured=(), limits=None):
        now = self.clock()
        if isinstance(at, bool) or not isinstance(at, (float, int)) or not math.isfinite(at):
            raise ValueError("at 必须是有限的 Unix 时间戳")
        day = _day(at)
        start = math.floor(at / 86400) * 86400
        result = {"version": 1, "instance_id": None, "as_of": now, "at": at,
                  "day": day, "day_start": start, "day_complete": False,
                  "rpm_complete": False, "available": False, "coverage_start": None,
                  "clock_skew": abs(now - at) > 30, "tokens_as_of": now, "rows": []}
        if result["clock_skew"]:
            return result
        limits = limits or {}
        try:
            with self._connect(True) as db:
                db.execute("BEGIN")  # All aggregates and timestamps share one read snapshot.
                meta = dict(db.execute("SELECT key,value FROM meta"))
                coverage = float(meta["coverage_start"])
                floor = float(meta["detail_floor"])
                identity = str(uuid.UUID(meta["instance_id"]))
                rows = {}
                def row(provider, identity):
                    group = (provider, identity)
                    if group not in rows:
                        limit = limits.get(provider) or {}
                        rows[group] = {"provider": provider, "credential_id": identity, "calls": 0, "tokens": 0, "unknown_tokens": 0, "rpm": 0,
                                       "daily_limit": limit.get("daily", 0), "rpm_limit": limit.get("rpm", 0)}
                    return rows[group]
                for item in db.execute("SELECT * FROM daily WHERE day=?", (day,)):
                    value = row(item["provider"], item["credential_id"])
                    for field in ("calls", "tokens", "unknown_tokens"):
                        value[field] = item[field]
                # Remove calls made after the common cutoff from today's aggregate.
                for item in db.execute("SELECT * FROM attempts WHERE day=? AND at>?", (day, at)):
                    value = row(item["provider"], item["credential_id"])
                    value["calls"] -= 1
                    value["tokens"] -= item["tokens"] or 0
                    value["unknown_tokens"] -= int(item["tokens"] is None)
                for item in db.execute("SELECT provider,credential_id,COUNT(*) AS rpm FROM attempts WHERE at>? AND at<=? GROUP BY provider,credential_id", (at - 60, at)):
                    row(item["provider"], item["credential_id"])["rpm"] = item["rpm"]
                for provider, base_url, key in configured:
                    if key:
                        row(provider, credential_id(base_url, key))
            gaps = [self._gap] if self._gap else []
            for path in self.directory.glob("gap.json"):
                gap = json.loads(path.read_text())
                if not isinstance(gap, list) or len(gap) != 2 or not all(isinstance(t, (int, float)) and math.isfinite(t) for t in gap):
                    raise ValueError("invalid coverage marker")
                gaps.append(gap)
            unknown_gap = (self.directory / "coverage-gap").exists()
            result.update(available=True, instance_id=identity, coverage_start=coverage,
                          day_complete=not unknown_gap and coverage <= start and floor <= at and not any(a <= at and b >= start for a, b in gaps),
                          rpm_complete=not unknown_gap and coverage <= at - 60 and floor <= at - 60 and not any(a <= at and b > at - 60 for a, b in gaps),
                          rows=sorted(rows.values(), key=lambda item: (item["provider"], item["credential_id"])))
        except (OSError, sqlite3.Error, ValueError, TypeError, KeyError, OverflowError):
            pass  # Missing/corrupt/partially initialized state is unavailable, never a zero.
        return result


def enable_usage():
    global _ledger
    if _ledger is None:
        _ledger = UsageLedger()


def begin_generation(provider, base_url, key):
    return _ledger.begin(provider, base_url, key) if _ledger is not None else None


def observe_usage(attempt, payload):
    if attempt is not None:
        attempt.observe(payload)


def observe_response(attempt, response):
    if attempt is not None:
        try:
            attempt.observe(response.json())
        except (ValueError, TypeError, AttributeError):
            pass
