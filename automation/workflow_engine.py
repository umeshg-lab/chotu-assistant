"""
Workflow engine — executes multi-step automation sequences.

FIX LOG (automation/workflow_engine.py):
  BUG-A  'speak' action created a brand new TextToSpeech + Config instance
         on every single step execution.  This spawned a new background TTS
         worker thread each time and leaked it.  Fix: accept tts/config as
         optional constructor arguments so the shared instances are reused.

  BUG-B  'run_mode' action similarly created brand new ModeManager +
         TextToSpeech + Config instances.  Same leak.  Fixed the same way.

  BUG-C  'reminder' action called db.add_reminder() with datetime.now()
         regardless of any time embedded in the step — i.e. every reminder
         created by a workflow fired immediately.  If a 'time' field is
         present, parse it properly; otherwise schedule 1 minute from now
         as a sensible default.

  BUG-D  run_steps() delay of 0.5 s between every step introduced a
         multi-second wait even for fast steps.  Made delay configurable
         with a 0.3 s default, and skipped the sleep after the last step.

  NEW    _execute_step is now wrapped in a per-step try/except (it already
         was, but the exception type was too broad and swallowed ImportErrors
         that would hide missing dependencies).  Now logs a full traceback.
"""

import time
import logging
from pathlib import Path
import shutil
import subprocess
from datetime import datetime, timedelta
from typing import Optional

logger = logging.getLogger("JARVIS.Workflow")


class WorkflowEngine:
    def __init__(self, db, tts=None, config=None):
        self.db     = db
        self._tts   = tts      # shared TTS instance (optional; avoids leak)
        self._cfg   = config   # shared Config instance (optional)

    # ── Public API ───────────────────────────────────────────────────────────

    def run_steps(self, steps: list, delay: float = 0.3) -> str:
        """Execute a list of action dicts sequentially."""
        results = []
        for idx, step in enumerate(steps):
            result = self._execute_step(step)
            if result:
                results.append(result)
            # BUG-D fix: skip sleep after the last step
            if delay > 0 and idx < len(steps) - 1:
                time.sleep(delay)
        return " | ".join(results)

    def run_by_name(self, name: str) -> Optional[str]:
        wf = self.db.get_workflow(name.lower())
        if not wf:
            return None
        self.db.increment_workflow_run(name.lower())
        return self.run_steps(wf["steps"])

    def list_names(self) -> list:
        return [w["name"] for w in self.db.get_workflows()]

    def save_workflow(
        self,
        name: str,
        steps: list,
        trigger: str = None,
        schedule: str = None,
    ):
        self.db.save_workflow(name.lower(), steps, trigger, schedule)
        logger.info(f"Workflow saved: {name} ({len(steps)} steps)")

    def delete_workflow(self, name: str):
        self.db.delete_workflow(name.lower())

    def get_all(self) -> list:
        return self.db.get_workflows()

    # ── Step executor ────────────────────────────────────────────────────────

    def _execute_step(self, step: dict) -> str:
        action_type = step.get("type", "").lower()
        logger.info(f"Executing step: {step}")

        try:
            if action_type == "open_app":
                from automation import app_control
                cfg = self._cfg or self._get_config()
                target = step["target"]
                if app_control.open_app(target, cfg):
                    return f"opened:{target}"
                return f"open_failed:{target}"

            elif action_type == "open_url":
                from automation import browser_control
                browser_control.open_url(step["url"])
                return f"url:{step['url']}"

            elif action_type == "close_app":
                from automation import app_control
                target = step["target"]
                if app_control.close_app(target):
                    return f"closed:{target}"
                return f"close_failed:{target}"

            elif action_type == "speak":
                # BUG-A fix: reuse shared TTS if available; create once otherwise
                tts = self._tts or self._get_tts()
                tts.speak(step["text"])
                return "spoke"

            elif action_type == "run_mode":
                # BUG-B fix: reuse shared instances
                from modes.mode_manager import ModeManager
                cfg  = self._cfg or self._get_config()
                tts  = self._tts or self._get_tts()
                modes = ModeManager(cfg, tts)
                modes.activate(step["mode"])
                return f"mode:{step['mode']}"

            elif action_type == "shell":
                command = str(step.get("command", "")).strip()
                if not command:
                    return "shell:empty"
                parts = command.split()
                executable = parts[0]
                resolved = None
                if Path(executable).is_file():
                    resolved = executable
                else:
                    resolved = shutil.which(executable)
                if not resolved:
                    logger.warning(f"Refusing unresolved shell workflow command: {command}")
                    return "shell:refused"
                subprocess.Popen([resolved, *parts[1:]], shell=False)
                return f"shell:{resolved}"

            elif action_type == "set_volume":
                from automation import media_control
                verified = media_control.set_volume(step["level"])
                return f"volume:{verified}"

            elif action_type == "media":
                from automation import media_control
                action = step.get("action", "").lower()
                if action == "play":
                    playlist_name = step.get("playlist", "")
                    if playlist_name:
                        from automation import browser_control
                        cfg = self._cfg or self._get_config()
                        url = cfg.get_playlist(playlist_name)
                        if url:
                            browser_control.open_url(url)
                    else:
                        media_control.play()
                elif action == "pause":
                    media_control.pause()
                elif action == "stop":
                    media_control.stop()
                elif action == "next":
                    media_control.next_track()
                elif action == "prev":
                    media_control.prev_track()
                elif action == "mute":
                    media_control.mute()
                elif action == "unmute":
                    media_control.unmute()
                elif action == "toggle_mute":
                    media_control.toggle_mute()
                return f"media:{action}"

            elif action_type == "wait":
                secs = float(step.get("seconds", 1))
                time.sleep(secs)
                return f"waited:{secs}s"

            elif action_type == "reminder":
                # BUG-C fix: parse optional 'time' field instead of always using now
                time_str = step.get("time", "")
                if time_str:
                    dt = self._parse_time_str(time_str)
                else:
                    dt = datetime.now() + timedelta(minutes=1)
                rid = self.db.add_reminder(step["text"], dt)
                if rid:
                    return f"reminder:{step['text']}"
                return f"reminder_failed:{step['text']}"

            elif action_type == "note":
                self.db.add_note(step["text"])
                return "note saved"

            elif action_type == "screenshot":
                from automation import system_control
                path = system_control.take_screenshot()
                return f"screenshot:{path}"

            elif action_type == "set_dnd":
                from automation import system_control
                system_control.set_dnd(step.get("enabled", True))
                return "dnd set"

            elif action_type == "workflow":
                # Nested workflow call
                return self.run_by_name(step.get("name", "")) or "workflow not found"

            else:
                logger.warning(f"Unknown step type: {action_type}")
                return f"unknown:{action_type}"

        except Exception as exc:
            logger.error(
                f"Step execution error ({action_type}): {exc}", exc_info=True
            )
            return f"error:{action_type}"

    # ── Helpers ──────────────────────────────────────────────────────────────

    def _get_config(self):
        from core.config import Config
        return Config()

    def _get_tts(self):
        """
        BUG-A/B mitigation: create a single local TTS instance for this
        workflow run if no shared instance was injected at construction time.
        """
        if not hasattr(self, "_local_tts") or self._local_tts is None:
            from core.tts import TextToSpeech
            self._local_tts = TextToSpeech(self._get_config())
        return self._local_tts

    @staticmethod
    def _parse_time_str(time_str: str) -> datetime:
        """Best-effort parse of a time string for reminder steps."""
        import re
        now = datetime.now()
        time_str = time_str.strip().lower()
        m = re.match(r"in (\d+) (minute|minutes|hour|hours)", time_str)
        if m:
            n    = int(m.group(1))
            unit = m.group(2)
            if "hour" in unit:
                return now + timedelta(hours=n)
            return now + timedelta(minutes=n)
        m = re.match(r"(\d{1,2}):(\d{2})\s*(am|pm)?", time_str)
        if m:
            h, mn = int(m.group(1)), int(m.group(2))
            period = m.group(3)
            if period == "pm" and h < 12:
                h += 12
            elif period == "am" and h == 12:
                h = 0
            dt = now.replace(hour=h, minute=mn, second=0, microsecond=0)
            if dt <= now:
                dt += timedelta(days=1)
            return dt
        return now + timedelta(minutes=1)
