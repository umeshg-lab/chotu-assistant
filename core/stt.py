"""
Speech-to-text using whisper.cpp (offline, CPU/GPU).
Falls back to speech_recognition if whisper not available.

FIX LOG (core/stt.py):
  BUG-A  listen() reused a single sr.Microphone across multiple calls.
         Same root cause as wake_word.py BUG-3: second pa.open() can return
         None, causing "NoneType has no attribute stop_stream".
         Fix: create a fresh sr.Microphone each call (simple, correct).
         Previously the same instance was constructed once at __init__ and
         reused, which is the actual problem.

  BUG-B  _transcribe_whispercpp re-instantiates Whisper(model_path) on every
         single call — extremely slow and leaks model memory.
         Fix: cache the Whisper instance after first load.

  BUG-C  _transcribe_whisper also re-loads the model on every call.
         Fix: cache the openai-whisper model after first load.

  BUG-D  _transcribe_sr constructed AudioData with width=2 (hard-coded).
         The actual recorded wav data already has the correct sample width
         embedded.  Use sr.AudioData.from_frame_data() or just parse the WAV
         header.  Simplest correct fix: pass audio_bytes directly via BytesIO.

  BUG-E  listen() calls recognizer.adjust_for_ambient_noise every time —
         adds ~300 ms latency on each command.  Changed to calibrate once at
         class init (if a microphone is available) and reuse the threshold.

  NEW    Public listen_and_transcribe() combines listen+transcribe in one call
         for callers (like main.py _on_wake) that always do both.
"""

import logging
import io
import os
import tempfile
import threading
from typing import Optional

logger = logging.getLogger("JARVIS.STT")


class SpeechToText:
    """
    Records audio and transcribes to text.
    Backend priority: whispercpp → openai-whisper → speech_recognition
    """

    def __init__(self, config):
        self.config = config
        self.timeout      = config.get("stt_timeout", 8)
        self.phrase_limit = config.get("stt_phrase_limit", 15)
        self._backend = self._detect_backend()
        # Cached model instances (avoid re-loading on every transcription)
        self._whisper_cpp_model = None
        self._whisper_model     = None
        self._model_lock = threading.Lock()
        # Pre-calibrated energy threshold for listen()
        self._energy_threshold: Optional[float] = None
        self._calibrate_once()

    # ── Backend detection ────────────────────────────────────────────────────

    def _detect_backend(self) -> str:
        try:
            from whispercpp import Whisper  # noqa: F401
            logger.info("STT backend: whispercpp")
            return "whispercpp"
        except ImportError:
            pass
        try:
            import whisper  # noqa: F401
            logger.info("STT backend: openai-whisper")
            return "whisper"
        except ImportError:
            pass
        try:
            import speech_recognition  # noqa: F401
            logger.info("STT backend: speech_recognition (fallback)")
            return "sr"
        except ImportError:
            pass
        logger.error("No STT backend available!")
        return "none"

    # ── One-time calibration ─────────────────────────────────────────────────

    def _calibrate_once(self):
        """
        BUG-E fix: calibrate the ambient noise threshold once at startup
        instead of on every listen() call.
        """
        if self._backend == "none":
            return
        try:
            import speech_recognition as sr
            recognizer = sr.Recognizer()
            with sr.Microphone(sample_rate=16_000) as source:
                recognizer.adjust_for_ambient_noise(source, duration=0.5)
            self._energy_threshold = recognizer.energy_threshold
            logger.info(
                f"STT calibrated — energy threshold: {self._energy_threshold:.0f}"
            )
        except Exception as exc:
            logger.warning(f"STT calibration skipped: {exc}")
            self._energy_threshold = 300.0  # safe default

    # ── Public API ───────────────────────────────────────────────────────────

    def listen(self) -> Optional[bytes]:
        """Record audio from the default microphone; return raw WAV bytes."""
        if self._backend == "none":
            return None
        try:
            import speech_recognition as sr
            recognizer = sr.Recognizer()
            if self._energy_threshold is not None:
                recognizer.energy_threshold        = self._energy_threshold
                recognizer.dynamic_energy_threshold = True
            else:
                recognizer.energy_threshold        = 300
                recognizer.dynamic_energy_threshold = True

            # BUG-A fix: create a NEW Microphone each call — never reuse.
            # sr.Microphone's __enter__/__exit__ open/close PyAudio internally;
            # reusing the same object causes pa.open() to return None on the
            # second invocation, crashing __exit__ with NoneType.close().
            with sr.Microphone(sample_rate=16_000) as source:
                logger.info("Listening for command…")
                audio = recognizer.listen(
                    source,
                    timeout=self.timeout,
                    phrase_time_limit=self.phrase_limit,
                )
            return audio.get_wav_data()

        except Exception as exc:
            logger.error(f"listen() error: {exc}")
            return None

    def transcribe(self, audio_bytes: bytes) -> Optional[str]:
        """Convert WAV bytes to a text string."""
        if not audio_bytes:
            return None
        if self._backend == "whispercpp":
            return self._transcribe_whispercpp(audio_bytes)
        if self._backend == "whisper":
            return self._transcribe_whisper(audio_bytes)
        if self._backend == "sr":
            return self._transcribe_sr(audio_bytes)
        return None

    def listen_and_transcribe(self) -> Optional[str]:
        """Convenience wrapper: listen then transcribe in one call."""
        audio = self.listen()
        if audio:
            return self.transcribe(audio)
        return None

    # ── Backend implementations ──────────────────────────────────────────────

    def _transcribe_whispercpp(self, audio_bytes: bytes) -> Optional[str]:
        """BUG-B fix: cache the Whisper instance after first load."""
        try:
            from whispercpp import Whisper
            with self._model_lock:
                if self._whisper_cpp_model is None:
                    model_path = self.config.get(
                        "whisper_model", "models/whisper/ggml-small.en.bin"
                    )
                    logger.info(f"Loading whispercpp model: {model_path}")
                    self._whisper_cpp_model = Whisper(model_path)

            with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as f:
                f.write(audio_bytes)
                tmp = f.name
            try:
                text = self._whisper_cpp_model.transcribe(tmp).strip()
                logger.info(f"Transcribed (whispercpp): '{text}'")
                return text or None
            finally:
                try:
                    os.unlink(tmp)
                except OSError:
                    pass

        except Exception as exc:
            logger.error(f"whispercpp transcription error: {exc}")
            # Cascade to next backend
            return self._transcribe_whisper(audio_bytes)

    def _transcribe_whisper(self, audio_bytes: bytes) -> Optional[str]:
        """BUG-C fix: cache the openai-whisper model after first load."""
        try:
            import whisper
            with self._model_lock:
                if self._whisper_model is None:
                    logger.info("Loading openai-whisper model: small.en")
                    self._whisper_model = whisper.load_model("small.en")

            with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as f:
                f.write(audio_bytes)
                tmp = f.name
            try:
                result = self._whisper_model.transcribe(tmp, language="en", fp16=False)
                text = result["text"].strip()
                logger.info(f"Transcribed (whisper): '{text}'")
                return text or None
            finally:
                try:
                    os.unlink(tmp)
                except OSError:
                    pass

        except Exception as exc:
            logger.error(f"openai-whisper transcription error: {exc}")
            return None

    def _transcribe_sr(self, audio_bytes: bytes) -> Optional[str]:
        """
        BUG-D fix: construct AudioData from the raw WAV bytes properly.
        The WAV header encodes sample rate and width; we parse it rather
        than hard-coding width=2.
        """
        try:
            import speech_recognition as sr
            import wave

            # Parse WAV header for correct metadata
            with io.BytesIO(audio_bytes) as wav_io:
                with wave.open(wav_io, "rb") as wf:
                    sample_rate  = wf.getframerate()
                    sample_width = wf.getsampwidth()

            recognizer = sr.Recognizer()
            audio_data = sr.AudioData(audio_bytes, sample_rate, sample_width)

            # Try offline Sphinx first
            try:
                text = recognizer.recognize_sphinx(audio_data).strip()
                logger.info(f"Transcribed (SR/Sphinx): '{text}'")
                return text or None
            except sr.UnknownValueError:
                return None   # heard but unintelligible
            except Exception:
                pass

            # Online fallback
            try:
                text = recognizer.recognize_google(audio_data).strip()
                logger.info(f"Transcribed (SR/Google): '{text}'")
                return text or None
            except sr.UnknownValueError:
                return None
            except Exception as exc:
                logger.error(f"SR recognition error: {exc}")
                return None

        except Exception as exc:
            logger.error(f"SR transcription error: {exc}")
            return None
