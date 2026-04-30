"""
Reminder scheduler — polls DB for due reminders and fires TTS alerts.

FIX LOG (automation/reminder.py):
  BUG-A  _parse_time() 'at HH:MM' branch built a regex that matched the
         literal string 'at 3:30pm' correctly but also matched unintended
         strings because the group for minutes was optional with no upper
         bound — e.g. 'at 3' alone matched and set minutes to 0.  Tightened
         to require exactly 2 digits for the minute group, and 'at' must be
         a word boundary.

  BUG-B  'tomorrow at HH:MM' recursed into _parse_time with the 'at HH:MM'
         string, which was correct, but then added timedelta(days=1) again
         on top of the already-next-day datetime returned by the branch that
         does 'if dt <= now: dt += timedelta(days=1)'.  This produced a
         reminder 2 days in the future.  Fix: strip the +1-day auto-advance
         inside the recursive call when 'tomorrow' is the prefix.

  BUG-C  _poll_loop used threading.Event.wait(30) as the sleep mechanism.
         When stop() is called, wait() returns True (event is set) and the
         loop exits — but only AFTER waiting the full 30 seconds if the
         event fires exactly as a check begins.  Actually wait() returns
         immediately when set, so this was only an apparent issue.  Left as-is
         but documented.

  NEW    add_from_text() now validates the parsed datetime and rejects
         times in the past (except via 'tomorrow' or 'in X minutes').
  NEW    repeat reminder support: 'daily' repeats reschedule themselves.
"""

import logging
import re
import threading
from datetime import datetime, timedelta
from typing import Optional

logger = logging.getLogger("JARVIS.Reminder")


class ReminderScheduler:
    def __init__(self, tts):
        self.tts  = tts
        self._db  = None   # injected after construction to avoid circular import
        self._thread: Optional[threading.Thread] = None
        self._stop  = threading.Event()
        self._last_reminder_id: Optional[int] = None

    def set_db(self, db):
        self._db = db

    # ── Lifecycle ────────────────────────────────────────────────────────────

    def start(self):
        self._stop.clear()
        self._thread = threading.Thread(
            target=self._poll_loop, daemon=True, name="JarvisReminder"
        )
        self._thread.start()
        logger.info("Reminder scheduler started.")

    def stop(self):
        self._stop.set()
        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=5)

    # ── Poll loop ────────────────────────────────────────────────────────────

    def _poll_loop(self):
        while not self._stop.wait(30):  # check every 30 seconds
            if self._db:
                self._check_reminders()

    def _check_reminders(self):
        try:
            pending = self._db.get_pending_reminders()
            for r in pending:
                text = r["text"]
                logger.info(f"Firing reminder: {text}")
                self.tts.speak_async(f"Reminder: {text}")
                self._db.mark_reminder_fired(r["id"])

                # Reschedule repeating reminders
                repeat = r["repeat"] if "repeat" in r.keys() else "none"
                if repeat == "daily":
                    try:
                        orig = datetime.fromisoformat(r["remind_at"])
                        self._db.add_reminder(text, orig + timedelta(days=1), repeat)
                    except Exception as exc:
                        logger.warning(f"Repeat reschedule failed: {exc}")

        except Exception as exc:
            logger.error(f"Reminder check error: {exc}", exc_info=True)

    # ── Public helpers ───────────────────────────────────────────────────────

    def add_from_text(
        self, task: str, time_str: str, repeat: str = "none"
    ) -> Optional[str]:
        """Parse time string, validate, and create a reminder."""
        task = " ".join(str(task or "").strip().split())
        time_str = " ".join(str(time_str or "").strip().split())
        if not task or not time_str:
            return None
        if not self._db:
            logger.error("Cannot add reminder: database is not attached")
            return None
        dt = self._parse_time(time_str)
        if not dt:
            return None
        if dt < datetime.now():
            logger.warning(f"Reminder time is in the past: {dt}")
            return None
        rid = self._db.add_reminder(task, dt, repeat)
        if not rid:
            logger.error(f"Reminder insert failed: '{task}' at {dt}")
            return None
        self._last_reminder_id = int(rid)
        logger.info(f"Reminder set: '{task}' at {dt} (repeat={repeat}, id={rid})")
        return f"{task} at {dt.strftime('%I:%M %p')}"

    def reschedule_from_text(
        self, task_hint: str, time_str: str, reminder_id: int = None
    ) -> Optional[str]:
        """Move the latest matching pending reminder to a new parsed time."""
        if not self._db:
            return None

        task_hint = self._clean_task_hint(task_hint)
        time_str = " ".join(str(time_str or "").strip().split())

        reminder = self._resolve_pending_reminder(task_hint, reminder_id)
        if not reminder:
            return None

        dt = self._parse_reschedule_time(time_str, reminder)
        if not dt or dt < datetime.now():
            return None

        if not self._db.update_reminder_time(reminder["id"], dt):
            logger.error(f"Reminder update failed: id={reminder['id']}")
            return None

        self._last_reminder_id = int(reminder["id"])
        text = reminder["text"]
        logger.info(f"Reminder rescheduled: '{text}' -> {dt}")
        return f"{text} moved to {dt.strftime('%I:%M %p')}"

    def _resolve_pending_reminder(
        self, task_hint: str = "", reminder_id: int = None
    ) -> Optional[dict]:
        if reminder_id:
            reminder = self._db.get_reminder(int(reminder_id))
            if reminder and not reminder.get("fired"):
                return reminder

        if task_hint and task_hint not in {"it", "that", "this", "the reminder"}:
            return self._db.find_pending_reminder(task_hint)

        if self._last_reminder_id:
            reminder = self._db.get_reminder(self._last_reminder_id)
            if reminder and not reminder.get("fired"):
                return reminder

        return self._db.find_pending_reminder()

    @staticmethod
    def _clean_task_hint(task_hint: str) -> str:
        task_hint = " ".join(str(task_hint or "").strip().lower().split())
        task_hint = re.sub(r"^(?:the|my)\s+", "", task_hint)
        task_hint = re.sub(r"\s+reminder$", "", task_hint)
        return task_hint

    def _parse_reschedule_time(self, time_str: str, reminder: dict) -> Optional[datetime]:
        if self._is_date_only_shift(time_str):
            try:
                original = datetime.fromisoformat(str(reminder["remind_at"]))
            except (KeyError, TypeError, ValueError):
                return None
            now = datetime.now()
            target = original
            if time_str.strip().lower() == "tomorrow":
                target = original.replace(
                    year=now.year, month=now.month, day=now.day
                ) + timedelta(days=1)
            return target.replace(second=0, microsecond=0)
        return self._parse_time(time_str)

    @staticmethod
    def _is_date_only_shift(time_str: str) -> bool:
        return str(time_str or "").strip().lower() in {"tomorrow"}

    # ── Time parser ──────────────────────────────────────────────────────────

    def _parse_time(self, time_str: str, _is_recursive: bool = False) -> Optional[datetime]:
        """
        Parse natural-language time strings.  Returns a datetime or None.
        _is_recursive is set when called from the 'tomorrow' branch to
        suppress the automatic next-day advance (BUG-B fix).
        """
        time_str = time_str.strip().lower()
        now      = datetime.now()

        # "in X minutes/hours"
        m = re.match(r"in (\d+)\s*(minute|minutes|min|hour|hours|hr)", time_str)
        if m:
            n    = int(m.group(1))
            unit = m.group(2)
            if "hour" in unit or unit == "hr":
                return now + timedelta(hours=n)
            return now + timedelta(minutes=n)

        # "4 pm", "4pm", "4:30 pm", "16:30", optionally prefixed with "at"
        m = re.match(r"(?:at\s+)?(\d{1,2})(?::(\d{2}))?\s*(am|pm)?\b", time_str)
        if m and (m.group(2) is not None or m.group(3) is not None):
            hour = int(m.group(1))
            minute = int(m.group(2) or 0)
            period = m.group(3)
            if period == "pm" and hour < 12:
                hour += 12
            elif period == "am" and hour == 12:
                hour = 0
            try:
                dt = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
            except ValueError:
                return None
            if dt <= now and not _is_recursive:
                dt += timedelta(days=1)
            return dt

        # "at HH:MM [am/pm]" — BUG-A fix: require exactly 2 digit minutes
        m = re.match(r"\bat\s+(\d{1,2}):(\d{2})\s*(am|pm)?", time_str)
        if m:
            hour   = int(m.group(1))
            minute = int(m.group(2))
            period = m.group(3)
            if period == "pm" and hour < 12:
                hour += 12
            elif period == "am" and hour == 12:
                hour = 0
            try:
                dt = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
            except ValueError:
                return None
            # Advance to tomorrow only when NOT called from the tomorrow branch
            if dt <= now and not _is_recursive:
                dt += timedelta(days=1)
            return dt

        # "at H am/pm" (no minutes)
        m = re.match(r"\bat\s+(\d{1,2})\s*(am|pm)", time_str)
        if m:
            hour   = int(m.group(1))
            period = m.group(2)
            if period == "pm" and hour < 12:
                hour += 12
            elif period == "am" and hour == 12:
                hour = 0
            try:
                dt = now.replace(hour=hour, minute=0, second=0, microsecond=0)
            except ValueError:
                return None
            if dt <= now and not _is_recursive:
                dt += timedelta(days=1)
            return dt

        # "tomorrow [at ...]" — BUG-B fix
        if time_str == "tomorrow":
            return now.replace(second=0, microsecond=0) + timedelta(days=1)

        if time_str.startswith("tomorrow"):
            rest = time_str[len("tomorrow"):].strip()
            base = self._parse_time(rest, _is_recursive=True) if rest else now
            if base:
                # Add exactly 1 day — no extra advance
                return base.replace(
                    year=now.year, month=now.month, day=now.day
                ) + timedelta(days=1) if not rest else base + timedelta(days=1)
        return None
