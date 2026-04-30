"""Local memory engine — logs commands, notes, key-value recall."""

import logging
from typing import Optional

logger = logging.getLogger('JARVIS.Memory')


class MemoryEngine:
    def __init__(self, db):
        self.db = db

    def log_command(self, command: str, response: str = '', mode: str = 'standard'):
        self.db.log_command(command, response, mode)

    def add_note(self, body: str, title: str = None):
        self.db.add_note(body, title)
        logger.info(f"Note saved: '{body[:50]}'")

    def get_notes(self, limit: int = 20):
        return self.db.get_notes(limit)

    def get_history(self, limit: int = 50):
        return self.db.get_history(limit)

    def remember(self, key: str, value: str):
        self.db.remember(key, value)
        logger.info(f"Remembered: {key} = {value}")

    def recall(self, key: str) -> Optional[str]:
        return self.db.recall(key)
