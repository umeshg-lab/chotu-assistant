"""
Short-Term Context Engine — manages the rolling conversation window.

Provides:
  - Entity tracking across turns (pronoun/reference resolution)
  - Task continuity (remembering what "it" refers to)
  - Session-scoped context that resets when JARVIS goes idle
"""

import re
import uuid
import logging
from datetime import datetime, timedelta
from typing import Optional, Dict, Any, List

logger = logging.getLogger("JARVIS.Context")

# How long a session stays "active" without a new utterance
SESSION_TIMEOUT_MINUTES = 30


class ShortTermContext:
    """
    Manages the in-session rolling context window.

    Tracks:
      - Last spoken entity (subject of current conversation)
      - Last intent that was executed
      - Last reminder / note / workflow referenced
      - Pronoun antecedents for 'it', 'that', 'the same one', etc.
    """

    def __init__(self, intel_db):
        self.db = intel_db
        self._session_id: str = self._new_session()
        self._last_active: datetime = datetime.now()

        # In-memory entity register (fast lookup, no DB round-trip)
        self._entities: Dict[str, Any] = {}
        # Last executed intent label
        self._last_intent: Optional[str] = None
        # Last spoken raw text
        self._last_user_text: Optional[str] = None
        # Last assistant response
        self._last_response: Optional[str] = None

    # ── Session management ────────────────────────────────────────────────────

    def _new_session(self) -> str:
        sid = str(uuid.uuid4())
        logger.info(f"New context session: {sid}")
        return sid

    def _check_session_timeout(self):
        """Start a fresh session if idle for too long."""
        idle = datetime.now() - self._last_active
        if idle > timedelta(minutes=SESSION_TIMEOUT_MINUTES):
            logger.info("Session timed out — starting fresh context")
            self._session_id = self._new_session()
            self._entities.clear()
            self._last_intent = None
            self._last_user_text = None
            self._last_response = None

    # ── Public API ────────────────────────────────────────────────────────────

    def record_user(self, text: str, intent: str = None, entities: dict = None):
        """Call this when a user utterance arrives."""
        self._check_session_timeout()
        self._last_active = datetime.now()
        self._last_user_text = text
        if intent:
            self._last_intent = intent
        if entities:
            self._entities.update(entities)
        # Persist to DB for cross-session analysis
        self.db.add_context(
            self._session_id, "user", text, intent, entities
        )

    def record_response(self, text: str):
        """Call this when JARVIS speaks a response."""
        self._last_active = datetime.now()
        self._last_response = text
        self.db.add_context(self._session_id, "assistant", text)

    def set_entity(self, key: str, value: Any):
        """Store a named entity in the current session."""
        self._entities[key] = value
        logger.debug(f"Context entity set: {key} = {value}")

    def get_entity(self, key: str) -> Optional[Any]:
        return self._entities.get(key)

    @property
    def last_intent(self) -> Optional[str]:
        return self._last_intent

    @property
    def last_user_text(self) -> Optional[str]:
        return self._last_user_text

    def get_history(self, limit: int = 10) -> List[Dict]:
        return self.db.get_context(self._session_id, limit)

    # ── Pronoun / reference resolution ───────────────────────────────────────

    # Words that indicate a follow-up referring to a previous entity
    _PRONOUNS = re.compile(
        r"\b(it|that|this|the same one|the same|that one|this one)\b",
        re.IGNORECASE
    )

    # Time-shift phrases that modify a previous entity's time
    _TIME_SHIFTS = re.compile(
        r"\b(move|reschedule|change|push|shift)\b.{0,20}\b(to|at|for)\b",
        re.IGNORECASE
    )

    def resolve(self, text: str) -> str:
        """
        Given a user utterance, attempt to expand pronouns by injecting
        context from the entity register.

        Returns an annotated text string (or original if nothing to resolve).

        Examples
        --------
        Session: user set entity 'reminder_text' = 'client call'
        Input:   "move it to 4 PM"
        Output:  "move client call reminder to 4 PM"
        """
        lower = text.lower()

        # Only bother if there's a pronoun/reference word in the text
        if not self._PRONOUNS.search(lower):
            return text

        resolved = text

        # Try to resolve against the most recently established entity
        priority_keys = [
            "reminder_text", "note_text", "last_app", "last_workflow",
            "last_mode", "last_search"
        ]
        for key in priority_keys:
            val = self._entities.get(key)
            if val:
                # Replace the pronoun/reference with the actual entity value
                resolved = self._PRONOUNS.sub(str(val), resolved, count=1)
                logger.info(
                    f"Resolved pronoun in '{text}' → '{resolved}' "
                    f"(via entity '{key}'='{val}')"
                )
                break

        return resolved

    def extract_entities(self, text: str, intent: str) -> Dict[str, Any]:
        """
        Simple rule-based entity extraction for common intents.
        Returns a dict of {entity_name: value} to be stored in session.
        """
        entities: Dict[str, Any] = {}
        lower = text.lower()

        if intent in ("reminder_set", "reminder_at"):
            # Extract the task text portion
            m = re.search(r"remind me(?:\s+to)?\s+(.+?)(?:\s+at\s+.+)?$", lower)
            if m:
                entities["reminder_text"] = m.group(1).strip()

        elif intent == "note":
            m = re.search(r"note[:\s]+(.+)", lower)
            if m:
                entities["note_text"] = m.group(1).strip()

        elif intent == "app_open":
            m = re.search(r"(?:open|launch|start)\s+(.+)", lower)
            if m:
                entities["last_app"] = m.group(1).strip()

        elif intent == "workflow_run":
            m = re.search(r"run workflow\s+(.+)", lower)
            if m:
                entities["last_workflow"] = m.group(1).strip()

        elif intent == "mode":
            m = re.search(r"(?:activate|switch to|enable)?\s*(\w+)\s*mode", lower)
            if m:
                entities["last_mode"] = m.group(1).strip()

        elif intent == "search":
            m = re.search(r"search(?:\s+for)?\s+(.+)", lower)
            if m:
                entities["last_search"] = m.group(1).strip()

        return entities
