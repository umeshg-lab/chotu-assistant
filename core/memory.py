"""
Memory Engine — unified facade over all JARVIS memory systems.

Preserves the original public API (log_command, add_note, get_notes,
get_history, remember, recall) while adding intelligent sub-systems.
"""

import logging
from typing import Optional, List, Dict

logger = logging.getLogger("JARVIS.Memory")


class MemoryEngine:
    def __init__(self, db, intel_db=None, ltm=None, ctx=None, learn=None):
        self.db    = db
        self.intel = intel_db
        self.ltm   = ltm
        self.ctx   = ctx
        self.learn = learn

    # Legacy API
    def log_command(self, command: str, response: str = "", mode: str = "standard"):
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

    # Natural-language learning
    def parse_and_learn(self, text: str) -> Optional[str]:
        if self.ltm:
            return self.ltm.parse_and_learn(text)
        return None

    # Relationship memory
    def remember_person(self, name: str, role: str = None, note: str = None):
        if self.ltm:
            self.ltm.remember_person(name, role, note)

    def describe_person(self, name: str) -> str:
        if self.ltm:
            return self.ltm.describe_person(name)
        return f"I don't have information about {name}."

    def get_person(self, name: str) -> Optional[Dict]:
        if self.ltm:
            return self.ltm.get_person(name)
        return None

    # Alias management
    def learn_alias(self, alias: str, canonical: str, category: str = "app"):
        if self.ltm:
            self.ltm.learn_alias(alias, canonical, category)

    def resolve_alias(self, phrase: str) -> Optional[str]:
        if self.ltm:
            return self.ltm.resolve_alias(phrase)
        return None

    # Profile facts
    def recall_fact(self, category: str, key: str) -> Optional[str]:
        if self.ltm:
            return self.ltm.recall_fact(category, key)
        return None

    def learn_fact(self, category: str, key: str, value: str,
                   confidence: float = 1.0, source: str = "explicit"):
        if self.ltm:
            self.ltm.learn(category, key, value, confidence, source)

    # Behaviour logging
    def record_action(self, action: str, target: str = None, mode: str = None):
        if self.learn:
            self.learn.record_action(action, target, mode)

    def get_suggestions(self) -> List[str]:
        if self.learn:
            return self.learn.get_suggestions()
        return []

    # Short-term context
    def record_turn(self, user_text: str, intent: str = None,
                    entities: dict = None, response: str = None):
        if self.ctx:
            self.ctx.record_user(user_text, intent, entities)
            if response:
                self.ctx.record_response(response)

    def set_context_entity(self, key: str, value):
        if self.ctx:
            self.ctx.set_entity(key, value)

    def get_context_entity(self, key: str):
        if self.ctx:
            return self.ctx.get_entity(key)
        return None

    def resolve_context(self, text: str) -> str:
        if self.ctx:
            return self.ctx.resolve(text)
        return text
