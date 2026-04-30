"""
Reflection Engine — daily and weekly analysis of user behaviour.

Runs once per day (at configurable time) and:
  1. Summarises what happened today / this week
  2. Detects new habits or changed patterns
  3. Identifies important new people mentioned
  4. Suggests workflow candidates
  5. Highlights missed opportunities (e.g. heavy Photoshop use → suggest Design Mode)
  6. Persists a reflection record to the DB

The engine is designed to produce actionable insights, not just statistics.
"""

import logging
import threading
from datetime import datetime, timedelta
from typing import List, Dict, Optional

logger = logging.getLogger("JARVIS.Reflection")

# Run reflection once per day at this hour (24h)
REFLECTION_HOUR = 23


class ReflectionEngine:
    """
    Produces periodic summaries and suggestions.
    Designed to run as a background daemon thread.
    """

    def __init__(self, intel_db, learning_engine, long_term_memory, tts):
        self.db      = intel_db
        self.learn   = learning_engine
        self.ltm     = long_term_memory
        self.tts     = tts
        self._stop   = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._last_reflection_date: Optional[str] = None

    # ── Lifecycle ─────────────────────────────────────────────────────────────

    def start(self):
        self._stop.clear()
        self._thread = threading.Thread(
            target=self._loop, daemon=True, name="JarvisReflection"
        )
        self._thread.start()
        logger.info("Reflection engine started.")

    def stop(self):
        self._stop.set()
        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=5)

    def _loop(self):
        while not self._stop.is_set():
            now = datetime.now()
            today_str = now.strftime("%Y-%m-%d")

            # Run once per day around REFLECTION_HOUR
            if (now.hour == REFLECTION_HOUR
                    and self._last_reflection_date != today_str):
                self._last_reflection_date = today_str
                self.run_daily_reflection()

            # Sleep 10 minutes between checks
            self._stop.wait(600)

    # ── Daily reflection ──────────────────────────────────────────────────────

    def run_daily_reflection(self) -> Dict:
        """
        Perform a full daily reflection.
        Returns the reflection record dict.
        """
        logger.info("Running daily reflection…")
        insights = []
        now      = datetime.now()

        # 1. Top apps today
        top = self.db.get_top_behaviors(days=1, limit=10)
        if top:
            app_names = [r["target"] for r in top if r.get("target")][:3]
            if app_names:
                insights.append(
                    f"Most used today: {', '.join(app_names)}."
                )

        # 2. Photoshop / Design tool frequency → suggest mode
        design_apps = {"photoshop", "figma", "illustrator", "affinity", "canva", "xd"}
        design_usage = sum(
            r["freq"] for r in top
            if r.get("target", "").lower() in design_apps
        )
        if design_usage >= 5:
            insights.append(
                f"You opened design tools {design_usage} times today. "
                f"Want me to create a Design Priority Mode that opens them all at once?"
            )

        # 3. Detect new workflow candidates
        wf = self.learn.detect_workflow_candidate(days=7)
        if wf:
            insights.append(wf["suggestion"])

        # 4. Detect new people mentioned in notes / relationships
        new_people = self._detect_new_people()
        if new_people:
            for name in new_people:
                insights.append(
                    f"I noticed you mentioned '{name}' recently. "
                    f"Want me to remember who they are?"
                )

        # 5. Mode usage patterns
        mode_patterns = self._detect_mode_patterns()
        if mode_patterns:
            insights.extend(mode_patterns)

        summary = self._build_summary(top, insights)
        self.db.save_reflection(
            period=now.strftime("%Y-%m-%d"),
            summary=summary,
            insights=insights,
        )
        logger.info(f"Daily reflection complete. {len(insights)} insights.")

        # Speak a brief summary if there are actionable insights
        if insights:
            self.tts.speak_async(
                f"Daily reflection: {insights[0]} "
                f"Check the dashboard for the full report."
            )

        return {"summary": summary, "insights": insights}

    # ── Weekly reflection ─────────────────────────────────────────────────────

    def run_weekly_reflection(self) -> Dict:
        """Called externally (e.g. from UI button) for a full weekly report."""
        stats    = self.learn.weekly_stats()
        insights = []

        top_apps = stats.get("top_apps", [])
        if top_apps:
            top_str = ", ".join(
                f"{a['app']} ({a['count']}x)" for a in top_apps[:3]
            )
            insights.append(f"Most used this week: {top_str}.")

        total = stats.get("total_actions", 0)
        insights.append(f"You gave JARVIS {total} commands this week.")

        active_hour = stats.get("most_active_hour")
        if active_hour is not None:
            period = "morning" if active_hour < 12 else (
                "afternoon" if active_hour < 17 else "evening"
            )
            insights.append(f"Most active period: {period} ({active_hour:02d}:00).")

        # App frequency suggestions
        for app in top_apps:
            if app["count"] >= 14:
                insights.append(
                    f"You opened {app['app']} {app['count']} times this week. "
                    f"Should I add it to your startup mode?"
                )
                break

        now = datetime.now()
        summary = (
            f"Week ending {now.strftime('%Y-%m-%d')}: "
            f"{total} total actions. "
            + " ".join(insights)
        )
        self.db.save_reflection(
            period=f"week-{now.strftime('%Y-W%U')}",
            summary=summary,
            insights=insights,
        )
        return {"summary": summary, "insights": insights}

    # ── Internal helpers ──────────────────────────────────────────────────────

    def _detect_new_people(self) -> List[str]:
        """
        Scan recent command history for names that aren't in relationship memory.
        Very basic: looks for capitalized words preceded by 'with', 'from', 'for'.
        """
        import re
        known = {r["name"].lower() for r in self.ltm.all_people()}
        new_names = []
        texts = self.db.get_recent_context_texts(days=7, limit=200)
        pattern = re.compile(
            r"\b(?:with|from|for|call|meet|meeting|message|email)\s+([A-Z][a-z]+(?:\s+[A-Z][a-z]+)?)\b"
        )

        for text in texts:
            for match in pattern.finditer(text):
                name = match.group(1).strip()
                key = name.lower()
                if key not in known and key not in {n.lower() for n in new_names}:
                    new_names.append(name)

        return new_names[:5]

    def _detect_mode_patterns(self) -> List[str]:
        """Detect if user consistently uses same mode at same time."""
        patterns = self.db.get_behavior_frequency(action="mode_activate", days=14)
        suggestions = []
        for p in patterns:
            if p["freq"] >= 5 and p.get("target"):
                from core.learning_engine import LearningEngine
                period = LearningEngine._time_period(p.get("hour", 12))
                suggestions.append(
                    f"You activate {p['target']} mode every {period} "
                    f"({p['freq']}x in 2 weeks). Want me to auto-activate it?"
                )
        return suggestions[:2]

    def _build_summary(self, top_behaviors: List[Dict], insights: List[str]) -> str:
        if not top_behaviors and not insights:
            return "Quiet day — no significant patterns detected."
        parts = []
        if top_behaviors:
            apps = [r["target"] for r in top_behaviors if r.get("target")][:3]
            if apps:
                parts.append(f"Most used: {', '.join(apps)}")
        if insights:
            parts.append(f"{len(insights)} insight(s) generated")
        return ". ".join(parts) + "."

    # ── On-demand report ──────────────────────────────────────────────────────

    def get_latest(self) -> Optional[Dict]:
        return self.db.get_latest_reflection()
