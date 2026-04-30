"""
Manual training engine — allows users to teach JARVIS custom commands.
Supports trigger → action chain mapping, stored in SQLite.
"""

import re
import json
import logging
from typing import Optional

logger = logging.getLogger('JARVIS.Trainer')


class TrainingEngine:
    """
    Teaches JARVIS new voice-to-action mappings.

    Actions schema (list of dicts):
      {"type": "open_app",    "target": "chrome"}
      {"type": "open_url",    "url": "https://..."}
      {"type": "speak",       "text": "Hello!"}
      {"type": "run_mode",    "mode": "design"}
      {"type": "shell",       "command": "notepad.exe"}
      {"type": "set_volume",  "level": 60}
      {"type": "media",       "action": "play|pause|stop|next|prev"}
      {"type": "reminder",    "text": "...", "time": "HH:MM"}
      {"type": "note",        "text": "..."}
      {"type": "workflow",    "name": "morning routine"}
    """

    def __init__(self, db):
        self.db = db
        self._cache = {}
        self._refresh_cache()

    def _refresh_cache(self):
        cmds = self.db.get_trained_commands()
        self._cache = {c['trigger']: c for c in cmds}

    def match(self, text: str) -> Optional[dict]:
        """Try to match text against trained triggers. Returns command dict or None."""
        text = text.strip().lower()
        # Exact match
        if text in self._cache:
            self.db.increment_command_use(text)
            return self._cache[text]
        # Partial/contains match
        for trigger, cmd in self._cache.items():
            if trigger in text or text in trigger:
                self.db.increment_command_use(trigger)
                return cmd
        return None

    def teach(self, trigger: str, action_text: str):
        """
        Parse a natural-language action description into structured actions.
        Example: "open chrome and spotify and mute notifications"
        """
        actions = self._parse_action_text(action_text)
        self.db.add_trained_command(trigger.lower().strip(), actions)
        self._refresh_cache()
        logger.info(f"Trained command: '{trigger}' → {actions}")

    def teach_structured(self, trigger: str, actions: list):
        """Directly save structured actions."""
        self.db.add_trained_command(trigger.lower().strip(), actions)
        self._refresh_cache()
        logger.info(f"Trained (structured): '{trigger}' → {actions}")

    def forget(self, trigger: str):
        self.db.delete_trained_command(trigger.lower().strip())
        self._refresh_cache()
        logger.info(f"Forgotten: '{trigger}'")

    def list_commands(self) -> list:
        return list(self._cache.keys())

    def get_all(self) -> list:
        return self.db.get_trained_commands()

    def _parse_action_text(self, text: str) -> list:
        """Heuristic parser: text → list of action dicts."""
        actions = []
        text = text.lower()

        # Split on "and", "then", ","
        parts = re.split(r'\band\b|\bthen\b|,', text)

        for part in parts:
            part = part.strip()
            if not part:
                continue

            # open app
            m = re.match(r'open (.+)', part)
            if m:
                target = m.group(1).strip()
                if '.' in target or 'http' in target:
                    actions.append({"type": "open_url", "url": target})
                else:
                    actions.append({"type": "open_app", "target": target})
                continue

            # play music / playlist
            m = re.match(r'play (.+)', part)
            if m:
                playlist = m.group(1).strip()
                actions.append({"type": "media", "action": "play", "playlist": playlist})
                continue

            # mute
            if 'mute' in part:
                actions.append({"type": "media", "action": "mute"})
                continue

            # volume
            m = re.match(r'set volume to (\d+)', part)
            if m:
                actions.append({"type": "set_volume", "level": int(m.group(1))})
                continue

            # mode
            m = re.match(r'(?:activate|enable|switch to) (.+) mode', part)
            if m:
                actions.append({"type": "run_mode", "mode": m.group(1).strip()})
                continue

            # speak/say
            m = re.match(r'(?:say|speak|tell me) (.+)', part)
            if m:
                actions.append({"type": "speak", "text": m.group(1).strip()})
                continue

            # note
            m = re.match(r'take a? note(?: about)? (.+)', part)
            if m:
                actions.append({"type": "note", "text": m.group(1).strip()})
                continue

            # Fallback — try to open as app name
            actions.append({"type": "open_app", "target": part})

        return actions
