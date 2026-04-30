"""
Learning Engine — makes JARVIS improve over time.

Features implemented here:
  1. learning_from_corrections — when user says "no, X", JARVIS updates its alias map
  2. routine_detection         — detect repeated app/action sequences by time-of-day
  3. smart_suggestions         — generate proactive recommendations based on patterns
  4. preference_scoring        — track and rank preferred choices per category
  5. time_based_learning       — differentiate weekday vs weekend, morning vs night
"""

import re
import logging
from datetime import datetime
from typing import Optional, Dict, List, Tuple

logger = logging.getLogger("JARVIS.Learning")


class LearningEngine:
    """
    Observes user behavior and corrections, updates memory accordingly.
    All state lives in IntelligenceDB — this class is stateless itself.
    """

    def __init__(self, intel_db, long_term_memory):
        self.db  = intel_db
        self.ltm = long_term_memory

    # ── Correction learning ───────────────────────────────────────────────────

    # Phrases that signal a correction
    _CORRECTION_PATTERNS = [
        re.compile(r"^no[,.]?\s+(?:open|use|launch|start)?\s*(.+)$", re.IGNORECASE),
        re.compile(r"^not (?:that|this)[,.]?\s+(?:open|use)?\s*(.+)$", re.IGNORECASE),
        re.compile(r"^i meant (.+)$", re.IGNORECASE),
        re.compile(r"^wrong[,.]?\s+(.+)$", re.IGNORECASE),
        re.compile(r"^actually[,.]?\s+(?:open|use)?\s*(.+)$", re.IGNORECASE),
    ]

    def detect_correction(self, text: str, last_intent: str,
                          last_target: str) -> Optional[Tuple[str, str]]:
        """
        Check if the user is correcting JARVIS's previous action.

        Returns (alias_from, alias_to) if a correction is detected, else None.

        Example:
            last_intent = "app_open", last_target = "notepad"
            text = "no, VS Code"
            → returns ("editor", "VS Code")   (alias for what they said last time)
        """
        for pattern in self._CORRECTION_PATTERNS:
            m = pattern.match(text.strip())
            if m:
                correct_val = m.group(1).strip()
                # Determine what alias to map
                # If the user said "open editor" last turn and we opened Notepad,
                # we need to map "editor" → correct_val.
                if last_intent == "app_open" and last_target:
                    alias = last_target  # the phrase they used that led to the wrong result
                    logger.info(
                        f"Correction detected: '{alias}' should open '{correct_val}'"
                    )
                    return (alias, correct_val)
        return None

    def apply_correction(self, alias: str, canonical: str):
        """Permanently update alias and boost preference score."""
        self.ltm.learn_alias(alias, canonical, category="app", confidence=1.0)
        alias_key = str(alias or "").strip().lower()
        if alias_key in {"editor", "code editor", "text editor"}:
            self.ltm.learn("editor", "preferred", canonical, source="corrected")
        elif alias_key in {"browser", "web browser"}:
            self.ltm.learn("browser", "preferred", canonical, source="corrected")
        self.ltm.upvote_preference(f"app:{alias}", canonical)
        logger.info(f"Correction applied: '{alias}' → '{canonical}' (permanently learned)")

    # ── Behavior logging ──────────────────────────────────────────────────────

    def record_action(self, action: str, target: str = None, mode: str = None):
        """Log an action to the behavior pattern store."""
        self.db.log_behavior(action, target, mode)
        # Also upvote preference for this target within the action category
        if target:
            self.ltm.upvote_preference(f"action:{action}", target)

    # ── Routine detection ─────────────────────────────────────────────────────

    def detect_routines(self, days: int = 14) -> List[Dict]:
        """
        Identify repeated action patterns.
        Returns a list of routines with frequency and time-of-day information.

        A routine is defined as the same action+target appearing ≥3 times
        at the same hour±1 band.
        """
        raw = self.db.get_behavior_frequency(days=days)
        # Filter to significant frequency (≥3 occurrences)
        routines = []
        for row in raw:
            if row["freq"] >= 3:
                routines.append({
                    "action":  row["action"],
                    "target":  row["target"],
                    "hour":    row["hour"],
                    "weekday": row["weekday"],
                    "freq":    row["freq"],
                    "label":   self._label_routine(row),
                })
        return sorted(routines, key=lambda r: r["freq"], reverse=True)

    def _label_routine(self, row: Dict) -> str:
        hour  = row.get("hour", 0)
        action = row.get("action", "")
        target = row.get("target", "")
        period = self._time_period(hour)
        if target:
            return f"{period} {action} {target}"
        return f"{period} {action}"

    @staticmethod
    def _time_period(hour: int) -> str:
        if 5 <= hour < 12:
            return "morning"
        if 12 <= hour < 17:
            return "afternoon"
        if 17 <= hour < 21:
            return "evening"
        return "night"

    # ── Smart suggestions ─────────────────────────────────────────────────────

    def get_suggestions(self, current_hour: int = None,
                        current_weekday: int = None) -> List[str]:
        """
        Generate proactive suggestions based on current time and past patterns.

        Returns a list of natural-language suggestion strings.
        """
        now = datetime.now()
        hour    = current_hour    if current_hour    is not None else now.hour
        weekday = current_weekday if current_weekday is not None else now.weekday()

        routines = self.detect_routines(days=14)
        suggestions = []

        for r in routines[:5]:  # Top 5 candidates
            if abs(r.get("hour", -99) - hour) <= 1:
                # This routine typically happens around now
                action = r["action"]
                target = r.get("target", "")
                freq   = r["freq"]

                if action == "app_open" and target:
                    suggestions.append(
                        f"You usually open {target} around this time "
                        f"({freq}x in past 2 weeks). Should I open it?"
                    )
                elif action == "mode_activate" and target:
                    suggestions.append(
                        f"You typically activate {target} mode now "
                        f"({freq}x in past 2 weeks). Should I activate it?"
                    )

        # Weekly pattern: certain days have certain behaviours
        top = self.db.get_top_behaviors(days=30, limit=10)
        for row in top:
            if row["freq"] >= 10 and row.get("target"):
                suggestions.append(
                    f"You've used {row['target']} {row['freq']} times this month. "
                    f"Should I create a quick-access shortcut?"
                )
                break  # One macro suggestion is enough

        return suggestions[:3]  # Cap at 3 suggestions

    # ── Workflow pattern detection ─────────────────────────────────────────────

    def detect_workflow_candidate(self, days: int = 7) -> Optional[Dict]:
        """
        Look for repeated multi-app sequences that could be turned into a workflow.

        Simplified approach: find top 3 apps opened in the same hour on 3+ days.
        Returns a suggestion dict or None.
        """
        raw = self.db.get_behavior_frequency(action="app_open", days=days)
        # Group by hour
        by_hour: Dict[int, List[str]] = {}
        for row in raw:
            if row.get("target") and row["freq"] >= 3:
                h = row["hour"]
                if h not in by_hour:
                    by_hour[h] = []
                by_hour[h].append(row["target"])

        for hour, apps in by_hour.items():
            if len(apps) >= 3:
                period = self._time_period(hour)
                return {
                    "apps":   apps[:5],
                    "hour":   hour,
                    "period": period,
                    "suggestion": (
                        f"You open {', '.join(apps[:3])} together every {period}. "
                        f"Want me to create a '{period.title()} Workflow' for this?"
                    )
                }
        return None

    # ── Weekly reflection input ───────────────────────────────────────────────

    def weekly_stats(self) -> Dict:
        """
        Compute stats for the weekly reflection report.
        Returns dict with top apps, most active day/hour, and any new patterns.
        """
        top_behaviors = self.db.get_top_behaviors(days=7, limit=20)
        now = datetime.now()

        top_apps = [
            {"app": r["target"], "count": r["freq"]}
            for r in top_behaviors
            if r["action"] == "app_open" and r.get("target")
        ][:5]

        # Most active hour (by total actions)
        all_behaviors = self.db.get_behavior_frequency(days=7)
        hour_counts: Dict[int, int] = {}
        for r in all_behaviors:
            h = r.get("hour", 0)
            hour_counts[h] = hour_counts.get(h, 0) + r["freq"]
        most_active_hour = max(hour_counts, key=hour_counts.get) if hour_counts else None

        return {
            "week_ending": now.strftime("%Y-%m-%d"),
            "top_apps":   top_apps,
            "most_active_hour": most_active_hour,
            "total_actions": sum(r["freq"] for r in top_behaviors),
        }
