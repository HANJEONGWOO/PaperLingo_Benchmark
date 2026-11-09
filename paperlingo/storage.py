"""Local SQLite records and opaque, revocable team sessions."""
import hashlib
import json
import secrets
import sqlite3
import time
from datetime import datetime, timezone
from pathlib import Path


def now():
    return datetime.now(timezone.utc).isoformat()


def report_status(report):
    qualities = [i["quality"]["status"] for p in report["papers"] for i in p["items"]]
    if "error" in qualities:
        return "error"
    if "needs_review" in qualities:
        return "needs_review"
    if "pending" in qualities:
        return "awaiting_review"
    return "complete"


class Store:
    def __init__(self, runtime_dir: Path):
        runtime_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.path = runtime_dir / "paperlingo.sqlite3"
        with self.connect() as db:
            db.execute("PRAGMA journal_mode=WAL")
            db.executescript("""
                CREATE TABLE IF NOT EXISTS runs (
                    id TEXT PRIMARY KEY, name TEXT NOT NULL,
                    created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
                    status TEXT NOT NULL, is_demo INTEGER NOT NULL,
                    report TEXT NOT NULL, judge_meta TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS reviews (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    run_id TEXT NOT NULL, paper_id TEXT NOT NULL, question_id TEXT NOT NULL,
                    created_at TEXT NOT NULL, previous_quality TEXT NOT NULL, quality TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS sessions (token_hash TEXT PRIMARY KEY, expires_at REAL NOT NULL);
            """)
        self.path.chmod(0o600)

    def connect(self):
        db = sqlite3.connect(self.path, timeout=15)
        db.row_factory = sqlite3.Row
        return db

    @staticmethod
    def _decode(row):
        if row is None:
            return None
        result = dict(row)
        result["report"] = json.loads(result["report"])
        result["judge_meta"] = json.loads(result["judge_meta"])
        result["is_demo"] = bool(result["is_demo"])
        return result

    def create(self, name, report, is_demo=False):
        run_id = secrets.token_hex(12)
        stamp = now()
        with self.connect() as db:
            db.execute("INSERT INTO runs VALUES (?,?,?,?,?,?,?,?)", (run_id, name, stamp, stamp, report_status(report), int(is_demo), json.dumps(report, ensure_ascii=False, allow_nan=False), "{}"))
        return self.get(run_id)

    def get(self, run_id):
        with self.connect() as db:
            return self._decode(db.execute("SELECT * FROM runs WHERE id=?", (run_id,)).fetchone())

    def count(self):
        with self.connect() as db:
            return db.execute("SELECT COUNT(*) FROM runs").fetchone()[0]

    def summaries(self, limit=100):
        with self.connect() as db:
            rows = db.execute("SELECT * FROM runs ORDER BY created_at DESC LIMIT ?", (limit,)).fetchall()
        result = []
        for row in rows:
            r = self._decode(row)
            report = r.pop("report")
            r.update(scope=report["scope"], metrics=report["metrics"], counts=report["counts"], dataset_version=report["dataset_version"], metrics_version=report.get("metrics_version"), score_mode=report.get("score_mode", "reviewed"), quality_policy_version=report.get("quality_policy_version"))
            result.append(r)
        return result

    def save(self, run, review=None, reviews=None):
        run["updated_at"] = now()
        with self.connect() as db:
            db.execute("UPDATE runs SET updated_at=?, status=?, report=?, judge_meta=? WHERE id=?", (run["updated_at"], run["status"], json.dumps(run["report"], ensure_ascii=False, allow_nan=False), json.dumps(run.get("judge_meta", {}), ensure_ascii=False, allow_nan=False), run["id"]))
            for entry in ([review] if review else []) + (reviews or []):
                db.execute("INSERT INTO reviews (run_id,paper_id,question_id,created_at,previous_quality,quality) VALUES (?,?,?,?,?,?)", (run["id"], entry["paper_id"], entry["question_id"], now(), json.dumps(entry["previous_quality"], ensure_ascii=False), json.dumps(entry["quality"], ensure_ascii=False)))
        return run

    def review_history(self, run_id):
        with self.connect() as db:
            rows = db.execute("SELECT * FROM reviews WHERE run_id=? ORDER BY id", (run_id,)).fetchall()
        return [{**dict(r), "previous_quality": json.loads(r["previous_quality"]), "quality": json.loads(r["quality"])} for r in rows]

    def recover_interrupted(self):
        with self.connect() as db:
            rows = db.execute("SELECT * FROM runs WHERE status IN ('judging', 'queued')").fetchall()
        for row in rows:
            run = self._decode(row)
            unfinished = any(i["structure_valid"] and (i["quality"]["status"] in {"pending", "needs_review", "error"}
                             or i["quality"].get("source") == "temporary") for p in run["report"]["papers"] for i in p["items"])
            run["status"] = "queued" if unfinished and run["judge_meta"].get("job_id") else report_status(run["report"])
            run["judge_meta"]["interrupted"] = True
            self.save(run)

    def create_session(self, hours):
        token = secrets.token_urlsafe(32)
        with self.connect() as db:
            db.execute("DELETE FROM sessions WHERE expires_at < ?", (time.time(),))
            db.execute("INSERT INTO sessions VALUES (?,?)", (hashlib.sha256(token.encode()).hexdigest(), time.time() + hours * 3600))
        return token

    def valid_session(self, token):
        if not token or len(token) > 256:
            return False
        with self.connect() as db:
            row = db.execute("SELECT expires_at FROM sessions WHERE token_hash=?", (hashlib.sha256(token.encode()).hexdigest(),)).fetchone()
            return bool(row and row[0] > time.time())

    def remove_session(self, token):
        with self.connect() as db:
            db.execute("DELETE FROM sessions WHERE token_hash=?", (hashlib.sha256((token or "").encode()).hexdigest(),))
