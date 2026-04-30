"""
Windows system control — shutdown, sleep, lock, clipboard, screenshots, DND.
"""

import os
import subprocess
import logging
import threading
from datetime import datetime
from pathlib import Path
from typing import Optional

logger = logging.getLogger('JARVIS.SystemControl')

_shutdown_timer: Optional[threading.Timer] = None


def shutdown(delay: int = 0):
    global _shutdown_timer
    if delay > 0:
        _shutdown_timer = threading.Timer(delay, _do_shutdown)
        _shutdown_timer.start()
    else:
        _do_shutdown()


def cancel_shutdown():
    global _shutdown_timer
    if _shutdown_timer:
        _shutdown_timer.cancel()
        _shutdown_timer = None
        subprocess.run(['shutdown', '/a'], capture_output=True)
        return True
    subprocess.run(['shutdown', '/a'], capture_output=True)
    return True


def _do_shutdown():
    os.system("shutdown /s /t 0")


def restart(delay: int = 0):
    if delay > 0:
        threading.Timer(delay, lambda: os.system("shutdown /r /t 0")).start()
    else:
        os.system("shutdown /r /t 0")


def sleep():
    subprocess.Popen(['rundll32.exe', 'powrprof.dll,SetSuspendState', '0', '1', '0'])


def lock_screen():
    import ctypes
    ctypes.windll.user32.LockWorkStation()


def take_screenshot() -> str:
    """Take screenshot and save to Desktop/JARVIS_Screenshots/"""
    try:
        from PIL import ImageGrab
        desktop = Path.home() / "Desktop" / "JARVIS_Screenshots"
        desktop.mkdir(parents=True, exist_ok=True)
        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        path = desktop / f"screenshot_{ts}.png"
        img = ImageGrab.grab()
        img.save(str(path))
        logger.info(f"Screenshot saved: {path}")
        return str(path)
    except Exception as e:
        logger.error(f"Screenshot error: {e}")
        # Fallback: Windows Snipping Tool
        subprocess.Popen(['snippingtool'])
        return "Snipping tool opened"


def empty_recycle_bin():
    try:
        import winshell
        winshell.recycle_bin().empty(confirm=False, show_progress=False, sound=False)
    except ImportError:
        # Fallback via PowerShell
        subprocess.run(
            ['powershell', '-c', 'Clear-RecycleBin -Force -ErrorAction SilentlyContinue'],
            capture_output=True
        )


def copy_to_clipboard(text: str):
    try:
        import win32clipboard
        win32clipboard.OpenClipboard()
        win32clipboard.EmptyClipboard()
        win32clipboard.SetClipboardText(text, win32clipboard.CF_UNICODETEXT)
        win32clipboard.CloseClipboard()
    except ImportError:
        subprocess.run(['clip'], input=text.encode('utf-16-le'), check=True)


def read_clipboard() -> str:
    try:
        import win32clipboard
        win32clipboard.OpenClipboard()
        data = win32clipboard.GetClipboardData(win32clipboard.CF_UNICODETEXT)
        win32clipboard.CloseClipboard()
        return data
    except Exception:
        result = subprocess.run(
            ['powershell', '-c', 'Get-Clipboard'],
            capture_output=True, text=True
        )
        return result.stdout.strip()


def set_dnd(enabled: bool):
    """Enable/disable Do Not Disturb via Windows Focus Assist registry."""
    try:
        import winreg
        key = winreg.OpenKey(
            winreg.HKEY_CURRENT_USER,
            r"SOFTWARE\Microsoft\Windows\CurrentVersion\Notifications\Settings",
            0, winreg.KEY_SET_VALUE
        )
        # NOC_GLOBAL_SETTING_TOASTS_ENABLED: 0=DND on, 1=DND off
        winreg.SetValueEx(key, "NOC_GLOBAL_SETTING_TOASTS_ENABLED",
                          0, winreg.REG_DWORD, 0 if enabled else 1)
        winreg.CloseKey(key)
        logger.info(f"DND {'enabled' if enabled else 'disabled'}")
    except Exception as e:
        logger.warning(f"Could not set DND: {e}")


def boost_performance():
    """Set power plan to High Performance."""
    try:
        # High Performance GUID
        subprocess.run(
            ['powercfg', '/s', '8c5e7fda-e8bf-4a96-9a85-a6e23a8c635c'],
            capture_output=True
        )
        logger.info("Power plan: High Performance")
    except Exception as e:
        logger.warning(f"Could not set power plan: {e}")


def reduce_brightness(level: int = 30):
    """Reduce screen brightness (requires WMI)."""
    try:
        subprocess.run(
            ['powershell', '-c',
             f'(Get-WmiObject -Namespace root/WMI -Class WmiMonitorBrightnessMethods).WmiSetBrightness(1,{level})'],
            capture_output=True
        )
    except Exception as e:
        logger.warning(f"Could not set brightness: {e}")


def get_system_stats() -> dict:
    import psutil
    cpu = psutil.cpu_percent(interval=0.5)
    ram = psutil.virtual_memory()
    disk = psutil.disk_usage('/')
    return {
        'cpu_percent': round(cpu, 1),
        'ram_percent': round(ram.percent, 1),
        'ram_used_gb': round(ram.used / 1e9, 1),
        'ram_total_gb': round(ram.total / 1e9, 1),
        'disk_percent': round(disk.percent, 1),
        'disk_free_gb': round(disk.free / 1e9, 1),
    }
