# JARVIS — Personal AI Desktop Assistant

Fully offline, privacy-first, Windows-native personal AI.  
No cloud. No APIs. No telemetry. Everything runs on your machine.

---

## Quick Start

```bash
# 1. Clone / extract to a folder
cd jarvis

# 2. Install dependencies + download models
python scripts/setup.py

# 3. Launch
python main.py
```

---

## Voice Commands

### Basics
| Say | What happens |
|-----|------|
| "Jarvis, open Chrome" | Opens Chrome browser |
| "Jarvis, volume up" | Increases volume by 10% |
| "Jarvis, set volume to 60" | Sets volume to 60% |
| "Jarvis, play focus" | Opens focus music playlist |
| "Jarvis, mute" | Toggles system mute |
| "Jarvis, take a screenshot" | Saves screenshot to Desktop |
| "Jarvis, what time is it" | Speaks current time |
| "Jarvis, status" | Reports CPU/RAM/mode |

### Modes
| Say | What happens |
|-----|------|
| "Jarvis, activate meeting mode" | Pauses media, closes apps, enables DND |
| "Jarvis, activate design mode" | Opens Photoshop, browser, reference sites, music |
| "Jarvis, activate edit mode" | Opens Premiere Pro, resources, editing music |
| "Jarvis, activate game mode" | Closes work apps, boosts performance, opens Steam |
| "Jarvis, activate code mode" | Opens VS Code, GitHub, focus music |
| "Jarvis, activate study mode" | Removes distractions, opens Notion, lo-fi music |
| "Jarvis, activate night mode" | Dims screen, silences notifications |

### Reminders
| Say | What happens |
|-----|------|
| "Remind me to check email at 3pm" | Sets 3:00 PM reminder |
| "Remind me to take a break in 45 minutes" | Sets reminder 45 min from now |
| "Set a reminder for client call at 2:30pm" | Sets reminder |

### Notes
| Say | What happens |
|-----|------|
| "Note: finish the landing page" | Saves note |
| "Take a note about the meeting summary" | Saves note |

### Training Custom Commands
```
"Teach Jarvis: when I say Start Focus, open Notion and play lofi and mute notifications"
"Teach Jarvis: when I say Deploy, open VS Code and open GitHub"
"Forget Start Focus"
"List my commands"
```

### Workflows
```
"Run workflow Morning Routine"
"Create workflow Evening Wrap"
"List workflows"
```

### System
| Say | What happens |
|-----|------|
| "Jarvis, shut down" | Schedules shutdown in 30s |
| "Jarvis, restart" | Schedules restart in 30s |
| "Jarvis, sleep" | Puts PC to sleep |
| "Jarvis, lock the screen" | Locks Windows |

---

## Project Structure

```
jarvis/
├── main.py                    # Entry point
├── requirements.txt
├── core/
│   ├── config.py              # Config manager (data/config.json)
│   ├── database.py            # SQLite storage
│   ├── wake_word.py           # Wake word detection (openwakeword)
│   ├── stt.py                 # Speech-to-text (Whisper)
│   ├── tts.py                 # Text-to-speech (Piper / pyttsx3)
│   ├── intent.py              # Command parser & router
│   ├── memory.py              # Note/history/recall engine
│   └── trainer.py             # Manual training engine
├── modes/
│   └── mode_manager.py        # All mode logic
├── automation/
│   ├── app_control.py         # Open/close apps
│   ├── system_control.py      # Shutdown, sleep, clipboard
│   ├── media_control.py       # Volume, playback
│   ├── browser_control.py     # URL opening
│   ├── workflow_engine.py     # Multi-step automation runner
│   └── reminder.py            # Reminder scheduler
├── ui/
│   └── app.py                 # PyQt6 dark dashboard
├── service/
│   └── jarvis_service.py      # Windows Service wrapper
├── scripts/
│   ├── setup.py               # First-run setup
│   └── build.py               # PyInstaller packager
├── data/                      # Local data (auto-created)
│   ├── config.json
│   ├── jarvis.db
│   ├── modes.json
│   └── jarvis.log
└── models/                    # AI models (downloaded by setup.py)
    ├── wake_word/
    ├── whisper/
    └── piper/
```

---

## Configuration

Edit `data/config.json` to customize:

- `wake_word` — default `"jarvis"`
- `app_paths` — add your installed apps
- `browser_shortcuts` — custom URL shortcuts
- `music_playlists` — Spotify / YouTube playlist URLs
- `tts_rate` — speech speed (100–300)
- `start_with_windows` — auto-start toggle

---

## AI / Model Stack

| Component | Tool | Size |
|-----------|------|------|
| Wake word | openwakeword | ~1 MB |
| Speech recognition | whisper.cpp `small.en` | ~142 MB |
| Text to speech | piper-tts `lessac-medium` | ~65 MB |
| Storage | SQLite | < 1 MB |
| Total disk | | ~210 MB |

**No internet connection required for any core feature.**

---

## Privacy

- Zero telemetry
- Zero cloud calls
- Zero API keys
- All audio processed in-RAM, never written unless screenshot
- All data stored in `data/` folder which you fully own
- Works completely air-gapped

---

## Building a .exe

```bash
pip install pyinstaller
python scripts/build.py
# Output: dist/JARVIS.exe
```

---

## Install as Windows Service

```bash
pip install pywin32
python service/jarvis_service.py install
python service/jarvis_service.py start
```

---

## Creating Custom Modes

**Via voice:**
```
"Teach Jarvis: when I say Client Mode, open chrome and open notion and mute notifications"
```

**Via dashboard:**
Open JARVIS → Workflows tab → New Workflow → add steps → save.

**Via modes.json** (`data/modes.json`):
```json
{
  "streaming": {
    "name": "Streaming Mode",
    "description": "Live streaming setup",
    "steps": [
      {"type": "open_app", "target": "obs"},
      {"type": "open_app", "target": "discord"},
      {"type": "media", "action": "play", "playlist": "gaming"},
      {"type": "set_volume", "level": 70},
      {"type": "speak", "text": "Stream mode ready. You're live!"}
    ]
  }
}
```

---

## License

MIT — personal use, modify freely.
