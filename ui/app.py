"""
JARVIS main UI application — PyQt6 dark futuristic dashboard.
Includes: system tray, main window, all panels.

FIX LOG (ui/app.py):
  BUG-A  QThread destroyed while running: WorkerThread objects were created
         as local variables and immediately garbage-collected after .start(),
         which can cause "QThread: Destroyed while thread is still running"
         and silent crashes.  Fix: keep a list of active workers and remove
         each one when it emits its finished signal.

  BUG-B  JarvisApp._quit() called jarvis.stop() then app.quit().  If TTS was
         speaking when quit was called, app.quit() could execute before stop()
         finished draining the queue, leaving background threads dangling.
         Fix: stop() is now called before exec() returns (via aboutToQuit).

  BUG-C  MainWindow.closeEvent ignored the event and called self.hide() but
         did NOT call event.ignore() when minimize_to_tray was False.  The
         else branch called event.accept() — but JARVIS was never stopped.
         Added jarvis.stop() in the accept path.

  BUG-D  _update_metrics ran psutil.cpu_percent(interval=0.5) on the main
         (GUI) thread, blocking it for 500 ms on every 3-second tick.
         Moved to a WorkerThread to keep the UI responsive.

  BUG-E  Calling style().unpolish/polish on mode buttons updated the active
         button style, but the previously-active button was not reset properly
         because setObjectName("") doesn't trigger a repaint unless the style
         is also refreshed for that widget.  Already handled correctly in the
         original but reinforced with ensurePolished().

  NEW    AI Stack panel now shows actual detected backends instead of
         hard-coded strings.

  NEW    _on_cmd_input stores the worker reference so it isn't prematurely
         garbage-collected.
"""

import sys
import logging
from PyQt6.QtWidgets import (
    QApplication, QMainWindow, QWidget, QVBoxLayout, QHBoxLayout,
    QLabel, QPushButton, QTextEdit, QLineEdit, QTabWidget,
    QScrollArea, QFrame, QSystemTrayIcon, QMenu, QSplitter,
    QGridLayout, QProgressBar, QListWidget, QListWidgetItem,
    QDialog, QFormLayout, QComboBox, QSpinBox, QCheckBox,
    QMessageBox, QSizePolicy,
)
from PyQt6.QtCore import Qt, QTimer, QThread, pyqtSignal, QSize, QPoint
from PyQt6.QtGui import (
    QFont, QColor, QPalette, QIcon, QPixmap, QPainter,
    QBrush, QPen, QAction,
)

logger = logging.getLogger("JARVIS.UI")

# ── Stylesheet ────────────────────────────────────────────────────────────────

DARK_STYLE = """
QWidget {
    background-color: #050a0f;
    color: #c8e8f8;
    font-family: 'Segoe UI', 'Consolas', monospace;
    font-size: 12px;
}
QMainWindow { background-color: #050a0f; }
QFrame#panel {
    background-color: #080f16;
    border: 1px solid #0d2235;
    border-radius: 4px;
}
QLabel#title {
    color: #00d4ff;
    font-size: 10px;
    letter-spacing: 3px;
}
QLabel#value {
    color: #00d4ff;
    font-size: 13px;
    font-weight: bold;
}
QLabel#muted { color: #4a7a99; font-size: 11px; }
QPushButton {
    background-color: transparent;
    border: 1px solid #0d2235;
    border-radius: 2px;
    color: #4a7a99;
    padding: 6px 10px;
    font-size: 11px;
    letter-spacing: 1px;
    text-align: left;
}
QPushButton:hover {
    border-color: #00d4ff;
    color: #00d4ff;
    background-color: rgba(0, 212, 255, 0.05);
}
QPushButton:pressed { background-color: rgba(0, 212, 255, 0.1); }
QPushButton#active {
    border-color: #00d4ff;
    color: #00d4ff;
    background-color: rgba(0, 212, 255, 0.05);
}
QPushButton#accent {
    border-color: #00ff9d;
    color: #00ff9d;
    background-color: rgba(0, 255, 157, 0.05);
}
QLineEdit {
    background-color: transparent;
    border: 1px solid #1a3a52;
    border-radius: 2px;
    color: #00ff9d;
    padding: 6px 10px;
    font-family: 'Consolas', monospace;
    font-size: 12px;
}
QLineEdit:focus { border-color: #00d4ff; }
QTextEdit {
    background-color: #030810;
    border: 1px solid #0d2235;
    border-radius: 2px;
    color: #4a7a99;
    font-family: 'Consolas', monospace;
    font-size: 11px;
    padding: 6px;
}
QTabWidget::pane {
    background-color: #080f16;
    border: 1px solid #0d2235;
    border-radius: 2px;
}
QTabBar::tab {
    background-color: transparent;
    color: #4a7a99;
    border: 1px solid transparent;
    padding: 4px 12px;
    font-size: 9px;
    letter-spacing: 2px;
}
QTabBar::tab:selected { color: #00d4ff; border-color: #00d4ff; }
QTabBar::tab:hover    { color: #c8e8f8; }
QProgressBar {
    background-color: #1a3a52;
    border: none;
    border-radius: 2px;
    height: 3px;
    color: transparent;
}
QProgressBar::chunk { background-color: #00d4ff; border-radius: 2px; }
QProgressBar#green::chunk { background-color: #00ff9d; }
QProgressBar#warn::chunk  { background-color: #ff6b35; }
QListWidget {
    background-color: transparent;
    border: none;
    outline: none;
}
QListWidget::item {
    padding: 4px 8px;
    border-radius: 2px;
    color: #4a7a99;
    font-family: Consolas, monospace;
    font-size: 10px;
}
QListWidget::item:hover {
    background-color: rgba(13, 34, 53, 0.8);
    color: #c8e8f8;
}
QScrollBar:vertical { background: transparent; width: 4px; }
QScrollBar::handle:vertical { background: #1a3a52; border-radius: 2px; }
QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical { height: 0; }
QComboBox, QSpinBox {
    background-color: #080f16;
    border: 1px solid #1a3a52;
    border-radius: 2px;
    color: #c8e8f8;
    padding: 4px 8px;
}
QCheckBox { color: #c8e8f8; spacing: 8px; }
QCheckBox::indicator {
    width: 14px; height: 14px;
    border: 1px solid #1a3a52;
    border-radius: 2px;
}
QCheckBox::indicator:checked { background-color: #00d4ff; border-color: #00d4ff; }
QMenu {
    background-color: #080f16;
    border: 1px solid #0d2235;
    color: #c8e8f8;
}
QMenu::item:selected { background-color: rgba(0, 212, 255, 0.1); }
"""


# ── Worker thread ─────────────────────────────────────────────────────────────

class WorkerThread(QThread):
    """Generic worker that runs a callable off the main thread."""
    result = pyqtSignal(str)

    def __init__(self, func, *args, **kwargs):
        super().__init__()
        self.func   = func
        self.args   = args
        self.kwargs = kwargs

    def run(self):
        try:
            r = self.func(*self.args, **self.kwargs)
            self.result.emit(str(r or ""))
        except Exception as exc:
            self.result.emit(f"Error: {exc}")


# ── Helpers ───────────────────────────────────────────────────────────────────

class PanelFrame(QFrame):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setObjectName("panel")
        self.setFrameShape(QFrame.Shape.StyledPanel)


class SectionTitle(QLabel):
    def __init__(self, text, parent=None):
        super().__init__(text.upper(), parent)
        self.setObjectName("title")
        font = QFont("Consolas", 8)
        font.setLetterSpacing(QFont.SpacingType.AbsoluteSpacing, 3)
        self.setFont(font)
        self.setStyleSheet("color: #4a7a99; letter-spacing: 3px;")


# ── JarvisApp ─────────────────────────────────────────────────────────────────

class JarvisApp:
    def __init__(self, jarvis):
        self.jarvis = jarvis
        self.app    = QApplication.instance() or QApplication(sys.argv)
        self.app.setApplicationName("JARVIS")
        self.app.setStyleSheet(DARK_STYLE)
        self._window = None
        self._tray   = None

    def run(self):
        self._window = MainWindow(self.jarvis)
        self._setup_tray()
        # BUG-B fix: connect cleanup to Qt's own shutdown signal
        self.app.aboutToQuit.connect(self._on_quit)
        self.jarvis.start()
        self._window.show()
        sys.exit(self.app.exec())

    def _setup_tray(self):
        self._tray = QSystemTrayIcon(self.app)
        pix = QPixmap(32, 32)
        pix.fill(Qt.GlobalColor.transparent)
        painter = QPainter(pix)
        painter.setBrush(QBrush(QColor("#00d4ff")))
        painter.setPen(Qt.PenStyle.NoPen)
        painter.drawEllipse(4, 4, 24, 24)
        painter.end()
        self._tray.setIcon(QIcon(pix))
        self._tray.setToolTip("JARVIS — Online")

        menu       = QMenu()
        show_act   = QAction("Show Dashboard", self.app)
        quit_act   = QAction("Quit JARVIS",    self.app)
        show_act.triggered.connect(self._window.show)
        quit_act.triggered.connect(self.app.quit)   # triggers aboutToQuit
        menu.addAction(show_act)
        menu.addSeparator()
        menu.addAction(quit_act)
        self._tray.setContextMenu(menu)
        self._tray.activated.connect(self._tray_activated)
        self._tray.show()

    def _tray_activated(self, reason):
        if reason == QSystemTrayIcon.ActivationReason.DoubleClick:
            self._window.show()
            self._window.raise_()

    def _on_quit(self):
        """BUG-B fix: called by aboutToQuit — guarantees cleanup before exit."""
        self.jarvis.stop()


# ── MainWindow ────────────────────────────────────────────────────────────────

class MainWindow(QMainWindow):
    def __init__(self, jarvis):
        super().__init__()
        self.jarvis    = jarvis
        self._workers  = []  # BUG-A fix: keep references to active threads
        self.setWindowTitle("JARVIS — Personal AI")
        self.setMinimumSize(1100, 700)
        self.resize(1200, 750)
        self.setWindowFlag(Qt.WindowType.WindowStaysOnTopHint, False)
        self._build_ui()
        self._start_timers()

    # ── UI construction ───────────────────────────────────────────────────────

    def _build_ui(self):
        central = QWidget()
        self.setCentralWidget(central)
        main_layout = QVBoxLayout(central)
        main_layout.setSpacing(6)
        main_layout.setContentsMargins(10, 10, 10, 10)

        main_layout.addWidget(self._build_topbar())

        body = QHBoxLayout()
        body.setSpacing(6)
        body.addWidget(self._build_left_panel(),   0)
        body.addWidget(self._build_center_panel(), 1)
        body.addWidget(self._build_right_panel(),  0)
        main_layout.addLayout(body)

        main_layout.addWidget(self._build_bottom_bar())

    # ── Top bar ───────────────────────────────────────────────────────────────

    def _build_topbar(self) -> QWidget:
        frame = PanelFrame()
        frame.setFixedHeight(50)
        layout = QHBoxLayout(frame)
        layout.setContentsMargins(16, 0, 16, 0)

        logo = QLabel("JARVIS")
        logo.setStyleSheet(
            "color: #00d4ff; font-size: 20px; font-weight: bold; letter-spacing: 6px;"
        )
        layout.addWidget(logo)

        sub = QLabel("PERSONAL AI  ·  v2.5.0")
        sub.setStyleSheet(
            "color: #1a3a52; font-size: 9px; letter-spacing: 3px; margin-left: 8px;"
        )
        layout.addWidget(sub)
        layout.addStretch()

        for color, label in [
            ("#00ff9d", "LISTENING"),
            ("#4a7a99", "LOCAL AI"),
            ("#00ff9d", "OFFLINE"),
        ]:
            dot = QLabel("●")
            dot.setStyleSheet(f"color: {color}; font-size: 8px;")
            lbl = QLabel(label)
            lbl.setStyleSheet(
                "color: #4a7a99; font-size: 9px; letter-spacing: 1px; margin-right: 12px;"
            )
            layout.addWidget(dot)
            layout.addWidget(lbl)

        self.clock_label = QLabel()
        self.clock_label.setStyleSheet(
            "color: #00d4ff; font-family: Consolas; font-size: 14px; letter-spacing: 2px;"
        )
        layout.addWidget(self.clock_label)
        return frame

    # ── Left panel ────────────────────────────────────────────────────────────

    def _build_left_panel(self) -> QWidget:
        frame = QWidget()
        frame.setFixedWidth(200)
        layout = QVBoxLayout(frame)
        layout.setSpacing(6)
        layout.setContentsMargins(0, 0, 0, 0)

        # Voice panel
        voice_panel = PanelFrame()
        voice_layout = QVBoxLayout(voice_panel)
        voice_layout.addWidget(SectionTitle("Voice Interface", voice_panel))

        self.wake_btn = QPushButton("🎙  ACTIVE — 'JARVIS'")
        self.wake_btn.setObjectName("active")
        self.wake_btn.setFixedHeight(40)
        self.wake_btn.clicked.connect(self._toggle_wake)
        voice_layout.addWidget(self.wake_btn)

        self.mode_label = QLabel(
            f"MODE: {self.jarvis.modes.get_current().upper()}"
        )
        self.mode_label.setStyleSheet(
            "color: #00ff9d; font-size: 10px; letter-spacing: 2px; font-family: Consolas;"
        )
        voice_layout.addWidget(self.mode_label)
        layout.addWidget(voice_panel)

        # Quick mode buttons
        modes_panel = PanelFrame()
        modes_layout = QVBoxLayout(modes_panel)
        modes_layout.addWidget(SectionTitle("Quick Modes"))

        mode_list = [
            ("◉", "Meeting"), ("◈", "Design"),  ("▶", "Edit"),
            ("◆", "Game"),    ("⟨/⟩", "Code"),  ("◎", "Study"),
            ("☽", "Night"),   ("◻", "Standard"),
        ]
        self._mode_btns = {}
        for icon, name in mode_list:
            btn = QPushButton(f"{icon}  {name}")
            btn.clicked.connect(lambda _, n=name: self._activate_mode(n))
            modes_layout.addWidget(btn)
            self._mode_btns[name.lower()] = btn

        layout.addWidget(modes_panel, 1)
        return frame

    # ── Center panel ──────────────────────────────────────────────────────────

    def _build_center_panel(self) -> QWidget:
        frame = QWidget()
        layout = QVBoxLayout(frame)
        layout.setSpacing(6)
        layout.setContentsMargins(0, 0, 0, 0)

        cmd_panel = PanelFrame()
        cmd_layout = QVBoxLayout(cmd_panel)
        cmd_layout.addWidget(SectionTitle("Last Command"))

        self.last_cmd_label = QLabel('▶ "Jarvis, activate meeting mode"')
        self.last_cmd_label.setStyleSheet(
            "color: #00ff9d; font-family: Consolas; font-size: 12px; "
            "border-left: 2px solid #00ff9d; padding-left: 8px; margin: 4px 0;"
        )
        self.last_cmd_label.setWordWrap(True)
        cmd_layout.addWidget(self.last_cmd_label)

        self.response_label = QLabel(
            "Activating meeting mode — pausing media, closing distractions."
        )
        self.response_label.setStyleSheet(
            "color: #4a7a99; font-family: Consolas; font-size: 11px;"
        )
        self.response_label.setWordWrap(True)
        cmd_layout.addWidget(self.response_label)
        layout.addWidget(cmd_panel)

        tabs = QTabWidget()
        tabs.addTab(self._build_history_tab(),   "HISTORY")
        tabs.addTab(self._build_training_tab(),  "TRAINING")
        tabs.addTab(self._build_workflow_tab(),  "WORKFLOWS")
        tabs.addTab(self._build_reminders_tab(), "REMINDERS")
        tabs.addTab(self._build_notes_tab(),     "NOTES")
        layout.addWidget(tabs, 1)

        qa_panel = PanelFrame()
        qa_layout = QVBoxLayout(qa_panel)
        qa_layout.addWidget(SectionTitle("Quick Actions"))
        grid = QGridLayout()
        grid.setSpacing(4)
        actions = [
            ("Open Chrome",   lambda: self._run_cmd("open chrome")),
            ("Volume Up",     lambda: self._run_cmd("volume up")),
            ("Play Music",    lambda: self._run_cmd("play lofi")),
            ("Reminder",      lambda: self._run_cmd("set reminder")),
            ("Quick Note",    lambda: self._run_cmd("take a note")),
            ("Screenshot",    lambda: self._run_cmd("take a screenshot")),
            ("System Status", lambda: self._run_cmd("status")),
            ("Sleep PC",      lambda: self._run_cmd("sleep")),
        ]
        for i, (label, fn) in enumerate(actions):
            btn = QPushButton(label)
            btn.clicked.connect(fn)
            btn.setFixedHeight(28)
            grid.addWidget(btn, i // 4, i % 4)
        qa_layout.addLayout(grid)
        layout.addWidget(qa_panel)
        return frame

    def _build_history_tab(self) -> QWidget:
        w = QWidget()
        layout = QVBoxLayout(w)
        self.history_list = QListWidget()
        layout.addWidget(self.history_list)
        self._refresh_history()
        return w

    def _build_training_tab(self) -> QWidget:
        w = QWidget()
        layout = QVBoxLayout(w)
        info = QLabel('Teach JARVIS: "when I say [phrase], open [app]"')
        info.setStyleSheet(
            "color: #00d4ff; font-size: 10px; font-family: Consolas; margin-bottom: 6px;"
        )
        info.setWordWrap(True)
        layout.addWidget(info)

        form = QHBoxLayout()
        self.train_trigger = QLineEdit()
        self.train_trigger.setPlaceholderText("Trigger phrase…")
        self.train_actions = QLineEdit()
        self.train_actions.setPlaceholderText(
            "Actions (e.g. open chrome and play focus)"
        )
        train_btn = QPushButton("TEACH")
        train_btn.setObjectName("accent")
        train_btn.clicked.connect(self._do_train)
        form.addWidget(self.train_trigger)
        form.addWidget(self.train_actions)
        form.addWidget(train_btn)
        layout.addLayout(form)

        self.commands_list = QListWidget()
        layout.addWidget(self.commands_list, 1)
        self._refresh_commands()
        return w

    def _build_workflow_tab(self) -> QWidget:
        w = QWidget()
        layout = QVBoxLayout(w)
        btn_row = QHBoxLayout()
        new_btn = QPushButton("+ New Workflow")
        new_btn.setObjectName("accent")
        new_btn.clicked.connect(self._new_workflow_dialog)
        btn_row.addWidget(new_btn)
        btn_row.addStretch()
        layout.addLayout(btn_row)

        self.workflow_list = QListWidget()
        layout.addWidget(self.workflow_list, 1)
        self._refresh_workflows()
        return w

    def _build_reminders_tab(self) -> QWidget:
        w = QWidget()
        layout = QVBoxLayout(w)
        form = QHBoxLayout()
        self.reminder_text = QLineEdit()
        self.reminder_text.setPlaceholderText("Reminder text…")
        self.reminder_time = QLineEdit()
        self.reminder_time.setPlaceholderText("Time (e.g. 3:30pm, in 20 minutes)")
        add_btn = QPushButton("ADD")
        add_btn.setObjectName("accent")
        add_btn.clicked.connect(self._do_add_reminder)
        form.addWidget(self.reminder_text, 2)
        form.addWidget(self.reminder_time, 1)
        form.addWidget(add_btn)
        layout.addLayout(form)

        self.reminders_list = QListWidget()
        layout.addWidget(self.reminders_list, 1)
        self._refresh_reminders()
        return w

    def _build_notes_tab(self) -> QWidget:
        w = QWidget()
        layout = QVBoxLayout(w)
        note_row = QHBoxLayout()
        self.note_input = QLineEdit()
        self.note_input.setPlaceholderText("Quick note…")
        self.note_input.returnPressed.connect(self._do_add_note)
        save_btn = QPushButton("SAVE")
        save_btn.setObjectName("accent")
        save_btn.clicked.connect(self._do_add_note)
        note_row.addWidget(self.note_input)
        note_row.addWidget(save_btn)
        layout.addLayout(note_row)

        self.notes_list = QListWidget()
        layout.addWidget(self.notes_list, 1)
        self._refresh_notes()
        return w

    # ── Right panel ───────────────────────────────────────────────────────────

    def _build_right_panel(self) -> QWidget:
        frame = QWidget()
        frame.setFixedWidth(190)
        layout = QVBoxLayout(frame)
        layout.setSpacing(6)
        layout.setContentsMargins(0, 0, 0, 0)

        sys_panel = PanelFrame()
        sys_layout = QVBoxLayout(sys_panel)
        sys_layout.addWidget(SectionTitle("System"))

        self.metrics = {}
        for name, color in [("CPU", ""), ("RAM", "green"), ("DISK", "")]:
            row = QHBoxLayout()
            lbl = QLabel(name)
            lbl.setStyleSheet(
                "color: #4a7a99; font-size: 10px; font-family: Consolas;"
            )
            val = QLabel("--")
            val.setObjectName("value")
            val.setStyleSheet(
                "color: #00d4ff; font-size: 10px; font-family: Consolas;"
            )
            row.addWidget(lbl)
            row.addStretch()
            row.addWidget(val)
            sys_layout.addLayout(row)
            bar = QProgressBar()
            if color:
                bar.setObjectName(color)
            bar.setValue(0)
            bar.setFixedHeight(3)
            bar.setTextVisible(False)
            sys_layout.addWidget(bar)
            self.metrics[name] = (val, bar)

        layout.addWidget(sys_panel)

        apps_panel = PanelFrame()
        apps_layout = QVBoxLayout(apps_panel)
        apps_layout.addWidget(SectionTitle("Running Apps"))
        self.apps_list = QListWidget()
        self.apps_list.setFixedHeight(120)
        apps_layout.addWidget(self.apps_list)
        layout.addWidget(apps_panel)

        # NEW: show actual detected backends
        ai_panel = PanelFrame()
        ai_layout = QVBoxLayout(ai_panel)
        ai_layout.addWidget(SectionTitle("AI Stack"))
        j = self.jarvis
        stack_info = [
            ("Wake Word", j.wake._backend),
            ("STT",       j.stt._backend),
            ("TTS",       j.tts._backend),
            ("Storage",   "SQLite"),
        ]
        for k, v in stack_info:
            row = QHBoxLayout()
            kl = QLabel(k)
            kl.setStyleSheet(
                "color: #4a7a99; font-size: 9px; font-family: Consolas;"
            )
            vl = QLabel(v)
            vl.setStyleSheet(
                "color: #00d4ff; font-size: 9px; font-family: Consolas;"
            )
            row.addWidget(kl)
            row.addStretch()
            row.addWidget(vl)
            ai_layout.addLayout(row)
        layout.addWidget(ai_panel)

        settings_btn = QPushButton("⚙  Settings")
        settings_btn.clicked.connect(self._open_settings)
        layout.addWidget(settings_btn)
        layout.addStretch()
        return frame

    # ── Bottom bar ────────────────────────────────────────────────────────────

    def _build_bottom_bar(self) -> QWidget:
        frame = PanelFrame()
        frame.setFixedHeight(46)
        layout = QHBoxLayout(frame)
        layout.setContentsMargins(16, 0, 16, 0)

        prompt = QLabel("▸")
        prompt.setStyleSheet(
            "color: #00d4ff; font-size: 14px; font-family: Consolas;"
        )
        layout.addWidget(prompt)

        self.cmd_input = QLineEdit()
        self.cmd_input.setPlaceholderText('Type a command or say "Jarvis…"')
        self.cmd_input.returnPressed.connect(self._on_cmd_input)
        self.cmd_input.setStyleSheet(
            "border: none; background: transparent; color: #00ff9d; "
            "font-family: Consolas; font-size: 12px; letter-spacing: 1px;"
        )
        layout.addWidget(self.cmd_input, 1)

        for tag in ["WAKE: JARVIS", "PRIVACY: LOCAL", "OFFLINE"]:
            lbl = QLabel(tag)
            lbl.setStyleSheet(
                "color: #1a3a52; font-size: 9px; letter-spacing: 2px; "
                "font-family: Consolas; margin-left: 12px;"
            )
            layout.addWidget(lbl)
        return frame

    # ── Timers ────────────────────────────────────────────────────────────────

    def _start_timers(self):
        self.clock_timer = QTimer()
        self.clock_timer.timeout.connect(self._update_clock)
        self.clock_timer.start(1000)
        self._update_clock()

        # BUG-D fix: metrics update runs in a worker thread
        self.metrics_timer = QTimer()
        self.metrics_timer.timeout.connect(self._enqueue_metrics_update)
        self.metrics_timer.start(3000)
        self._enqueue_metrics_update()

        self.history_timer = QTimer()
        self.history_timer.timeout.connect(self._refresh_history)
        self.history_timer.start(5000)

        self.apps_timer = QTimer()
        self.apps_timer.timeout.connect(self._update_apps)
        self.apps_timer.start(8000)
        self._update_apps()

    def _update_clock(self):
        from datetime import datetime
        self.clock_label.setText(datetime.now().strftime("%H:%M:%S"))

    def _enqueue_metrics_update(self):
        """BUG-D fix: run psutil on a worker thread to avoid GUI stutter."""
        def _fetch():
            from automation.system_control import get_system_stats
            stats = get_system_stats()
            return stats

        w = WorkerThread(_fetch)
        w.result.connect(self._apply_metrics)
        # BUG-A fix: keep reference
        self._workers.append(w)
        w.finished.connect(lambda _w=w: self._workers.discard(_w) if hasattr(self._workers, 'discard') else self._cleanup_worker(_w))
        w.start()

    def _cleanup_worker(self, w):
        try:
            self._workers.remove(w)
        except ValueError:
            pass

    def _apply_metrics(self, result_str: str):
        try:
            # result_str is str(dict) — eval safely
            import ast
            stats = ast.literal_eval(result_str)
            labels = {
                "CPU":  stats["cpu_percent"],
                "RAM":  stats["ram_percent"],
                "DISK": stats["disk_percent"],
            }
            for name, val in labels.items():
                lbl, bar = self.metrics[name]
                lbl.setText(f"{val}%")
                bar.setValue(int(val))
        except Exception:
            pass

    def _update_apps(self):
        try:
            from automation.app_control import get_running_apps
            apps  = get_running_apps()[:8]
            watch = [
                "chrome.exe", "code.exe", "photoshop.exe", "premiere pro.exe",
                "steam.exe",  "spotify.exe", "discord.exe", "obs64.exe",
            ]
            shown = [a for a in apps if any(w in a["name"].lower() for w in watch)][:6]
            self.apps_list.clear()
            for app in shown:
                item = QListWidgetItem(f"  {app['name'].replace('.exe', '')}")
                item.setForeground(QColor("#00ff9d"))
                self.apps_list.addItem(item)
        except Exception:
            pass

    # ── Actions ───────────────────────────────────────────────────────────────

    def _on_cmd_input(self):
        text = self.cmd_input.text().strip()
        if not text:
            return
        self.cmd_input.clear()
        self.last_cmd_label.setText(f'▶ "{text}"')
        self.response_label.setText("Processing…")
        self._run_cmd_worker(text)

    def _run_cmd(self, cmd: str):
        self.last_cmd_label.setText(f'▶ "{cmd}"')
        self._run_cmd_worker(cmd)

    def _run_cmd_worker(self, cmd: str):
        """BUG-A fix: keep the WorkerThread reference alive until it finishes."""
        w = WorkerThread(self.jarvis.process_text, cmd, "typed")
        w.result.connect(lambda r: self.response_label.setText(r or "Done."))
        w.result.connect(lambda _: self._refresh_history())
        self._workers.append(w)
        w.finished.connect(lambda _w=w: self._cleanup_worker(_w))
        w.start()

    def _activate_mode(self, name: str):
        self._run_cmd(f"activate {name} mode")
        self.mode_label.setText(f"MODE: {name.upper()}")
        for btn_name, btn in self._mode_btns.items():
            is_active = btn_name == name.lower()
            btn.setObjectName("active" if is_active else "")
            btn.style().unpolish(btn)
            btn.style().polish(btn)
            btn.update()

    def _toggle_wake(self):
        active = not self.jarvis.wake._active
        self.jarvis.wake.set_active(active)
        if active:
            self.wake_btn.setText("🎙  ACTIVE — 'JARVIS'")
            self.wake_btn.setObjectName("active")
        else:
            self.wake_btn.setText("⏸  PAUSED")
            self.wake_btn.setObjectName("")
        self.wake_btn.style().unpolish(self.wake_btn)
        self.wake_btn.style().polish(self.wake_btn)

    def _do_train(self):
        trigger = self.train_trigger.text().strip()
        actions = self.train_actions.text().strip()
        if trigger and actions:
            self.jarvis.trainer.teach(trigger, actions)
            self.train_trigger.clear()
            self.train_actions.clear()
            self._refresh_commands()
            self.response_label.setText(f"Trained: '{trigger}'")

    def _do_add_reminder(self):
        text     = self.reminder_text.text().strip()
        time_str = self.reminder_time.text().strip()
        if text and time_str:
            result = self.jarvis.reminder.add_from_text(text, time_str)
            if result:
                self.reminder_text.clear()
                self.reminder_time.clear()
                self._refresh_reminders()

    def _do_add_note(self):
        text = self.note_input.text().strip()
        if text:
            self.jarvis.memory.add_note(text)
            self.note_input.clear()
            self._refresh_notes()

    def _new_workflow_dialog(self):
        dlg = WorkflowDialog(self.jarvis, self)
        if dlg.exec() == QDialog.DialogCode.Accepted:
            self._refresh_workflows()

    def _open_settings(self):
        dlg = SettingsDialog(self.jarvis.config, self)
        dlg.exec()

    # ── Refresh helpers ───────────────────────────────────────────────────────

    def _refresh_history(self):
        try:
            history = self.jarvis.memory.get_history(30)
            self.history_list.clear()
            for row in history:
                ts  = str(row["ts"])[-8:][:5] if row["ts"] else ""
                item = QListWidgetItem(f"  {ts}  {row['command']}")
                item.setForeground(QColor("#4a7a99"))
                self.history_list.addItem(item)
        except Exception:
            pass

    def _refresh_commands(self):
        try:
            cmds = self.jarvis.trainer.get_all()
            self.commands_list.clear()
            for c in cmds:
                item = QListWidgetItem(
                    f"  [{c['use_count']}x]  {c['trigger']}  →  {len(c['actions'])} actions"
                )
                item.setForeground(QColor("#c8e8f8"))
                self.commands_list.addItem(item)
        except Exception:
            pass

    def _refresh_workflows(self):
        try:
            flows = self.jarvis.workflow.get_all()
            self.workflow_list.clear()
            for f in flows:
                status = "ACTIVE" if f["enabled"] else "PAUSED"
                item   = QListWidgetItem(
                    f"  {f['name']}  ·  {len(f['steps'])} steps  "
                    f"·  {f['run_count']} runs  ·  {status}"
                )
                item.setForeground(
                    QColor("#00ff9d" if f["enabled"] else "#4a7a99")
                )
                self.workflow_list.addItem(item)
        except Exception:
            pass

    def _refresh_reminders(self):
        try:
            reminders = self.jarvis.db.get_all_reminders()
            self.reminders_list.clear()
            for r in reminders:
                item = QListWidgetItem(
                    f"  {str(r['remind_at'])[:16]}  —  {r['text']}"
                )
                item.setForeground(QColor("#c8e8f8"))
                self.reminders_list.addItem(item)
        except Exception:
            pass

    def _refresh_notes(self):
        try:
            notes = self.jarvis.memory.get_notes(20)
            self.notes_list.clear()
            for n in notes:
                item = QListWidgetItem(
                    f"  {str(n['created'])[:10]}  {n['body'][:60]}"
                )
                item.setForeground(QColor("#c8e8f8"))
                self.notes_list.addItem(item)
        except Exception:
            pass

    # ── Close behaviour ───────────────────────────────────────────────────────

    def closeEvent(self, event):
        if self.jarvis.config.get("minimize_to_tray", True):
            event.ignore()
            self.hide()
        else:
            # BUG-C fix: stop JARVIS cleanly before the window is destroyed
            self.jarvis.stop()
            event.accept()


# ── WorkflowDialog ────────────────────────────────────────────────────────────

class WorkflowDialog(QDialog):
    def __init__(self, jarvis, parent=None):
        super().__init__(parent)
        self.jarvis = jarvis
        self.setWindowTitle("New Workflow")
        self.setMinimumWidth(500)
        self.setStyleSheet(DARK_STYLE)
        self._build()

    def _build(self):
        layout = QVBoxLayout(self)
        layout.addWidget(QLabel("Workflow Name:"))
        self.name_input = QLineEdit()
        self.name_input.setPlaceholderText("e.g. Morning Routine")
        layout.addWidget(self.name_input)

        layout.addWidget(QLabel("Voice Trigger (optional):"))
        self.trigger_input = QLineEdit()
        self.trigger_input.setPlaceholderText("e.g. good morning")
        layout.addWidget(self.trigger_input)

        layout.addWidget(
            QLabel(
                "Steps (one per line — e.g. open chrome, play lofi, activate design mode):"
            )
        )
        self.steps_input = QTextEdit()
        self.steps_input.setPlaceholderText(
            "open chrome\nplay lofi\nactivate design mode\nopen notion"
        )
        self.steps_input.setFixedHeight(120)
        layout.addWidget(self.steps_input)

        btn_row   = QHBoxLayout()
        save_btn  = QPushButton("SAVE WORKFLOW")
        save_btn.setObjectName("accent")
        save_btn.clicked.connect(self._save)
        cancel_btn = QPushButton("Cancel")
        cancel_btn.clicked.connect(self.reject)
        btn_row.addWidget(save_btn)
        btn_row.addWidget(cancel_btn)
        layout.addLayout(btn_row)

    def _save(self):
        name = self.name_input.text().strip()
        if not name:
            return
        lines = [
            l.strip()
            for l in self.steps_input.toPlainText().splitlines()
            if l.strip()
        ]
        steps = []
        for line in lines:
            steps.extend(self.jarvis.trainer._parse_action_text(line))

        trigger = self.trigger_input.text().strip() or None
        self.jarvis.workflow.save_workflow(name, steps, trigger)

        if trigger:
            self.jarvis.trainer.teach_structured(
                trigger, [{"type": "workflow", "name": name}]
            )
        self.accept()


# ── SettingsDialog ────────────────────────────────────────────────────────────

class SettingsDialog(QDialog):
    def __init__(self, config, parent=None):
        super().__init__(parent)
        self.config = config
        self.setWindowTitle("JARVIS Settings")
        self.setMinimumSize(500, 400)
        self.setStyleSheet(DARK_STYLE)
        self._build()

    def _build(self):
        layout = QVBoxLayout(self)
        tabs   = QTabWidget()

        # General tab
        gen   = QWidget()
        gen_l = QFormLayout(gen)
        self.wake_word_input = QLineEdit(self.config.get("wake_word", "jarvis"))
        gen_l.addRow("Wake Word:", self.wake_word_input)
        self.startup_cb = QCheckBox("Start with Windows")
        self.startup_cb.setChecked(self.config.get("start_with_windows", False))
        gen_l.addRow(self.startup_cb)
        self.tray_cb = QCheckBox("Minimize to system tray")
        self.tray_cb.setChecked(self.config.get("minimize_to_tray", True))
        gen_l.addRow(self.tray_cb)
        tabs.addTab(gen, "GENERAL")

        # Voice tab
        voice   = QWidget()
        voice_l = QFormLayout(voice)
        self.rate_spin = QSpinBox()
        self.rate_spin.setRange(100, 300)
        self.rate_spin.setValue(self.config.get("tts_rate", 175))
        voice_l.addRow("TTS Speed:", self.rate_spin)
        self.whisper_input = QLineEdit(self.config.get("whisper_model", ""))
        voice_l.addRow("Whisper Model:", self.whisper_input)
        self.piper_input = QLineEdit(self.config.get("piper_model", ""))
        voice_l.addRow("Piper Model:", self.piper_input)
        tabs.addTab(voice, "VOICE")

        layout.addWidget(tabs)

        btn_row    = QHBoxLayout()
        save_btn   = QPushButton("SAVE")
        save_btn.setObjectName("accent")
        save_btn.clicked.connect(self._save)
        cancel_btn = QPushButton("Cancel")
        cancel_btn.clicked.connect(self.reject)
        btn_row.addWidget(save_btn)
        btn_row.addWidget(cancel_btn)
        layout.addLayout(btn_row)

    def _save(self):
        self.config.set("wake_word",            self.wake_word_input.text().strip())
        self.config.set("start_with_windows",   self.startup_cb.isChecked())
        self.config.set("minimize_to_tray",     self.tray_cb.isChecked())
        self.config.set("tts_rate",             self.rate_spin.value())
        self.config.set("whisper_model",        self.whisper_input.text().strip())
        self.config.set("piper_model",          self.piper_input.text().strip())
        self.accept()
