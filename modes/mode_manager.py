"""
Mode manager — activates, deactivates, and tracks modes.
Loads built-in modes + custom modes from data/modes.json.
"""

import json
import logging
from pathlib import Path
from typing import Optional

logger = logging.getLogger('JARVIS.Modes')

MODES_PATH = Path("data/modes.json")


class ModeManager:
    def __init__(self, config, tts):
        self.config = config
        self.tts = tts
        self.current_mode = config.get('active_mode', 'standard')
        self._custom_modes = self._load_custom()
        self._built_in = self._build_builtin()

    def _load_custom(self) -> dict:
        if MODES_PATH.exists():
            with open(MODES_PATH, 'r') as f:
                return json.load(f)
        return {}

    def _save_custom(self):
        MODES_PATH.parent.mkdir(exist_ok=True)
        with open(MODES_PATH, 'w') as f:
            json.dump(self._custom_modes, f, indent=2)

    def _build_builtin(self) -> dict:
        from automation import app_control, media_control, system_control, browser_control
        self._apps = app_control
        self._media = media_control
        self._sys = system_control
        self._browser = browser_control
        return {
            'standard': self._mode_standard,
            'meeting':  self._mode_meeting,
            'design':   self._mode_design,
            'edit':     self._mode_edit,
            'editing':  self._mode_edit,
            'game':     self._mode_game,
            'gaming':   self._mode_game,
            'code':     self._mode_code,
            'coding':   self._mode_code,
            'study':    self._mode_study,
            'studying': self._mode_study,
            'night':    self._mode_night,
            'sleep':    self._mode_night,
            'stream':   self._mode_stream,
            'streaming':self._mode_stream,
        }

    def activate(self, mode_name: str) -> Optional[str]:
        name = mode_name.lower().strip()
        # Remove trailing "mode" keyword if present
        name = name.replace(' mode', '').strip()

        if name in self._built_in:
            try:
                self._built_in[name]()
                self.current_mode = name
                self.config.set('active_mode', name)
                logger.info(f"Mode activated: {name}")
                return f"Mode: {name}"
            except Exception as e:
                logger.error(f"Mode error ({name}): {e}")
                self.tts.speak_async(f"Error activating {name} mode.")
                return None

        if name in self._custom_modes:
            return self._run_custom_mode(name)

        self.tts.speak_async(f"I don't know a {name} mode. You can create one in settings.")
        return None

    def _run_custom_mode(self, name: str) -> str:
        from automation.workflow_engine import WorkflowEngine
        mode = self._custom_modes[name]
        steps = mode.get('steps', [])
        logger.info(f"Running custom mode: {name} ({len(steps)} steps)")
        self.tts.speak_async(f"Activating {name} mode.")
        from core.database import Database
        db = Database()
        wf = WorkflowEngine(db)
        wf.run_steps(steps)
        self.current_mode = name
        self.config.set('active_mode', name)
        return f"Custom mode: {name}"

    def save_custom_mode(self, name: str, steps: list, description: str = ''):
        self._custom_modes[name.lower()] = {
            'name': name,
            'description': description,
            'steps': steps
        }
        self._save_custom()
        logger.info(f"Custom mode saved: {name}")

    def delete_custom_mode(self, name: str):
        self._custom_modes.pop(name.lower(), None)
        self._save_custom()

    def list_modes(self) -> list:
        built = list(self._built_in.keys())
        custom = list(self._custom_modes.keys())
        return sorted(set(built + custom))

    def get_current(self) -> str:
        return self.current_mode

    # ── Built-in Modes ───────────────────────────────────────────────────

    def _mode_standard(self):
        self.tts.speak_async("Standard mode. Everything normal.")

    def _mode_meeting(self):
        self.tts.speak_async("Activating meeting mode.")
        self._media.pause_all()
        # Close distracting apps
        for app in ['spotify', 'steam', 'epic', 'discord', 'vlc']:
            self._apps.close_app(app)
        self._sys.set_dnd(True)
        self.tts.speak_async("Meeting mode active. Media paused, distractions closed.")

    def _mode_design(self):
        self.tts.speak_async("Activating design mode.")
        self._apps.open_app('photoshop', self.config)
        self._apps.open_app('chrome', self.config)
        for site in ['envato', 'freepik', 'pinterest']:
            url = self.config.get_url(site)
            if url:
                self._browser.open_url(url)
        playlist = self.config.get_playlist('design')
        if playlist:
            self._browser.open_url(playlist)
        self.tts.speak_async("Design mode active. Photoshop, browser, and music ready.")

    def _mode_edit(self):
        self.tts.speak_async("Activating edit mode.")
        self._apps.open_app('premiere', self.config)
        self._apps.open_app('chrome', self.config)
        for site in ['envato', 'freepik', 'pinterest']:
            url = self.config.get_url(site)
            if url:
                self._browser.open_url(url)
        playlist = self.config.get_playlist('editing')
        if playlist:
            self._browser.open_url(playlist)
        self.tts.speak_async("Edit mode active. Premiere Pro and resources ready.")

    def _mode_game(self):
        self.tts.speak_async("Activating game mode.")
        # Close work apps
        for app in ['chrome', 'vscode', 'photoshop', 'premiere']:
            self._apps.close_app(app)
        self._sys.boost_performance()
        self._apps.open_app('steam', self.config)
        self.tts.speak_async("Game mode active. Performance boosted, launcher opened.")

    def _mode_code(self):
        self.tts.speak_async("Activating code mode.")
        self._apps.open_app('vscode', self.config)
        self._apps.open_app('chrome', self.config)
        url = self.config.get_url('github')
        if url:
            self._browser.open_url(url)
        playlist = self.config.get_playlist('focus')
        if playlist:
            self._browser.open_url(playlist)
        self.tts.speak_async("Code mode active. VS Code, browser, and focus music ready.")

    def _mode_study(self):
        self.tts.speak_async("Activating study mode.")
        for app in ['steam', 'discord', 'spotify']:
            self._apps.close_app(app)
        self._sys.set_dnd(True)
        url = self.config.get_url('notion')
        if url:
            self._browser.open_url(url)
            self._apps.open_app('chrome', self.config)
        playlist = self.config.get_playlist('lofi')
        if playlist:
            self._browser.open_url(playlist)
        self.tts.speak_async("Study mode active. Distractions removed, lo-fi music on.")

    def _mode_night(self):
        self.tts.speak_async("Activating night mode.")
        self._media.pause_all()
        self._sys.reduce_brightness()
        self._sys.set_dnd(True)
        self.tts.speak_async("Night mode active. Brightness reduced, notifications silenced.")

    def _mode_stream(self):
        self.tts.speak_async("Activating stream mode.")
        self._apps.open_app('obs', self.config)
        self._sys.boost_performance()
        self.tts.speak_async("Stream mode active. OBS launched, performance optimized.")
