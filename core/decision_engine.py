"""
Decision Engine — replaces hard intent failure with intelligent fallback.

Architecture:
  1. Alias resolution    — expand user shorthand before intent matching
  2. Confidence scoring  — every match gets a 0.0-1.0 confidence score
  3. Semantic fallback   — fuzzy matching when regex fails
  4. Clarification flow  — asks a targeted question instead of failing silently
  5. Recovery responses  — "I think you want X. Should I do that?"

This layer sits BETWEEN raw text input and the intent regex engine.
It pre-processes text and post-processes failures.
"""

import re
import logging
from difflib import SequenceMatcher
from typing import Optional, Tuple, List, Dict

logger = logging.getLogger("JARVIS.Decision")

# Minimum confidence to act without asking for confirmation
ACT_THRESHOLD     = 0.72
# Minimum confidence to make a guess and ask
SUGGEST_THRESHOLD = 0.40


class DecisionEngine:
    """
    Wraps the intent engine with intelligent pre/post processing.

    Usage:
        result = decision_engine.process(raw_text)
        # result is always a non-empty string — never silent failure
    """

    def __init__(self, intent_engine, long_term_memory, context_engine,
                 learning_engine, tts):
        self.intent  = intent_engine
        self.ltm     = long_term_memory
        self.ctx     = context_engine
        self.learn   = learning_engine
        self.tts     = tts

        # Track last action for correction detection
        self._last_intent_label: Optional[str] = None
        self._last_target:       Optional[str] = None

        # Pre-built semantic vocab for fuzzy fallback
        self._semantic_vocab = self._build_semantic_vocab()

    # ── Semantic vocabulary ────────────────────────────────────────────────────

    def _build_semantic_vocab(self) -> List[Tuple[str, str]]:
        """
        Map natural-language paraphrases → canonical commands.
        Adds semantic coverage that regex alone cannot provide.
        """
        return [
            # Volume synonyms
            ("lower the sound",         "volume down"),
            ("turn it down",            "volume down"),
            ("quieter please",          "volume down"),
            ("can you lower the volume","volume down"),
            ("reduce volume",           "volume down"),
            ("make it louder",          "volume up"),
            ("turn it up",              "volume up"),
            ("increase the sound",      "volume up"),
            ("louder please",           "volume up"),
            # Media synonyms
            ("start the music",         "play music"),
            ("put some music on",       "play music"),
            ("play something",          "play music"),
            ("stop the music",          "stop music"),
            ("pause the song",          "pause music"),
            ("next track",              "next song"),
            ("skip this song",          "next song"),
            ("skip",                    "next song"),
            # App synonyms
            ("fire up chrome",          "open chrome"),
            ("bring up chrome",         "open chrome"),
            ("launch the browser",      "open browser"),
            ("open my editor",          "open editor"),
            ("start coding",            "open editor"),
            # System synonyms
            ("grab a screenshot",       "take a screenshot"),
            ("capture the screen",      "take a screenshot"),
            ("turn off the pc",         "shutdown"),
            ("power down",              "shutdown"),
            ("lock my pc",              "lock the pc"),
            # Reminder/Note synonyms
            ("don't let me forget",     "remind me"),
            ("make a note",             "take a note"),
            ("write this down",         "take a note"),
            ("jot this down",           "take a note"),
            # Greeting synonyms
            ("you there",               "hello"),
            ("wake up",                 "hello"),
            ("are you listening",       "hello"),
        ]

    # ── Main entry point ──────────────────────────────────────────────────────

    def process(self, raw_text: str) -> str:
        """
        Process user input through the full decision pipeline.
        Always returns a non-empty string (never fails silently).
        """
        text = raw_text.strip()
        if not text:
            return ""

        # Step 1: Check if this is a correction of a previous action
        correction = self.learn.detect_correction(
            text, self._last_intent_label or "", self._last_target or ""
        )
        if correction:
            alias, canonical = correction
            self.learn.apply_correction(alias, canonical)
            response = (
                f"Got it. I'll remember that '{alias}' means {canonical}. "
                f"Opening {canonical} now."
            )
            self.tts.speak_async(response)
            # Re-process with the corrected target
            text = f"open {canonical}"

        # Step 2: Pronoun/reference resolution against current session context
        resolved_text = self.ctx.resolve(text)
        if resolved_text != text:
            logger.info(f"Context resolved: '{text}' → '{resolved_text}'")
            text = resolved_text

        # Step 3: Alias expansion using long-term memory
        expanded_text = self._expand_aliases(text)
        if expanded_text != text:
            logger.info(f"Alias expanded: '{text}' → '{expanded_text}'")
            text = expanded_text

        # Step 4: Answer memory/profile questions before regex fallback so
        # known facts do not show up as unresolved intents in logs.
        memory_response = self._handle_memory_query(text)
        if memory_response:
            self.tts.speak_async(memory_response)
            return memory_response

        # Step 5: Try the intent engine directly
        response = self.intent.process(
            text, log_history=False, use_fallback=False
        )
        if self._has_response(response):
            self._update_last_action(text)
            return str(response)

        # Step 6: Semantic fuzzy fallback
        canonical, confidence = self._semantic_match(text)
        if canonical:
            if confidence >= ACT_THRESHOLD:
                # High confidence — just do it
                logger.info(
                    f"Semantic match (high confidence={confidence:.2f}): "
                    f"'{text}' → '{canonical}'"
                )
                response = self.intent.process(
                    canonical, log_history=False, use_fallback=False
                )
                if self._has_response(response):
                    self._update_last_action(canonical)
                    return str(response)

            elif confidence >= SUGGEST_THRESHOLD:
                # Medium confidence — ask first
                msg = (
                    f"I think you want to {canonical}. "
                    f"Should I do that?"
                )
                self.tts.speak_async(msg)
                self.ctx.set_entity("pending_action", canonical)
                return f"Clarifying: {canonical} (confidence={confidence:.0%})"

        # Step 7: Check if user is confirming a pending action ("yes", "go ahead")
        pending = self._handle_confirmation(text)
        if pending:
            return pending

        # Step 8: Check for memory/profile queries after semantic fallback too,
        # in case context or alias expansion changed the phrasing.
        memory_response = self._handle_memory_query(text)
        if memory_response:
            self.tts.speak_async(memory_response)
            return memory_response

        # Step 9: Final fallback — ask a clarifying question
        return self._clarify(text)

    # ── Alias expansion ───────────────────────────────────────────────────────

    def _expand_aliases(self, text: str) -> str:
        """
        Expand any known alias in the text to its canonical form.
        e.g. "open editor" → "open VS Code" if alias editor=VS Code
        """
        words = text.split()
        # Try multi-word aliases first (up to 3-word phrases)
        for n in (3, 2, 1):
            for i in range(len(words) - n + 1):
                phrase = " ".join(words[i:i + n])
                canonical = self.ltm.resolve_alias(phrase)
                if not canonical and phrase == "editor":
                    canonical = self.ltm.preferred_editor()
                if not canonical and phrase == "browser":
                    canonical = self.ltm.preferred_browser()
                if canonical:
                    words[i:i + n] = [canonical]
                    return " ".join(words)
        return text

    # ── Semantic matching ─────────────────────────────────────────────────────

    def _semantic_match(self, text: str) -> Tuple[Optional[str], float]:
        """
        Find the closest match in the semantic vocabulary using string similarity.
        Returns (canonical_command, confidence_score).
        """
        best_cmd   = None
        best_score = 0.0
        text_lower = text.lower()

        for phrase, canonical in self._semantic_vocab:
            # Check for substring containment first (fast path)
            if phrase in text_lower:
                return canonical, 0.95

            # Full string similarity
            score = SequenceMatcher(None, text_lower, phrase).ratio()
            if score > best_score:
                best_score = score
                best_cmd   = canonical

        return (best_cmd, best_score) if best_score > 0 else (None, 0.0)

    # ── Confirmation handling ─────────────────────────────────────────────────

    _YES_WORDS = re.compile(r"^(yes|yeah|yep|sure|go ahead|do it|ok|okay|please)\.?$",
                            re.IGNORECASE)

    def _handle_confirmation(self, text: str) -> Optional[str]:
        """Execute a pending action if the user confirms it."""
        pending = self.ctx.get_entity("pending_action")
        if pending and self._YES_WORDS.match(text.strip()):
            self.ctx.set_entity("pending_action", None)
            logger.info(f"User confirmed pending action: {pending}")
            response = self.intent.process(
                pending, log_history=False, use_fallback=False
            )
            self._update_last_action(pending)
            return str(response) if response is not None else None
        return None

    @staticmethod
    def _has_response(response) -> bool:
        return response is not None and str(response).strip() != ""

    # ── Memory/profile queries ─────────────────────────────────────────────────

    _MEMORY_QUERIES = [
        (re.compile(r"who is (\w+)", re.I),            "who"),
        (re.compile(r"what is my (.+)", re.I),          "what_mine"),
        (re.compile(r"what'?s? my (.+)", re.I),         "what_mine"),
        (re.compile(r"do you (?:know|remember) (.+)", re.I), "remember"),
    ]

    def _handle_memory_query(self, text: str) -> Optional[str]:
        for pattern, qtype in self._MEMORY_QUERIES:
            m = pattern.search(text)
            if not m:
                continue
            subject = m.group(1).strip().lower()

            if qtype == "who":
                person = self.ltm.get_person(subject)
                if person:
                    return self.ltm.describe_person(subject)

            elif qtype in ("what_mine", "remember"):
                if subject in ("editor", "code editor", "text editor"):
                    val = self.ltm.preferred_editor()
                    if val:
                        return f"Your editor is {val}."
                if subject in ("browser", "web browser"):
                    val = self.ltm.preferred_browser()
                    if val:
                        return f"Your browser is {val}."
                # Try common profile categories
                for cat in ("editor", "browser", "work", "personal"):
                    val = self.ltm.recall_fact(cat, subject)
                    if val:
                        return f"Your {subject} is {val}."
                # Try alias
                alias_val = self.ltm.resolve_alias(subject)
                if alias_val:
                    return f"You've told me '{subject}' refers to {alias_val}."

        return None

    # ── Clarification ─────────────────────────────────────────────────────────

    _CLARIFY_TEMPLATES = [
        "I didn't quite catch that. Did you want to open something, set a reminder, or do something else?",
        "I'm not sure I understood. Can you rephrase that?",
        "Sorry, I'm not sure what you mean. Try saying something like 'open Chrome' or 'remind me at 3 PM'.",
        "I didn't get that. Could you be more specific?",
    ]
    _clarify_index = 0

    def _clarify(self, text: str) -> str:
        """Rotate through clarification prompts instead of silent failure."""
        msg = self._CLARIFY_TEMPLATES[
            self._clarify_index % len(self._CLARIFY_TEMPLATES)
        ]
        self._clarify_index += 1
        self.tts.speak_async(msg)
        logger.info(f"Clarification issued for: '{text}'")
        return f"Clarifying (unmatched: '{text}')"

    # ── Internal state tracking ───────────────────────────────────────────────

    def _update_last_action(self, text: str):
        """Track what the last resolved intent was for correction detection."""
        lower = text.lower()
        if re.search(r"\bopen\b|\blaunch\b|\bstart\b", lower):
            m = re.search(r"(?:open|launch|start)\s+(.+)", lower)
            self._last_intent_label = "app_open"
            self._last_target = m.group(1).strip() if m else None
        elif re.search(r"\bvolume\b", lower):
            self._last_intent_label = "volume"
            self._last_target = None
        elif re.search(r"\bremind\b", lower):
            self._last_intent_label = "reminder_set"
            self._last_target = None
        else:
            self._last_intent_label = "unknown"
            self._last_target = None
