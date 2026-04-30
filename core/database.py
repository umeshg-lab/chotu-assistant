"""
SQLite database — all local persistent storage for JARVIS.

FIX LOG (core/database.py):
  BUG-A  check_same_thread=False is required for multi-threaded access but it
         does NOT make SQLite thread-safe on its own — all write operations
         must be serialised.  Added a threading.Lock around every execute +
         commit pair so concurrent threads (TTS worker, reminder scheduler,
         wake-word handler) never step on each other.

  BUG-B  DB_PATH was a module-level constant resolved at import time.  If
         main.py hasn't os.chdir'd to the project root yet, the path can be
         wrong.  Fix: resolve relative to the project root (parent of core/).

  BUG-C  executescript() performs an implicit COMMIT before it runs, which
         means if the schema migration is interrupted the DB can be in a
         partially-committed state.  Switched to individual CREATE TABLE
         statements inside a single transaction guarded by the lock.

  BUG-D  get_workflow() compared name to name.lower() but saved name was
         already lowercased in save_workflow().  Consistent now.

  NEW    Added a WAL pragma for better concurrent-read performance.
  NEW    Added get_reminder() for lookup by ID (used by UI delete).
  NEW    All public methods now swallow sqlite3.OperationalError and log
         instead of propagating, so a single bad query can't crash a thread.
"""

import sqlite3
import json
import logging
import threading
from datetime import datetime
from pathlib import Path
from typing import Optional

logger = logging.getLogger("JARVIS.Database")

# Resolve DB path relative to project root (two levels up from this file)
_ROOT   = Path(__file__).resolve().parent.parent
DB_PATH = _ROOT / "data" / "jarvis.db"


class Database:
    def __init__(self):
        DB_PATH.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self.conn = sqlite3.connect(
            str(DB_PATH),
            check_same_thread=False,
            detect_types=sqlite3.PARSE_DECLTYPES,
        )
        self.conn.row_factory = sqlite3.Row
        # WAL mode: readers don't block writers; writers don't block readers
        self.conn.execute("PRAGMA journal_mode=WAL")
        self._init_schema()

    # ── Schema ───────────────────────────────────────────────────────────────

    def _init_schema(self):
        with self._lock:
            self.conn.execute("BEGIN")
            try:
                self.conn.execute("""
                    CREATE TABLE IF NOT EXISTS command_history (
                        id       INTEGER PRIMARY KEY AUTOINCREMENT,
                        command  TEXT    NOT NULL,
                        response TEXT    DEFAULT '',
                        mode     TEXT    DEFAULT 'standard',
                        ts       TEXT    DEFAULT (datetime('now'))
                    )
                """)
                self.conn.execute("""
                    CREATE TABLE IF NOT EXISTS trained_commands (
                        id        INTEGER PRIMARY KEY AUTOINCREMENT,
                        trigger   TEXT    NOT NULL UNIQUE,
                        actions   TEXT    NOT NULL,
                        use_count INTEGER DEFAULT 0,
                        created   TEXT    DEFAULT (datetime('now')),
                        updated   TEXT    DEFAULT (datetime('now'))
                    )
                """)
                self.conn.execute("""
                    CREATE TABLE IF NOT EXISTS workflows (
                        id        INTEGER PRIMARY KEY AUTOINCREMENT,
                        name      TEXT    NOT NULL UNIQUE,
                        trigger   TEXT,
                        steps     TEXT    NOT NULL,
                        schedule  TEXT,
                        enabled   INTEGER DEFAULT 1,
                        run_count INTEGER DEFAULT 0,
                        created   TEXT    DEFAULT (datetime('now'))
                    )
                """)
                self.conn.execute("""
                    CREATE TABLE IF NOT EXISTS reminders (
                        id        INTEGER PRIMARY KEY AUTOINCREMENT,
                        text      TEXT    NOT NULL,
                        remind_at TEXT    NOT NULL,
                        repeat    TEXT    DEFAULT 'none',
                        fired     INTEGER DEFAULT 0,
                        created   TEXT    DEFAULT (datetime('now'))
                    )
                """)
                self.conn.execute("""
                    CREATE TABLE IF NOT EXISTS notes (
                        id      INTEGER PRIMARY KEY AUTOINCREMENT,
                        title   TEXT,
                        body    TEXT    NOT NULL,
                        tags    TEXT    DEFAULT '[]',
                        created TEXT    DEFAULT (datetime('now'))
                    )
                """)
                self.conn.execute("""
                    CREATE TABLE IF NOT EXISTS memory (
                        id      INTEGER PRIMARY KEY AUTOINCREMENT,
                        key     TEXT    NOT NULL UNIQUE,
                        value   TEXT    NOT NULL,
                        updated TEXT    DEFAULT (datetime('now'))
                    )
                """)
                self.conn.commit()
            except Exception as exc:
                self.conn.rollback()
                logger.error(f"Schema init failed: {exc}")
                raise

    # ── Command History ──────────────────────────────────────────────────────

    def log_command(self, command: str, response: str = "", mode: str = "standard"):
        with self._lock:
            try:
                self.conn.execute(
                    "INSERT INTO command_history (command, response, mode) VALUES (?,?,?)",
                    (command, response, mode),
                )
                self.conn.commit()
            except sqlite3.Error as exc:
                logger.error(f"log_command error: {exc}")

    def get_history(self, limit: int = 50):
        with self._lock:
            try:
                return self.conn.execute(
                    "SELECT * FROM command_history ORDER BY ts DESC LIMIT ?",
                    (limit,),
                ).fetchall()
            except sqlite3.Error as exc:
                logger.error(f"get_history error: {exc}")
                return []

    # ── Trained Commands ─────────────────────────────────────────────────────

    def add_trained_command(self, trigger: str, actions: list):
        with self._lock:
            try:
                self.conn.execute(
                    """INSERT INTO trained_commands (trigger, actions)
                       VALUES (?,?)
                       ON CONFLICT(trigger) DO UPDATE SET
                         actions  = excluded.actions,
                         updated  = datetime('now')""",
                    (trigger.lower().strip(), json.dumps(actions)),
                )
                self.conn.commit()
            except sqlite3.Error as exc:
                logger.error(f"add_trained_command error: {exc}")

    def get_trained_commands(self):
        with self._lock:
            try:
                rows = self.conn.execute(
                    "SELECT * FROM trained_commands ORDER BY use_count DESC"
                ).fetchall()
            except sqlite3.Error as exc:
                logger.error(f"get_trained_commands error: {exc}")
                return []
        result = []
        for r in rows:
            d = dict(r)
            try:
                d["actions"] = json.loads(d["actions"])
            except (json.JSONDecodeError, KeyError):
                d["actions"] = []
            result.append(d)
        return result

    def increment_command_use(self, trigger: str):
        with self._lock:
            try:
                self.conn.execute(
                    "UPDATE trained_commands SET use_count = use_count + 1 WHERE trigger = ?",
                    (trigger,),
                )
                self.conn.commit()
            except sqlite3.Error as exc:
                logger.error(f"increment_command_use error: {exc}")

    def delete_trained_command(self, trigger: str):
        with self._lock:
            try:
                self.conn.execute(
                    "DELETE FROM trained_commands WHERE trigger = ?", (trigger,)
                )
                self.conn.commit()
            except sqlite3.Error as exc:
                logger.error(f"delete_trained_command error: {exc}")

    # ── Workflows ────────────────────────────────────────────────────────────

    def save_workflow(
        self, name: str, steps: list, trigger: str = None, schedule: str = None
    ):
        with self._lock:
            try:
                self.conn.execute(
                    """INSERT INTO workflows (name, trigger, steps, schedule)
                       VALUES (?,?,?,?)
                       ON CONFLICT(name) DO UPDATE SET
                         trigger  = excluded.trigger,
                         steps    = excluded.steps,
                         schedule = excluded.schedule""",
                    (name.lower(), trigger, json.dumps(steps), schedule),
                )
                self.conn.commit()
            except sqlite3.Error as exc:
                logger.error(f"save_workflow error: {exc}")

    def get_workflows(self, enabled_only: bool = False):
        q = "SELECT * FROM workflows"
        if enabled_only:
            q += " WHERE enabled = 1"
        q += " ORDER BY name"
        with self._lock:
            try:
                rows = self.conn.execute(q).fetchall()
            except sqlite3.Error as exc:
                logger.error(f"get_workflows error: {exc}")
                return []
        result = []
        for r in rows:
            d = dict(r)
            try:
                d["steps"] = json.loads(d["steps"])
            except (json.JSONDecodeError, KeyError):
                d["steps"] = []
            result.append(d)
        return result

    def get_workflow(self, name: str) -> Optional[dict]:
        with self._lock:
            try:
                row = self.conn.execute(
                    "SELECT * FROM workflows WHERE name = ?", (name.lower(),)
                ).fetchone()
            except sqlite3.Error as exc:
                logger.error(f"get_workflow error: {exc}")
                return None
        if row:
            d = dict(row)
            try:
                d["steps"] = json.loads(d["steps"])
            except (json.JSONDecodeError, KeyError):
                d["steps"] = []
            return d
        return None

    def increment_workflow_run(self, name: str):
        with self._lock:
            try:
                self.conn.execute(
                    "UPDATE workflows SET run_count = run_count + 1 WHERE name = ?",
                    (name.lower(),),
                )
                self.conn.commit()
            except sqlite3.Error as exc:
                logger.error(f"increment_workflow_run error: {exc}")

    def delete_workflow(self, name: str):
        with self._lock:
            try:
                self.conn.execute(
                    "DELETE FROM workflows WHERE name = ?", (name.lower(),)
                )
                self.conn.commit()
            except sqlite3.Error as exc:
                logger.error(f"delete_workflow error: {exc}")

    # ── Reminders ────────────────────────────────────────────────────────────

    def add_reminder(self, text: str, remind_at: datetime, repeat: str = "none"):
        with self._lock:
            try:
                cur = self.conn.execute(
                    "INSERT INTO reminders (text, remind_at, repeat) VALUES (?,?,?)",
                    (text, remind_at.isoformat(), repeat),
                )
                self.conn.commit()
                return cur.lastrowid
            except sqlite3.Error as exc:
                logger.error(f"add_reminder error: {exc}")
                return None

    def get_pending_reminders(self):
        with self._lock:
            try:
                return self.conn.execute(
                    "SELECT * FROM reminders WHERE fired = 0 AND remind_at <= ? ORDER BY remind_at",
                    (datetime.now().isoformat(),),
                ).fetchall()
            except sqlite3.Error as exc:
                logger.error(f"get_pending_reminders error: {exc}")
                return []

    def get_all_reminders(self):
        with self._lock:
            try:
                return self.conn.execute(
                    "SELECT * FROM reminders WHERE fired = 0 ORDER BY remind_at"
                ).fetchall()
            except sqlite3.Error as exc:
                logger.error(f"get_all_reminders error: {exc}")
                return []

    def get_reminder(self, rid: int) -> Optional[dict]:
        with self._lock:
            try:
                row = self.conn.execute(
                    "SELECT * FROM reminders WHERE id = ?", (rid,)
                ).fetchone()
                return dict(row) if row else None
            except sqlite3.Error as exc:
                logger.error(f"get_reminder error: {exc}")
                return None

    def find_pending_reminder(self, text: str = None) -> Optional[dict]:
        with self._lock:
            try:
                if text:
                    row = self.conn.execute(
                        """SELECT * FROM reminders
                           WHERE fired = 0 AND lower(text) LIKE ?
                           ORDER BY created DESC LIMIT 1""",
                        (f"%{text.lower().strip()}%",),
                    ).fetchone()
                else:
                    row = self.conn.execute(
                        """SELECT * FROM reminders
                           WHERE fired = 0 ORDER BY created DESC LIMIT 1"""
                    ).fetchone()
                return dict(row) if row else None
            except sqlite3.Error as exc:
                logger.error(f"find_pending_reminder error: {exc}")
                return None

    def update_reminder_time(self, rid: int, remind_at: datetime):
        with self._lock:
            try:
                cur = self.conn.execute(
                    "UPDATE reminders SET remind_at = ?, fired = 0 WHERE id = ?",
                    (remind_at.isoformat(), rid),
                )
                self.conn.commit()
                return cur.rowcount > 0
            except sqlite3.Error as exc:
                logger.error(f"update_reminder_time error: {exc}")
                return False

    def mark_reminder_fired(self, rid: int):
        with self._lock:
            try:
                self.conn.execute(
                    "UPDATE reminders SET fired = 1 WHERE id = ?", (rid,)
                )
                self.conn.commit()
            except sqlite3.Error as exc:
                logger.error(f"mark_reminder_fired error: {exc}")

    def delete_reminder(self, rid: int):
        with self._lock:
            try:
                self.conn.execute("DELETE FROM reminders WHERE id = ?", (rid,))
                self.conn.commit()
            except sqlite3.Error as exc:
                logger.error(f"delete_reminder error: {exc}")

    # ── Notes ────────────────────────────────────────────────────────────────

    def add_note(self, body: str, title: str = None, tags: list = None):
        with self._lock:
            try:
                self.conn.execute(
                    "INSERT INTO notes (title, body, tags) VALUES (?,?,?)",
                    (title, body, json.dumps(tags or [])),
                )
                self.conn.commit()
            except sqlite3.Error as exc:
                logger.error(f"add_note error: {exc}")

    def get_notes(self, limit: int = 20):
        with self._lock:
            try:
                return self.conn.execute(
                    "SELECT * FROM notes ORDER BY created DESC LIMIT ?", (limit,)
                ).fetchall()
            except sqlite3.Error as exc:
                logger.error(f"get_notes error: {exc}")
                return []

    # ── Memory (key-value) ───────────────────────────────────────────────────

    def remember(self, key: str, value: str):
        with self._lock:
            try:
                self.conn.execute(
                    """INSERT INTO memory (key, value) VALUES (?,?)
                       ON CONFLICT(key) DO UPDATE SET
                         value   = excluded.value,
                         updated = datetime('now')""",
                    (key, value),
                )
                self.conn.commit()
            except sqlite3.Error as exc:
                logger.error(f"remember error: {exc}")

    def recall(self, key: str) -> Optional[str]:
        with self._lock:
            try:
                row = self.conn.execute(
                    "SELECT value FROM memory WHERE key = ?", (key,)
                ).fetchone()
                return row[0] if row else None
            except sqlite3.Error as exc:
                logger.error(f"recall error: {exc}")
                return None

    # ── Lifecycle ────────────────────────────────────────────────────────────

    def close(self):
        with self._lock:
            try:
                self.conn.close()
            except Exception as exc:
                logger.error(f"Database close error: {exc}")
