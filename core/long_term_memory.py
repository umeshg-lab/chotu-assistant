"""
Long-Term Memory Engine — persistent user knowledge store.

Covers:
  - User profile facts (preferred browser, editor, work hours, etc.)
  - Relationship memory (people → roles/notes)
  - Alias map (user-defined phrase → canonical target)
  - Preference scoring (ranked choices per category with confidence)

This is NOT a raw note-dump. Every fact is structured with a category,
key, value, confidence score, and source (explicit / inferred / corrected).
"""

import logging
from typing import Optional, Dict, List, Any

logger = logging.getLogger("JARVIS.LongTermMemory")

# Categories used throughout the system
CAT_APPS        = "apps"          # preferred apps
CAT_BROWSER     = "browser"       # preferred browser
CAT_EDITOR      = "editor"        # preferred code/text editor
CAT_WORK        = "work"          # work hours, schedule
CAT_PERSONAL    = "personal"      # name, timezone, preferences
CAT_FOLDERS     = "folders"       # frequently used paths
CAT_MODES       = "modes"         # preferred modes by time/context
CAT_MEDIA       = "media"         # music preferences
CAT_SYSTEM      = "system"        # system preferences


class LongTermMemory:
    """
    All persistent user knowledge, structured by category.

    Design rules:
    - Every write has a category + key + value (never raw free text).
    - Confidence decreases on contradiction, increases on repetition.
    - Source tracks whether a fact was explicit (user told us),
      inferred (we learned from behaviour), or corrected (user fixed us).
    """

    def __init__(self, intel_db):
        self.db = intel_db
        # In-memory cache to avoid constant DB reads
        self._cache: Dict[str, Dict[str, str]] = {}
        self._load_cache()

    def _load_cache(self):
        rows = self.db.get_profile()
        for row in rows:
            cat = row["category"]
            if cat not in self._cache:
                self._cache[cat] = {}
            self._cache[cat][row["key"]] = row["value"]

    # ── Profile facts ─────────────────────────────────────────────────────────

    def learn(self, category: str, key: str, value: str,
              confidence: float = 1.0, source: str = "explicit"):
        """Store or update a fact about the user."""
        self.db.set_profile(category, key, value, confidence, source)
        if category not in self._cache:
            self._cache[category] = {}
        self._cache[category][key] = value
        logger.info(f"Learned [{category}] {key} = {value!r} "
                    f"(confidence={confidence:.2f}, source={source})")

    def recall_fact(self, category: str, key: str) -> Optional[str]:
        """Retrieve a profile fact. Returns None if not known."""
        cached = self._cache.get(category, {}).get(key)
        if cached:
            return cached
        return self.db.get_profile_value(category, key)

    def recall_category(self, category: str) -> Dict[str, str]:
        """Return all facts in a category as {key: value}."""
        cached = self._cache.get(category)
        if cached:
            return dict(cached)
        rows = self.db.get_profile(category)
        return {r["key"]: r["value"] for r in rows}

    def list_all(self) -> List[Dict]:
        return self.db.get_profile()

    # ── Convenience helpers (common profile questions) ─────────────────────────

    def preferred_browser(self) -> Optional[str]:
        return self.recall_fact(CAT_BROWSER, "preferred")

    def preferred_editor(self) -> Optional[str]:
        return self.recall_fact(CAT_EDITOR, "preferred")

    def user_name(self) -> Optional[str]:
        return self.recall_fact(CAT_PERSONAL, "name")

    def work_start_hour(self) -> Optional[str]:
        return self.recall_fact(CAT_WORK, "start_hour")

    def work_end_hour(self) -> Optional[str]:
        return self.recall_fact(CAT_WORK, "end_hour")

    # ── Relationship memory ───────────────────────────────────────────────────

    def remember_person(self, name: str, role: str = None, note: str = None):
        """Store or update information about a person."""
        self.db.upsert_relationship(name, role, note)
        logger.info(f"Person stored: {name!r} role={role!r} note={note!r}")

    def get_person(self, name: str) -> Optional[Dict]:
        return self.db.get_relationship(name)

    def all_people(self) -> List[Dict]:
        return self.db.get_all_relationships()

    def describe_person(self, name: str) -> str:
        """Return a human-readable summary of what we know about a person."""
        p = self.get_person(name)
        if not p:
            return f"I don't have any information about {name}."
        parts = [f"{name.title()}"]
        if p.get("role"):
            parts.append(f"is your {p['role']}")
        if p.get("notes"):
            parts.append(". Notes: " + "; ".join(p["notes"]))
        return " ".join(parts) + "."

    # ── Alias map ─────────────────────────────────────────────────────────────

    def learn_alias(self, alias: str, canonical: str,
                    category: str = "app", confidence: float = 1.0):
        """
        Store a user-defined alias.
        e.g. learn_alias("editor", "VS Code")
             learn_alias("design app", "Photoshop")
        """
        self.db.set_alias(alias, canonical, category, confidence)

    def resolve_alias(self, phrase: str) -> Optional[str]:
        """
        Resolve a user phrase to its canonical target.
        Returns None if no alias is known.
        """
        return self.db.resolve_alias(phrase)

    def all_aliases(self) -> List[Dict]:
        return self.db.get_all_aliases()

    # ── Preference scoring ────────────────────────────────────────────────────

    def upvote_preference(self, category: str, choice: str):
        """Record a positive signal for a choice in a category."""
        self.db.update_preference(category, choice, delta=0.1)

    def top_preference(self, category: str) -> Optional[str]:
        """Return the highest-scored choice for a category."""
        result = self.db.get_top_preference(category)
        return result["choice"] if result else None

    def preferences_with_scores(self, category: str) -> List[Dict]:
        return self.db.get_preferences(category)

    def confidence_str(self, category: str, choice: str) -> str:
        """Return a human-readable confidence string like 'VS Code (confidence 92%)'"""
        prefs = self.db.get_preferences(category)
        for p in prefs:
            if p["choice"].lower() == choice.lower():
                pct = int(p["score"] * 100)
                return f"{choice} (confidence {pct}%)"
        return choice

    # ── Natural language parsing helpers ─────────────────────────────────────

    def parse_and_learn(self, text: str) -> Optional[str]:
        """
        Attempt to extract a preference or fact from natural language.

        Examples:
          "my editor is VS Code"           → learns editor preference
          "Rahul is my client"             → stores relationship
          "I prefer Chrome"                → learns browser preference
          "I usually start work at 9 AM"   → learns work start_hour
          "call design app Photoshop"      → learns alias

        Returns a confirmation string if something was learned, else None.
        """
        import re
        lower = text.lower().strip()
        original = text.strip()

        # Pattern: "my Y is X"
        # Match on lowercase but extract value from original to preserve casing
        m = re.match(r"my (.+?) is (.+)", lower)
        if m:
            attr = m.group(1).strip()
            # Extract val from original text preserving case
            m2 = re.match(r"my .+? is (.+)", original, re.IGNORECASE)
            val = m2.group(1).strip() if m2 else m.group(2).strip()
            learned = self._learn_from_attr(attr, val, source="explicit")
            if learned:
                return learned
            if self._looks_like_person(val) and self._looks_like_role(attr):
                self.remember_person(val, role=attr)
                return f"Got it. I'll remember that {val.title()} is your {attr}."

        # Pattern: "X is my Y"
        m = re.match(r"(.+?) is my (.+)", lower)
        if m:
            attr = m.group(2).strip()
            m2 = re.match(r"(.+?) is my .+", original, re.IGNORECASE)
            val = m2.group(1).strip() if m2 else m.group(1).strip()
            learned = self._learn_from_attr(attr, val, source="explicit")
            if learned:
                return learned

            if self._looks_like_person(val) and self._looks_like_role(attr):
                self.remember_person(val, role=attr)
                return f"Got it. I'll remember that {val.title()} is your {attr}."

        # Pattern: "I prefer X" / "I use X"
        m = re.match(r"i (?:prefer|use|always use|usually use) (.+)", lower)
        if m:
            m2 = re.match(r"i (?:prefer|use|always use|usually use) (.+)", original, re.IGNORECASE)
            val = m2.group(1).strip() if m2 else m.group(1).strip()
            val_lower = val.lower()
            # Heuristic: if it sounds like a browser, record as browser
            if any(b in val_lower for b in ("chrome", "firefox", "edge", "brave", "opera")):
                self.learn(CAT_BROWSER, "preferred", val)
                self.learn_alias("browser", val, category="app")
                return f"Got it. I'll remember you prefer {val} as your browser."
            if any(e in val_lower for e in ("vs code", "vscode", "notepad", "sublime",
                                            "vim", "neovim", "atom", "intellij", "pycharm")):
                self.learn(CAT_EDITOR, "preferred", val)
                self.learn_alias("editor", val, category="app")
                return f"Got it. I'll remember your editor is {val}."

        # Pattern: "I usually start/end work at 9 AM"
        m = re.match(r"i (?:usually |normally )?(start|begin|end|finish) work at (.+)", lower)
        if m:
            key = "start_hour" if m.group(1) in {"start", "begin"} else "end_hour"
            m2 = re.match(
                r"i (?:usually |normally )?(?:start|begin|end|finish) work at (.+)",
                original,
                re.IGNORECASE,
            )
            val = m2.group(1).strip() if m2 else m.group(2).strip()
            self.learn(CAT_WORK, key, val, source="explicit")
            label = "start work" if key == "start_hour" else "end work"
            return f"Got it. I'll remember you usually {label} at {val}."

        # Pattern: "call X [my] Y" → alias
        m = re.match(r"call (.+?) (?:my )?(.+)", lower)
        if m:
            alias, canonical = m.group(1).strip(), m.group(2).strip()
            self.learn_alias(alias, canonical)
            return f"Understood. I'll treat '{alias}' as '{canonical}'."

        # Pattern: relationship — "Rahul is my client/boss/colleague"
        m = re.match(r"(\w+) is my (.+)", lower)
        if m:
            name, role = m.group(1).strip(), m.group(2).strip()
            self.remember_person(name, role=role)
            return f"Got it. I'll remember that {name.title()} is your {role}."

        return None

    @staticmethod
    def _looks_like_person(value: str) -> bool:
        words = value.strip().split()
        return 1 <= len(words) <= 3 and all(w[:1].isalpha() for w in words)

    @staticmethod
    def _looks_like_role(attr: str) -> bool:
        roles = {
            "client", "boss", "manager", "colleague", "coworker", "friend",
            "wife", "husband", "partner", "brother", "sister", "designer",
            "developer", "doctor", "accountant", "teacher", "mentor",
        }
        return attr.lower().strip() in roles or attr.lower().strip().endswith(" client")

    def _learn_from_attr(self, attr: str, val: str, source: str = "explicit") -> Optional[str]:
        attr_map = {
            "editor":        (CAT_EDITOR,  "preferred"),
            "text editor":   (CAT_EDITOR,  "preferred"),
            "code editor":   (CAT_EDITOR,  "preferred"),
            "browser":       (CAT_BROWSER, "preferred"),
            "web browser":   (CAT_BROWSER, "preferred"),
            "favorite app":   (CAT_APPS,    "favorite"),
            "favourite app":  (CAT_APPS,    "favorite"),
            "preferred app":  (CAT_APPS,    "preferred"),
            "name":          (CAT_PERSONAL,"name"),
            "work start":    (CAT_WORK,    "start_hour"),
            "work end":      (CAT_WORK,    "end_hour"),
            "work hours":     (CAT_WORK,    "hours"),
            "start time":    (CAT_WORK,    "start_hour"),
            "end time":      (CAT_WORK,    "end_hour"),
        }
        entry = attr_map.get(attr)
        if entry:
            cat, key = entry
            self.learn(cat, key, val, source=source)
            if cat == CAT_EDITOR and key == "preferred":
                self.learn_alias("editor", val, category="app")
            elif cat == CAT_BROWSER and key == "preferred":
                self.learn_alias("browser", val, category="app")
            return f"Got it. I'll remember your {attr} is {val}."
        return None
