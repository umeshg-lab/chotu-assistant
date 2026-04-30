"""
Wake word detection using SpeechRecognition and PyAudio only.
"""

import logging
import threading
import time
from typing import Callable, Optional

logger = logging.getLogger("JARVIS.WakeWord")

_DETECTION_COOLDOWN = 1.5


class WakeWordDetector:
    """Listens continuously for the configured wake word."""

    def __init__(self, config, on_detect: Callable):
        self.config = config
        self.on_detect = on_detect
        self._thread: Optional[threading.Thread] = None
        self._stop_event = threading.Event()
        self._active = True
        self._last_detection = 0.0
        self._backend = self._detect_backend()

    def _detect_backend(self) -> str:
        return "sr_fallback"

    def start(self):
        if self._thread and self._thread.is_alive():
            logger.info("Wake word detector already running.")
            return

        self._stop_event.clear()
        self._thread = threading.Thread(
            target=self._run_loop,
            daemon=True,
            name="JarvisWakeWord",
        )
        self._thread.start()
        logger.info("Wake word detector started (sr_fallback).")

    def stop(self):
        self._stop_event.set()
        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=5)
        logger.info("Wake word detector stopped.")

    def set_active(self, active: bool):
        self._active = bool(active)
        logger.info(f"Wake word {'activated' if self._active else 'paused'}.")

    def _run_loop(self):
        try:
            self._run_sr_fallback()
        except Exception as exc:
            logger.error(
                f"Wake word thread crashed unexpectedly: {exc}. "
                "Voice activation disabled for this session.",
                exc_info=True,
            )

    def _fire_detection(self, label: str = ""):
        now = time.monotonic()
        if now - self._last_detection < _DETECTION_COOLDOWN:
            return

        self._last_detection = now
        logger.info(f"Wake word detected{f' ({label})' if label else ''}.")
        if self.on_detect:
            threading.Thread(
                target=self.on_detect,
                daemon=True,
                name="JarvisOnWake",
            ).start()

    def _run_sr_fallback(self):
        try:
            import difflib
            import re

            import pyaudio
            import speech_recognition as sr
        except ImportError as exc:
            logger.error(f"SR fallback import failed: {exc}")
            return

        def as_int(key, default, minimum=None):
            try:
                value = int(self.config.get(key, default))
            except (TypeError, ValueError):
                value = default
            if minimum is not None:
                value = max(minimum, value)
            return value

        def as_float(key, default, minimum=None):
            try:
                value = float(self.config.get(key, default))
            except (TypeError, ValueError):
                value = default
            if minimum is not None:
                value = max(minimum, value)
            return value

        sample_rate = as_int("wake_sample_rate", 16_000, 8_000)
        chunk_size = as_int("wake_chunk_size", 1_024, 256)
        listen_timeout = as_float("wake_listen_timeout", 1.0, 0.2)
        phrase_limit = as_float("wake_phrase_limit", 2.4, 0.8)
        reopen_delay = as_float("wake_reopen_delay", 0.75, 0.1)
        command_release = as_float(
            "wake_release_seconds",
            min(
                30.0,
                as_float("stt_timeout", 8.0, 1.0)
                + as_float("stt_phrase_limit", 15.0, 1.0)
                + 2.0,
            ),
            1.0,
        )

        wake_word = str(self.config.get("wake_word", "jarvis") or "jarvis")
        wake_word = re.sub(r"\s+", " ", wake_word.lower()).strip() or "jarvis"

        recognizer = sr.Recognizer()
        recognizer.operation_timeout = as_float("wake_recognition_timeout", 3.0, 1.0)
        recognizer.energy_threshold = as_int("wake_energy_threshold", 260, 50)
        recognizer.dynamic_energy_threshold = True
        recognizer.dynamic_energy_adjustment_damping = 0.15
        recognizer.dynamic_energy_ratio = 1.35
        recognizer.pause_threshold = 0.45
        recognizer.phrase_threshold = 0.1
        recognizer.non_speaking_duration = 0.25

        def normalize(text):
            text = re.sub(r"[^a-z0-9 ]+", " ", str(text).lower())
            return re.sub(r"\s+", " ", text).strip()

        aliases = {
            wake_word,
            f"hey {wake_word}",
            f"ok {wake_word}",
            f"okay {wake_word}",
        }
        if wake_word == "jarvis":
            aliases.update(
                {
                    "jarvis",
                    "jervis",
                    "javis",
                    "travis",
                    "charvis",
                    "service",
                    "hey jarvis",
                    "ok jarvis",
                    "okay jarvis",
                }
            )
        aliases = {normalize(alias) for alias in aliases if normalize(alias)}

        def contains_wake_word(text):
            normalized = normalize(text)
            if not normalized:
                return False

            padded = f" {normalized} "
            if any(f" {alias} " in padded for alias in aliases):
                return True

            words = normalized.split()
            target_words = wake_word.split()
            window_sizes = {1, max(1, len(target_words))}
            candidates = []
            for size in window_sizes:
                for index in range(0, max(0, len(words) - size + 1)):
                    candidates.append(" ".join(words[index:index + size]))

            return any(
                difflib.SequenceMatcher(None, candidate, wake_word).ratio() >= 0.78
                for candidate in candidates
            )

        class PyAudioStreamAdapter:
            def __init__(self, wrapped_stream, stop_event):
                self._wrapped_stream = wrapped_stream
                self._stop_event = stop_event

            def read(self, size):
                if self._wrapped_stream is None or self._stop_event.is_set():
                    return b""
                return self._wrapped_stream.read(
                    size,
                    exception_on_overflow=False,
                )

        class PyAudioSource(sr.AudioSource):
            def __init__(self, wrapped_stream, sample_width, stop_event):
                self.SAMPLE_RATE = sample_rate
                self.SAMPLE_WIDTH = sample_width
                self.CHUNK = chunk_size
                self.stream = PyAudioStreamAdapter(wrapped_stream, stop_event)

            def __enter__(self):
                return self

            def __exit__(self, exc_type, exc_value, traceback):
                return False

        pa = None
        stream = None
        source = None
        calibrated = False

        def close_audio():
            nonlocal pa, stream, source
            source = None

            current_stream = stream
            stream = None
            if current_stream is not None:
                try:
                    if current_stream.is_active():
                        current_stream.stop_stream()
                except Exception:
                    pass
                try:
                    current_stream.close()
                except Exception:
                    pass

            current_pa = pa
            pa = None
            if current_pa is not None:
                try:
                    current_pa.terminate()
                except Exception:
                    pass

        def open_audio():
            nonlocal pa, stream, source, calibrated
            close_audio()

            try:
                pa = pyaudio.PyAudio()
                stream = pa.open(
                    format=pyaudio.paInt16,
                    channels=1,
                    rate=sample_rate,
                    input=True,
                    frames_per_buffer=chunk_size,
                )
                if stream is None:
                    raise RuntimeError("PyAudio returned no input stream")

                source = PyAudioSource(
                    stream,
                    pa.get_sample_size(pyaudio.paInt16),
                    self._stop_event,
                )

                if not calibrated:
                    try:
                        recognizer.adjust_for_ambient_noise(source, duration=0.45)
                        calibrated = True
                        logger.info(
                            "SR fallback energy threshold: "
                            f"{recognizer.energy_threshold:.0f}"
                        )
                    except Exception as exc:
                        logger.warning(f"SR fallback calibration skipped: {exc}")

                logger.info(f"SR fallback listening for wake word: '{wake_word}'")
                return True

            except Exception as exc:
                logger.error(f"SR fallback microphone open failed: {exc}")
                close_audio()
                return False

        def recognize(audio):
            keyword_entries = [(alias, 1.0) for alias in sorted(aliases, key=len)]

            try:
                text = recognizer.recognize_sphinx(
                    audio,
                    keyword_entries=keyword_entries,
                )
                text = normalize(text)
                if text:
                    logger.debug(f"SR fallback heard via Sphinx: {text}")
                    return text
            except sr.UnknownValueError:
                pass
            except Exception as exc:
                logger.debug(f"SR fallback Sphinx unavailable: {exc}")

            try:
                text = recognizer.recognize_google(audio, language="en-US")
                text = normalize(text)
                if text:
                    logger.debug(f"SR fallback heard via Google: {text}")
                    return text
            except sr.UnknownValueError:
                return None
            except Exception as exc:
                logger.debug(f"SR fallback Google unavailable: {exc}")

            return None

        try:
            while not self._stop_event.is_set():
                if not self._active:
                    close_audio()
                    self._stop_event.wait(0.2)
                    continue

                if source is None and not open_audio():
                    self._stop_event.wait(reopen_delay)
                    continue

                try:
                    audio = recognizer.listen(
                        source,
                        timeout=listen_timeout,
                        phrase_time_limit=phrase_limit,
                    )
                except sr.WaitTimeoutError:
                    continue
                except (AssertionError, AttributeError, OSError) as exc:
                    logger.warning(f"SR fallback stream error; reopening mic: {exc}")
                    close_audio()
                    self._stop_event.wait(reopen_delay)
                    continue
                except Exception as exc:
                    logger.warning(f"SR fallback listen failed; reopening mic: {exc}")
                    close_audio()
                    self._stop_event.wait(reopen_delay)
                    continue

                text = recognize(audio)
                if not text or not contains_wake_word(text):
                    continue

                if time.monotonic() - self._last_detection < _DETECTION_COOLDOWN:
                    continue

                close_audio()
                self._fire_detection(f"text='{text}'")

                release_until = time.monotonic() + command_release
                while (
                    not self._stop_event.is_set()
                    and time.monotonic() < release_until
                ):
                    self._stop_event.wait(0.1)

        except Exception as exc:
            logger.error(f"SR fallback fatal error: {exc}", exc_info=True)
        finally:
            close_audio()
            logger.info("SR fallback exited cleanly.")
