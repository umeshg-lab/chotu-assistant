"""
App control — open and close Windows applications.
Uses subprocess + psutil for process management.
"""

import glob
import logging
import os
from pathlib import Path
import shutil
import subprocess
import re
from typing import Optional

import psutil

logger = logging.getLogger('JARVIS.AppControl')


_APP_ALIASES = {
    "browser": "chrome",
    "web browser": "chrome",
    "google chrome": "chrome",
    "editor": "vscode",
    "code editor": "vscode",
    "vs code": "vscode",
    "visual studio code": "vscode",
    "premiere pro": "premiere",
    "adobe premiere": "premiere",
    "adobe premiere pro": "premiere",
    "adobe photoshop": "photoshop",
    "afterfx": "after effects",
    "aftereffects": "after effects",
}


_GLOB_FALLBACKS = {
    "chrome": [
        r"C:\Program Files\Google\Chrome\Application\chrome.exe",
        r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
    ],
    "photoshop": [
        r"C:\Program Files\Adobe\Adobe Photoshop *\Photoshop.exe",
        r"C:\Program Files (x86)\Adobe\Adobe Photoshop *\Photoshop.exe",
    ],
    "premiere": [
        r"C:\Program Files\Adobe\Adobe Premiere Pro *\Adobe Premiere Pro.exe",
        r"C:\Program Files (x86)\Adobe\Adobe Premiere Pro *\Adobe Premiere Pro.exe",
    ],
    "after effects": [
        r"C:\Program Files\Adobe\Adobe After Effects *\Support Files\AfterFX.exe",
        r"C:\Program Files\Adobe\Adobe After Effects *\AfterFX.exe",
    ],
    "illustrator": [
        r"C:\Program Files\Adobe\Adobe Illustrator *\Support Files\Contents\Windows\Illustrator.exe",
    ],
    "discord": [
        r"C:\Users\*\AppData\Local\Discord\app-*\Discord.exe",
    ],
    "steam": [
        r"C:\Program Files (x86)\Steam\steam.exe",
        r"C:\Program Files\Steam\steam.exe",
    ],
    "vscode": [
        r"C:\Users\*\AppData\Local\Programs\Microsoft VS Code\Code.exe",
        r"C:\Program Files\Microsoft VS Code\Code.exe",
    ],
    "spotify": [
        r"C:\Users\*\AppData\Roaming\Spotify\Spotify.exe",
    ],
    "firefox": [
        r"C:\Program Files\Mozilla Firefox\firefox.exe",
        r"C:\Program Files (x86)\Mozilla Firefox\firefox.exe",
    ],
    "vlc": [
        r"C:\Program Files\VideoLAN\VLC\vlc.exe",
        r"C:\Program Files (x86)\VideoLAN\VLC\vlc.exe",
    ],
    "obs": [
        r"C:\Program Files\obs-studio\bin\64bit\obs64.exe",
        r"C:\Program Files (x86)\obs-studio\bin\64bit\obs64.exe",
    ],
    "notion": [
        r"C:\Users\*\AppData\Local\Programs\Notion\Notion.exe",
        r"C:\Users\*\AppData\Local\Notion\Notion.exe",
    ],
}


_COMMAND_FALLBACKS = {
    "notepad": "notepad.exe",
    "explorer": "explorer.exe",
    "taskmgr": "taskmgr.exe",
}

_ALLOWED_SYSTEM_COMMANDS = {"notepad", "explorer", "taskmgr", "task manager"}

_PROTECTED_EXECUTABLES = {
    "cmd.exe",
    "powershell.exe",
    "pwsh.exe",
    "regedit.exe",
    "regedt32.exe",
    "mmc.exe",
    "control.exe",
    "services.msc",
    "compmgmt.msc",
    "diskmgmt.msc",
    "eventvwr.exe",
    "gpedit.msc",
    "secpol.msc",
    "taskschd.msc",
    "wf.msc",
}

_PROTECTED_START_MENU_LABELS = {
    "command prompt",
    "computer management",
    "control panel",
    "device manager",
    "disk cleanup",
    "disk management",
    "event viewer",
    "local security policy",
    "registry editor",
    "services",
    "system configuration",
    "task scheduler",
    "windows defender firewall",
    "windows powershell",
    "windows terminal",
}

_START_MENU_TERMS = {
    "chrome": {"chrome", "google chrome"},
    "firefox": {"firefox", "mozilla firefox"},
    "vscode": {"code", "vs code", "visual studio code"},
    "photoshop": {"photoshop", "adobe photoshop"},
    "premiere": {"premiere", "premiere pro", "adobe premiere pro"},
    "after effects": {"after effects", "adobe after effects"},
    "illustrator": {"illustrator", "adobe illustrator"},
    "spotify": {"spotify"},
    "discord": {"discord"},
    "steam": {"steam"},
    "vlc": {"vlc", "vlc media player"},
    "obs": {"obs", "obs studio"},
    "notion": {"notion"},
}


def _canonical_name(name: str) -> str:
    normalized = " ".join(str(name or "").strip().lower().split())
    return _APP_ALIASES.get(normalized, normalized)


def _normalize_label(value: str) -> str:
    value = re.sub(r"[^a-z0-9]+", " ", str(value or "").lower())
    return " ".join(value.split())


def _is_protected_target(target: str, canonical: str) -> bool:
    name = Path(target).name.lower()
    suffix = Path(target).suffix.lower()
    if suffix == ".lnk":
        label = _normalize_label(Path(target).stem)
        parent_labels = {_normalize_label(part) for part in Path(target).parts}
        protected = label in _PROTECTED_START_MENU_LABELS or (
            "administrative tools" in parent_labels
        )
        return protected and canonical not in _ALLOWED_SYSTEM_COMMANDS
    return name in _PROTECTED_EXECUTABLES and canonical not in _ALLOWED_SYSTEM_COMMANDS


def _existing_file(path: str) -> Optional[str]:
    if not path:
        return None
    expanded = os.path.expandvars(os.path.expanduser(path.strip().strip('"')))
    if not expanded:
        return None
    if "*" in expanded or "?" in expanded:
        return _resolve_glob_path(expanded)
    return expanded if Path(expanded).is_file() else None


def _newest_existing(paths: list[str]) -> Optional[str]:
    existing = [p for p in paths if p and Path(p).is_file()]
    if not existing:
        return None
    return max(existing, key=lambda p: Path(p).stat().st_mtime)


def _resolve_glob_path(name_or_pattern: str) -> Optional[str]:
    if any(ch in name_or_pattern for ch in "*?"):
        patterns = [name_or_pattern]
    else:
        patterns = _GLOB_FALLBACKS.get(_canonical_name(name_or_pattern), [])

    matches = []
    for pattern in patterns:
        pattern = os.path.expandvars(pattern)
        matches.extend(glob.glob(pattern))

    return _newest_existing(matches)


def _resolve_start_menu_shortcut(name: str) -> Optional[str]:
    canonical = _canonical_name(name)
    program_dirs = [
        os.environ.get("ProgramData", r"C:\ProgramData"),
        os.environ.get("APPDATA", ""),
    ]
    shortcut_roots = [
        Path(base) / "Microsoft" / "Windows" / "Start Menu" / "Programs"
        for base in program_dirs
        if base
    ]

    terms = {
        _normalize_label(term)
        for term in _START_MENU_TERMS.get(canonical, {canonical})
        if _normalize_label(term)
    }

    matches = []
    for root in shortcut_roots:
        if not root.exists():
            continue
        for shortcut in root.rglob("*.lnk"):
            shortcut_name = _normalize_label(shortcut.stem)
            if not _start_menu_match_is_safe(shortcut_name, terms):
                continue
            if _is_protected_target(str(shortcut), canonical):
                logger.warning(
                    f"Refusing protected Start Menu target for '{name}': {shortcut}"
                )
                continue
            if shortcut_name in terms or any(shortcut_name.startswith(f"{term} ") for term in terms):
                matches.append(str(shortcut))

    return _newest_existing(matches)


def _start_menu_match_is_safe(shortcut_name: str, terms: set[str]) -> bool:
    """
    Start Menu search is intentionally strict.

    The old resolver used substring containment, so "edito" matched
    "Registry Editor". A shortcut now matches only an exact term or a known
    versioned app label such as "adobe photoshop 2025".
    """
    if not shortcut_name or not terms:
        return False
    if shortcut_name in terms:
        return True
    return any(shortcut_name.startswith(f"{term} ") for term in terms)


def _resolve_command(name: str) -> Optional[str]:
    command = _COMMAND_FALLBACKS.get(_canonical_name(name))
    if not command:
        return None
    return shutil.which(command) or command


def _launch_target(target: str, name: str) -> bool:
    flags = subprocess.CREATE_NO_WINDOW if hasattr(subprocess, "CREATE_NO_WINDOW") else 0
    canonical = _canonical_name(name)
    if _is_protected_target(target, canonical):
        logger.warning(f"Refusing protected launch target for {canonical}: {target}")
        return False
    try:
        if target.lower().endswith(".lnk"):
            os.startfile(target)  # type: ignore[attr-defined]
            logger.info(f"Launch requested: {name} ({target})")
        else:
            subprocess.Popen(
                [target],
                shell=False,
                cwd=str(Path(target).parent) if Path(target).is_file() else None,
                creationflags=flags,
            )
            logger.info(f"Opened: {name} ({target})")
        return True
    except Exception as exc:
        logger.error(f"Failed to open {name} ({target}): {exc}")
        return False


def open_app(name: str, config) -> bool:
    """
    Open an application by name.
    Priority: exact config path -> known install globs -> Start Menu shortcut
    -> vetted Windows command fallback.
    """
    canonical = _canonical_name(name)
    if not canonical:
        logger.warning("Could not open app: empty name")
        return False

    config_path = config.get_app_path(canonical)
    resolved = _existing_file(config_path)
    if resolved and _launch_target(resolved, canonical):
        return True

    if config_path:
        logger.warning(f"Configured path for {canonical} is missing: {config_path}")

    resolved = _resolve_glob_path(canonical)
    if resolved and _launch_target(resolved, canonical):
        _persist_app_path(config, canonical, resolved)
        return True

    resolved = _resolve_start_menu_shortcut(canonical)
    if resolved and _launch_target(resolved, canonical):
        _persist_app_path(config, canonical, resolved)
        return True

    resolved = _resolve_command(canonical)
    if resolved and _launch_target(resolved, canonical):
        return True

    logger.warning(f"Could not find app: {canonical}")
    return False


def _persist_app_path(config, canonical: str, resolved: str):
    if not resolved or resolved.lower().endswith(".lnk"):
        return
    try:
        current = config.get_app_path(canonical)
        if os.path.normcase(os.path.expandvars(current or "")) != os.path.normcase(resolved):
            config.set(f"app_paths.{canonical}", resolved)
            logger.info(f"Persisted app path: {canonical} -> {resolved}")
    except Exception as exc:
        logger.warning(f"Could not persist app path for {canonical}: {exc}")


def close_app(name: str) -> bool:
    """
    Close all running processes matching the name.

    BUG-10 fix: the original loop iterated process_iter() while also calling
    proc.terminate() inside the same loop.  On some Windows versions this
    caused the same process to appear multiple times in the iteration (once
    alive, once as a zombie), producing repeated "Closed: Discord.exe" log
    entries.  Fix: collect PIDs first, then terminate in a second pass,
    deduplicating by PID.
    """
    name = _canonical_name(name)
    if not name:
        return False

    protected = {
        "jarvis", "assistant", "system", "windows", "explorer",
        "desktop", "python", "powershell", "cmd",
        "command prompt", "windows powershell", "registry editor", "regedit",
        "mmc", "services", "device manager", "task manager", "taskmgr",
        "control panel", "event viewer", "task scheduler",
    }
    if name in protected:
        logger.warning(f"Refusing unsafe close request for: {name}")
        return False

    name_map = {
        'chrome':     ['chrome.exe'],
        'firefox':    ['firefox.exe'],
        'vscode':     ['code.exe'],
        'vs code':    ['code.exe'],
        'photoshop':  ['photoshop.exe'],
        'premiere':   ['adobe premiere pro.exe'],
        'steam':      ['steam.exe'],
        'epic':       ['epicgameslauncher.exe'],
        'spotify':    ['spotify.exe'],
        'discord':    ['discord.exe'],
        'vlc':        ['vlc.exe'],
        'obs':        ['obs64.exe', 'obs.exe'],
        'notepad':    ['notepad.exe'],
        'notion':     ['notion.exe'],
    }

    targets = {t.lower() for t in name_map.get(name, [name, name + '.exe'])}

    to_kill: dict = {}
    for proc in psutil.process_iter(['name', 'pid']):
        try:
            pname = (proc.info['name'] or '').lower()
            pid   = proc.info['pid']
            if pid not in to_kill and pname in targets:
                to_kill[pid] = proc.info['name']
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            pass

    killed = []
    for pid, proc_name in to_kill.items():
        try:
            p = psutil.Process(pid)
            p.terminate()
            try:
                p.wait(timeout=5)
            except psutil.TimeoutExpired:
                logger.warning(f"Process did not exit after terminate: {proc_name} (pid={pid})")
                continue
            killed.append(proc_name)
            logger.info(f"Closed: {proc_name} (pid={pid})")
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            pass

    return len(killed) > 0


def get_running_apps() -> list:
    """Return list of running process names (deduplicated)."""
    seen = set()
    result = []
    for proc in psutil.process_iter(['name', 'pid', 'memory_percent']):
        try:
            name = proc.info['name']
            if name and name not in seen:
                seen.add(name)
                result.append({
                    'name': name,
                    'pid': proc.info['pid'],
                    'memory': round(proc.info['memory_percent'] or 0, 1)
                })
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            pass
    return sorted(result, key=lambda x: x['memory'], reverse=True)


def is_running(name: str) -> bool:
    name_lower = name.lower()
    for proc in psutil.process_iter(['name']):
        try:
            if name_lower in proc.info['name'].lower():
                return True
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            pass
    return False
