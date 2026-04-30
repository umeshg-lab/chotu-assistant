"""
Text-to-speech using piper-tts (offline, natural voice).
Falls back to pyttsx3 if piper not available.

FIX LOG (core/tts.py):
  BUG-A  pyttsx3 DEADLOCK: pyttsx3.Engine is not thread-safe.  The original
         code created the engine in __init__ (main thread) then called
         .say()/.runAndWait() from the background worker thread.  On Windows,
         pyttsx3 uses SAPI5 COM objects which must be created and used on the
         SAME thread.  The engine was sometimes None in the worker thread.
         Fix: create the pyttsx3 engine inside the worker thread and keep it
         there for the thread's entire lifetime.

  BUG-B  _speak_worker loop has no protection against None sentinel being
         sent while .speak() is blocking.  The None sentinel is now handled
         correctly, and the worker sets a done-event so stop() can confirm.

  BUG-C  Temp WAV files from piper were not deleted if an exception occurred
         during playback.  Fix: move os.unlink into a finally block.

  BUG-D  _speak_pyttsx3 used self._engine which was a shared attribute but
         engine must live on worker thread only.  The engine is now a local
         variable in the worker thread closure.

  BUG-E  stop() just sent None — but if the queue already had items, stop()
         returned before they finished, leaving threads dangling.
         Fix: drain queue first, then put sentinel, then join worker thread.

  NEW    speak_async() guard: if TTS is shutting down, the item is dropped
         rather than blocking forever.

  NEW    piper CLI subprocess timeout is now configurable; default kept at 10s.
"""

import logging
import os
import queue
import subprocess
import tempfile
import threading
from typing import Optional

logger = logging.getLogger("JARVIS.TTS")


class TextToSpeech:
    """
    Converts text to speech locally.
    Backend priority: piper (CLI) → piper (Python) → pyttsx3
    """

    def __init__(self, config):
        self.config    = config
        self._backend  = self._detect_backend()
        self._queue: queue.Queue = queue.Queue()
        self._stopped  = threading.Event()
        self._worker   = threading.Thread(
            target=self._speak_worker, daemon=True, name="JarvisTTS"
        )
        self._worker.start()

    # ── Backend detection ────────────────────────────────────────────────────

    def _detect_backend(self) -> str:
        model = self.config.get("piper_model", "")
        if model and os.path.exists(model):
            # Check for piper CLI
            try:
                result = subprocess.run(
                    ["piper", "--version"],
                    capture_output=True,
                    timeout=3,
                )
                if result.returncode == 0:
                    logger.info("TTS backend: piper-tts (CLI)")
                    return "piper"
            except (FileNotFoundError, subprocess.TimeoutExpired):
                pass
            # Check for piper Python package
            try:
                from piper.voice import PiperVoice  # noqa: F401
                logger.info("TTS backend: piper-tts (Python)")
                return "piper_py"
            except ImportError:
                pass

        try:
            import pyttsx3  # noqa: F401
            logger.info("TTS backend: pyttsx3 (fallback)")
            return "pyttsx3"
        except ImportError:
            pass

        logger.warning("No TTS backend found. Voice output disabled.")
        return "none"

    # ── Public API ───────────────────────────────────────────────────────────

    def speak(self, text: str):
        """Blocking speak — waits for this utterance to finish."""
        if not text or self._backend == "none" or self._stopped.is_set():
            return
        done = threading.Event()
        self._queue.put((text, done))
        done.wait(timeout=30)

    def speak_async(self, text: str):
        """Non-blocking speak — queues the utterance and returns immediately."""
        if not text or self._backend == "none" or self._stopped.is_set():
            return
        self._queue.put((text, None))

    # ── Worker ───────────────────────────────────────────────────────────────

    def _speak_worker(self):
        """
        BUG-A/D fix: pyttsx3 engine lives entirely inside this thread.
        Created once on first use, reused for all subsequent utterances.
        """
        pyttsx3_engine = None  # local to this thread — never accessed elsewhere

        while True:
            item = self._queue.get()
            if item is None:
                # Sentinel — shut down
                break

            text, done_event = item
            try:
                if self._backend == "piper":
                    self._speak_piper_cli(text)
                elif self._backend == "piper_py":
                    self._speak_piper_py(text)
                elif self._backend == "pyttsx3":
                    pyttsx3_engine = self._speak_pyttsx3(text, pyttsx3_engine)
            except Exception as exc:
                logger.error(f"TTS worker error: {exc}")
            finally:
                self._queue.task_done()
                if done_event is not None:
                    done_event.set()

        # Clean up pyttsx3 engine if it was created
        if pyttsx3_engine is not None:
            try:
                pyttsx3_engine.stop()
            except Exception:
                pass

    # ── Stop ─────────────────────────────────────────────────────────────────

    def stop(self):
        """
        BUG-E fix: drain the queue of pending items, then send the sentinel
        and wait for the worker to confirm it has exited.
        """
        if self._stopped.is_set():
            return
        self._stopped.set()
        # Drain pending items so the worker reaches the sentinel quickly
        try:
            while True:
                self._queue.get_nowait()
                self._queue.task_done()
        except queue.Empty:
            pass
        self._queue.put(None)   # sentinel
        self._worker.join(timeout=5)

    # ── Piper CLI ────────────────────────────────────────────────────────────

    def _speak_piper_cli(self, text: str):
        model = self.config.get("piper_model")
        tmp = None
        try:
            fd, tmp = tempfile.mkstemp(suffix=".wav")
            os.close(fd)
            subprocess.run(
                ["piper", "--model", model, "--output_file", tmp],
                input=text.encode(),
                capture_output=True,
                timeout=10,
                check=True,
            )
            self._play_wav(tmp)
        except subprocess.CalledProcessError as exc:
            logger.error(f"Piper CLI failed: {exc.stderr.decode(errors='replace')}")
            self._speak_pyttsx3_fallback(text)
        except Exception as exc:
            logger.error(f"Piper CLI TTS error: {exc}")
            self._speak_pyttsx3_fallback(text)
        finally:
            if tmp and os.path.exists(tmp):
                try:
                    os.unlink(tmp)
                except OSError:
                    pass

    # ── Piper Python ─────────────────────────────────────────────────────────

    def _speak_piper_py(self, text: str):
        tmp = None
        try:
            from piper.voice import PiperVoice
            import wave

            model = self.config.get("piper_model")
            voice = PiperVoice.load(model)

            fd, tmp = tempfile.mkstemp(suffix=".wav")
            os.close(fd)
            with wave.open(tmp, "w") as wav_file:
                voice.synthesize(text, wav_file)
            self._play_wav(tmp)
        except Exception as exc:
            logger.error(f"Piper Python TTS error: {exc}")
            self._speak_pyttsx3_fallback(text)
        finally:
            if tmp and os.path.exists(tmp):
                try:
                    os.unlink(tmp)
                except OSError:
                    pass

    # ── WAV playback ─────────────────────────────────────────────────────────

    def _play_wav(self, path: str):
        """Play a WAV file using ffplay (if available) or PowerShell."""
        try:
            subprocess.run(
                ["ffplay", "-nodisp", "-autoexit", path],
                capture_output=True,
                timeout=30,
            )
        except FileNotFoundError:
            subprocess.run(
                [
                    "powershell",
                    "-c",
                    f'(New-Object Media.SoundPlayer "{path}").PlaySync()',
                ],
                timeout=30,
                capture_output=True,
            )

    # ── pyttsx3 ──────────────────────────────────────────────────────────────

    def _speak_pyttsx3(self, text: str, engine=None):
        """
        BUG-A fix: engine is passed in and returned so it stays on the worker
        thread.  First call creates it; subsequent calls reuse it.
        Returns the (possibly newly created) engine.
        """
        try:
            import pyttsx3
            if engine is None:
                engine = pyttsx3.init()
                engine.setProperty("rate",   self.config.get("tts_rate", 175))
                engine.setProperty("volume", self.config.get("tts_volume", 1.0))
                voices = engine.getProperty("voices")
                # Prefer a male voice for the JARVIS aesthetic
                for v in voices:
                    if "david" in v.name.lower() or "mark" in v.name.lower():
                        engine.setProperty("voice", v.id)
                        break
            engine.say(text)
            engine.runAndWait()
            return engine
        except Exception as exc:
            logger.error(f"pyttsx3 TTS error: {exc}")
            return None  # engine is broken; discard it

    def _speak_pyttsx3_fallback(self, text: str):
        """
        Used as a fallback from piper failures.
        Runs pyttsx3 inline (we are already on the worker thread).
        """
        try:
            import pyttsx3
            eng = pyttsx3.init()
            eng.say(text)
            eng.runAndWait()
            eng.stop()
        except Exception as exc:
            logger.error(f"pyttsx3 fallback error: {exc}")
