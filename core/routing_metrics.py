"""Bounded rolling business metrics and shared provider cooldowns; no request bodies."""
import math
import os
import sqlite3
import statistics
import time
from contextlib import closing
from pathlib import Path
from core.paths import DATA_DIR

WINDOW = 86400
SAMPLE_LIMIT = 100

class RoutingMetrics:
    def __init__(self, path=None):
        self.path = Path(path or Path(DATA_DIR) / 'routing-metrics.sqlite3')

    def _open(self):
        self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        fd = os.open(self.path, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
        os.close(fd)
        db = sqlite3.connect(self.path, timeout=5)
        db.execute('PRAGMA journal_mode=WAL')
        db.execute('CREATE TABLE IF NOT EXISTS samples(id INTEGER PRIMARY KEY,target TEXT,task TEXT,stamp REAL,success INTEGER,duration REAL,ttft REAL,tps REAL)')
        db.execute('CREATE INDEX IF NOT EXISTS samples_target_task ON samples(target,task,stamp)')
        db.execute('CREATE TABLE IF NOT EXISTS blocks(provider TEXT PRIMARY KEY,reason TEXT,until REAL)')
        return db

    def record(self, target, task, success, duration=None, ttft=None, output_tokens=None, eligible=True, now=None):
        if not eligible: return
        now = time.time() if now is None else now
        def valid(v): return float(v) if type(v) in (int,float) and math.isfinite(v) and v >= 0 else None
        duration,ttft,output_tokens=map(valid,(duration,ttft,output_tokens))
        generation = duration - ttft if duration is not None and ttft is not None else None
        tps = output_tokens/generation if output_tokens and generation and generation>0 else None
        with closing(self._open()) as db, db:
            db.execute('INSERT INTO samples(target,task,stamp,success,duration,ttft,tps) VALUES(?,?,?,?,?,?,?)',(target,task,now,int(success),duration,ttft,tps))
            db.execute('DELETE FROM samples WHERE stamp < ?', (now-WINDOW,))
            db.execute('DELETE FROM samples WHERE target=? AND task=? AND id NOT IN (SELECT id FROM samples WHERE target=? AND task=? ORDER BY stamp DESC,id DESC LIMIT ?)',(target,task,target,task,SAMPLE_LIMIT))

    def stats(self, target, task, now=None):
        now=time.time() if now is None else now
        rows=[]
        available=True
        try:
            if self.path.exists():
                with closing(sqlite3.connect(self.path.resolve().as_uri()+'?mode=ro',uri=True,timeout=5)) as db:
                    rows=db.execute('SELECT success,duration,ttft,tps FROM samples WHERE target=? AND task=? AND stamp>=? ORDER BY stamp DESC,id DESC LIMIT ?',(target,task,now-WINDOW,SAMPLE_LIMIT)).fetchall()
        except (OSError, sqlite3.Error):
            available=False
        n=len(rows); successes=sum(r[0] for r in rows)
        def median(index):
            values=[r[index] for r in rows if r[0] and r[index] is not None]
            return round(statistics.median(values),3) if values else None
        return {'available':available,'samples':n,'successes':successes,'success_rate':successes/n if n else None,
                'smoothed_success_rate':(successes+5)/(n+10), 'ttft':median(2),'duration':median(1),'tokens_per_second':median(3),
                'ttft_samples':sum(1 for r in rows if r[0] and r[2] is not None),'window_hours':24}

    def block(self, provider, reason, seconds):
        until=time.time()+max(1,min(float(seconds),86400))
        with closing(self._open()) as db, db:
            db.execute('INSERT INTO blocks VALUES(?,?,?) ON CONFLICT(provider) DO UPDATE SET reason=excluded.reason,until=max(blocks.until,excluded.until)',(provider,reason,until))

    def provider_status(self, provider):
        if not self.path.exists(): return None
        try:
            with closing(sqlite3.connect(self.path.resolve().as_uri()+'?mode=ro',uri=True,timeout=5)) as db:
                row=db.execute('SELECT reason,until FROM blocks WHERE provider=? AND until>?',(provider,time.time())).fetchone()
            return {'reason':row[0],'until':row[1]} if row else None
        except (OSError, sqlite3.Error):
            return None

    def clear_provider(self, provider):
        if not self.path.exists():return
        with closing(self._open()) as db, db: db.execute('DELETE FROM blocks WHERE provider=?',(provider,))
