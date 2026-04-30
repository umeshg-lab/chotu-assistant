"""
Configuration manager — reads/writes data/config.json

FIX LOG (core/config.py):
  - Added thread-safe write via temp-file + atomic rename (prevents config
    corruption if JARVIS is killed during a write)
  - Added _lock so concurrent threads calling set() don't race on the file
  - CONFIG_PATH resolved relative to project root, not the calling CWD
  - Deep-merge for nested dicts is now recursive (was only one level deep)
  - get() now supports dot-notation keys for nested access: config.get("app_paths.chrome")
  - set() supports dot-notation for nested update
  - Added validate() that logs warnings for missing/invalid critical keys
"""

import json
import os
import threading
import tempfile
import logging
from pathlib import Path
from typing import Any, Optional

logger = logging.getLogger("JARVIS.Config")

# Project root = two levels up from this file (core/config.py → jarvis/)
_ROOT = Path(__file__).resolve().parent.parent

DEFAULTS: dict = {
    "wake_word":        "jarvis",
    "whisper_model":    "models/whisper/ggml-small.en.bin",
    "piper_model":      "models/piper/en_US-lessac-medium.onnx",
    "piper_config":     "models/piper/en_US-lessac-medium.onnx.json",
    "tts_rate":         175,
    "tts_volume":       1.0,
    "stt_timeout":      8,
    "stt_phrase_limit": 15,
    "startup_message":  "JARVIS online. How can I help?",
    "start_with_windows": False,
    "minimize_to_tray": True,
    "theme":            "dark",
    "active_mode":      "standard",
    "app_paths": {
        "chrome":    "C:\\Program Files\\Google\\Chrome\\Application\\chrome.exe",
        "firefox":   "C:\\Program Files\\Mozilla Firefox\\firefox.exe",
        "vscode":    "C:\\Users\\%USERNAME%\\AppData\\Local\\Programs\\Microsoft VS Code\\Code.exe",
        "photoshop": "C:\\Program Files\\Adobe\\Adobe Photoshop *\\Photoshop.exe",
        "premiere":  "C:\\Program Files\\Adobe\\Adobe Premiere Pro *\\Adobe Premiere Pro.exe",
        "steam":     "C:\\Program Files (x86)\\Steam\\steam.exe",
        "epic":      "C:\\Program Files (x86)\\Epic Games\\Launcher\\Portal\\Binaries\\Win32\\EpicGamesLauncher.exe",
        "spotify":   "C:\\Users\\%USERNAME%\\AppData\\Roaming\\Spotify\\Spotify.exe",
        "notepad":   "notepad.exe",
        "explorer":  "explorer.exe",
        "taskmgr":   "taskmgr.exe",
        "vlc":       "C:\\Program Files\\VideoLAN\\VLC\\vlc.exe",
        "discord":   "C:\\Users\\%USERNAME%\\AppData\\Local\\Discord\\app-*\\Discord.exe",
        "obs":       "C:\\Program Files\\obs-studio\\bin\\64bit\\obs64.exe",
        "notion":    "C:\\Users\\%USERNAME%\\AppData\\Local\\Programs\\Notion\\Notion.exe",
    },
    "browser_shortcuts": {
        "envato":    "https://elements.envato.com",
        "freepik":   "https://www.freepik.com",
        "pinterest": "https://www.pinterest.com",
        "youtube":   "https://www.youtube.com",
        "gmail":     "https://mail.google.com",
        "github":    "https://github.com",
        "notion":    "https://www.notion.so",
        "figma":     "https://www.figma.com",
    },
    "music_playlists": {
        "design":  "https://open.spotify.com/playlist/37i9dQZF1DX8Uebhn9wzrS",
        "focus":   "https://open.spotify.com/playlist/37i9dQZF1DWZeKCadgRdKQ",
        "gaming":  "https://open.spotify.com/playlist/37i9dQZF1DWTyiBJ6yEqeu",
        "lofi":    "https://www.youtube.com/watch?v=jfKfPfyJRdk",
        "editing": "https://open.spotify.com/playlist/37i9dQZF1DX4sWSpwq3LiO",
    },
}


def _deep_merge(base: dict, override: dict) -> dict:
    """Recursively merge override into base, returning a new dict."""
    result = base.copy()
    for key, value in override.items():
        if key in result and isinstance(result[key], dict) and isinstance(value, dict):
            result[key] = _deep_merge(result[key], value)
        else:
            result[key] = value
    return result


class Config:
    # Resolved absolute path so it works no matter the CWD
    CONFIG_PATH: Path = _ROOT / "data" / "config.json"

    def __init__(self):
        self._data: dict = {}
        self._lock = threading.Lock()
        self.load()
        self.validate()

    # ── Persistence ──────────────────────────────────────────────────────────

    def load(self):
        with self._lock:
            if self.CONFIG_PATH.exists():
                try:
                    with open(self.CONFIG_PATH, "r", encoding="utf-8") as f:
                        saved = json.load(f)
                    self._data = _deep_merge(DEFAULTS, saved)
                except (json.JSONDecodeError, OSError) as e:
                    logger.warning(f"Config load error ({e}); using defaults.")
                    self._data = DEFAULTS.copy()
            else:
                self._data = _deep_merge({}, DEFAULTS)
                self._write_locked()

    def save(self):
        with self._lock:
            self._write_locked()

    def _write_locked(self):
        """Atomic write: write to temp then rename — never corrupts on crash."""
        self.CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
        try:
            fd, tmp_path = tempfile.mkstemp(
                dir=self.CONFIG_PATH.parent, suffix=".tmp"
            )
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump(self._data, f, indent=2)
            Path(tmp_path).replace(self.CONFIG_PATH)
        except OSError as e:
            logger.error(f"Config save failed: {e}")

    # ── Accessors ────────────────────────────────────────────────────────────

    def get(self, key: str, default: Any = None) -> Any:
        """
        Supports simple key or dot-notation for nested access.
        e.g. config.get("app_paths.chrome")
        """
        with self._lock:
            if "." in key:
                parts = key.split(".", 1)
                sub = self._data.get(parts[0])
                if isinstance(sub, dict):
                    return sub.get(parts[1], default)
                return default
            return self._data.get(key, default)

    def set(self, key: str, value: Any):
        """
        Supports dot-notation for nested set.
        e.g. config.set("app_paths.chrome", "C:/...")
        """
        with self._lock:
            if "." in key:
                parts = key.split(".", 1)
                if parts[0] not in self._data or not isinstance(self._data[parts[0]], dict):
                    self._data[parts[0]] = {}
                self._data[parts[0]][parts[1]] = value
            else:
                self._data[key] = value
            self._write_locked()

    def get_app_path(self, name: str) -> str:
        paths = self.get("app_paths") or {}
        path = paths.get(name.lower(), "")
        return os.path.expandvars(path)

    def get_url(self, name: str) -> str:
        shortcuts = self.get("browser_shortcuts") or {}
        return shortcuts.get(name.lower(), "")

    def get_playlist(self, name: str) -> str:
        playlists = self.get("music_playlists") or {}
        return playlists.get(name.lower(), "")

    def all(self) -> dict:
        with self._lock:
            return self._data.copy()

    # ── Validation ───────────────────────────────────────────────────────────

    def validate(self):
        """Log warnings for missing or obviously wrong critical values."""
        issues = []
        if not self.get("wake_word"):
            issues.append("wake_word is empty")
        tts_rate = self.get("tts_rate", 175)
        if not isinstance(tts_rate, (int, float)) or not (50 <= tts_rate <= 400):
            issues.append(f"tts_rate out of range: {tts_rate}")
        for issue in issues:
            logger.warning(f"Config warning: {issue}")
