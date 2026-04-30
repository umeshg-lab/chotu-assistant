"""
JARVIS - Personal AI Desktop Assistant
Fully offline, privacy-first, Windows-native

INTELLIGENCE UPGRADE (main.py):
  Added initialisation of all AI intelligence sub-systems:
    - IntelligenceDB     : new structured AI tables
    - ShortTermContext   : session-scoped pronoun/reference resolution
    - LongTermMemory     : persistent user profile, relationships, aliases
    - LearningEngine     : correction learning, routine detection, suggestions
    - DecisionEngine     : replaces regex-only execution with semantic reasoning
    - ReflectionEngine   : daily/weekly behaviour analysis

  MemoryEngine is now constructed with all AI sub-systems injected.
  The wake callback routes through DecisionEngine instead of IntentEngine directly.
  ReflectionEngine starts alongside the reminder scheduler.
"""

import sys
import os
import logging
import threading
from pathlib import Path

_PROJECT_ROOT = Path(__file__).resolve().parent
os.chdir(_PROJECT_ROOT)

_root_logger = logging.getLogger()
if not _root_logger.handlers:
    Path("data").mkdir(exist_ok=True)
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
        handlers=[
            logging.FileHandler("data/jarvis.log", encoding="utf-8"),
            logging.StreamHandler(),
        ],
    )

logger = logging.getLogger("JARVIS")

if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

from core.config          import Config
from core.database        import Database
from core.wake_word       import WakeWordDetector
from core.stt             import SpeechToText
from core.tts             import TextToSpeech
from core.intent          import IntentEngine
from core.memory          import MemoryEngine
from core.trainer         import TrainingEngine
from core.intelligence_db import IntelligenceDB
from core.context_engine  import ShortTermContext
from core.long_term_memory import LongTermMemory
from core.learning_engine  import LearningEngine
from core.decision_engine  import DecisionEngine
from core.reflection_engine import ReflectionEngine
from automation.workflow_engine import WorkflowEngine
from automation.reminder        import ReminderScheduler
from modes.mode_manager         import ModeManager
from ui.app                     import JarvisApp


class Jarvis:
    """Core JARVIS orchestrator — with full AI intelligence layer."""

    def __init__(self):
        logger.info("Initialising JARVIS…")
        self.running = False

        # ── Core infrastructure ──────────────────────────────────────────────
        self.config   = Config()
        self.db       = Database()
        self.tts      = TextToSpeech(self.config)
        self.stt      = SpeechToText(self.config)

        # ── Intelligence layer ───────────────────────────────────────────────
        self.intel_db = IntelligenceDB()
        self.ctx      = ShortTermContext(self.intel_db)
        self.ltm      = LongTermMemory(self.intel_db)

        # ── Upgraded MemoryEngine (injects all intelligence sub-systems) ─────
        # LearningEngine needs ltm → constructed separately first
        self.learn    = LearningEngine(self.intel_db, self.ltm)
        self.memory   = MemoryEngine(
            db       = self.db,
            intel_db = self.intel_db,
            ltm      = self.ltm,
            ctx      = self.ctx,
            learn    = self.learn,
        )

        # ── Automation layer ─────────────────────────────────────────────────
        self.trainer  = TrainingEngine(self.db)
        self.workflow = WorkflowEngine(self.db, tts=self.tts, config=self.config)
        self.reminder = ReminderScheduler(self.tts)
        self.modes    = ModeManager(self.config, self.tts)
        self.intent   = IntentEngine(
            self.tts, self.memory, self.trainer,
            self.workflow, self.modes, self.reminder, self.config
        )
        self.intent.set_assistant_shutdown_callback(self.request_shutdown)

        # ── Decision Engine (wraps IntentEngine with intelligence) ───────────
        self.decision = DecisionEngine(
            intent_engine   = self.intent,
            long_term_memory= self.ltm,
            context_engine  = self.ctx,
            learning_engine = self.learn,
            tts             = self.tts,
        )

        # ── Reflection Engine ────────────────────────────────────────────────
        self.reflection = ReflectionEngine(
            intel_db        = self.intel_db,
            learning_engine = self.learn,
            long_term_memory= self.ltm,
            tts             = self.tts,
        )

        # ── Wake word detector ───────────────────────────────────────────────
        self.wake = WakeWordDetector(self.config, on_detect=self._on_wake)

        self.reminder.set_db(self.db)

        logger.info(
            f"JARVIS initialised — "
            f"Wake={self.wake._backend}, "
            f"TTS={self.tts._backend}, "
            f"STT={self.stt._backend}"
        )

    # ── Wake word callback ────────────────────────────────────────────────────

    def _on_wake(self):
        """Called from the wake-word thread when the trigger is detected."""
        logger.info("Wake word detected.")
        self.tts.speak_async("Yes?")
        audio = self.stt.listen()
        if audio:
            text = self.stt.transcribe(audio)
            if text:
                logger.info(f"Command: {text}")
                response = self.process_text(text, source="voice")
                if response:
                    logger.info(f"Response: {response}")

    def process_text(self, text: str, source: str = "manual") -> str:
        """
        Single command path for voice and typed commands.

        This keeps memory learning, alias expansion, context resolution,
        decision fallback, and history logging consistent across the app.
        """
        text = str(text or "").strip()
        if not text:
            return ""

        learn_response = self.memory.parse_and_learn(text)
        if learn_response:
            self.tts.speak_async(learn_response)
            self.memory.record_turn(
                text, intent="memory_learn", response=learn_response
            )
            self.memory.log_command(text, learn_response)
            self.memory.record_action(
                action="memory_learn",
                target=text[:80],
                mode=self.config.get("active_mode", "standard"),
            )
            return learn_response

        response = self.decision.process(text)
        response = response or ""
        self.memory.record_turn(text, response=response)
        self.memory.log_command(text, response)
        self.memory.record_action(
            action=f"{source}_command",
            target=text[:80],
            mode=self.config.get("active_mode", "standard"),
        )
        return response

    # ── Lifecycle ─────────────────────────────────────────────────────────────

    def start(self):
        self.running = True
        self.reminder.start()
        self.reflection.start()
        self.wake.start()
        logger.info("JARVIS is online.")
        self.tts.speak_async(
            self.config.get("startup_message", "JARVIS online. Ready.")
        )

    def stop(self):
        if not self.running:
            return
        self.running = False
        self.wake.stop()
        self.reminder.stop()
        self.reflection.stop()
        self.tts.stop()
        try:
            self.db.close()
        except Exception:
            pass
        try:
            self.intel_db.close()
        except Exception:
            pass
        logger.info("JARVIS shut down cleanly.")

    def request_shutdown(self):
        """Request a Qt-safe assistant shutdown from any worker thread."""
        logger.info("Assistant shutdown requested.")
        try:
            from PyQt6.QtCore import QMetaObject, Qt
            from PyQt6.QtWidgets import QApplication

            app = QApplication.instance()
            if app is not None:
                QMetaObject.invokeMethod(
                    app,
                    "quit",
                    Qt.ConnectionType.QueuedConnection,
                )
                return
        except Exception as exc:
            logger.warning(f"Qt shutdown request failed; stopping directly: {exc}")

        threading.Thread(
            target=self.stop,
            daemon=True,
            name="JarvisShutdown",
        ).start()


def main():
    for d in ["data", "models/wake_word", "models/whisper", "models/piper"]:
        Path(d).mkdir(parents=True, exist_ok=True)

    jarvis = Jarvis()
    app = JarvisApp(jarvis)
    app.run()


if __name__ == "__main__":
    main()
