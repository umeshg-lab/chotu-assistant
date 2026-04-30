"""
Media control — Windows master volume and playback controls.
"""

import logging
import subprocess

logger = logging.getLogger("JARVIS.MediaControl")

VOLUME_STEP = 10


class AudioControlError(RuntimeError):
    """Raised when Windows audio state could not be read or changed."""


# ── Core Audio helpers ───────────────────────────────────────────────────────

def _get_volume() -> int:
    """Return current master volume as 0-100, or raise AudioControlError."""
    errors = []

    for backend in (_comtypes_get_volume, _pycaw_get_volume, _powershell_get_volume):
        try:
            return _clamp_percent(round(float(backend())))
        except Exception as exc:
            errors.append(f"{backend.__name__}: {exc}")

    raise AudioControlError("Unable to read Windows master volume: " + " | ".join(errors))


def _set_volume_scalar(level: int) -> int:
    """
    Set master volume and verify the result. Returns the verified level.
    Raises AudioControlError if the state could not be changed.
    """
    target = _clamp_percent(level)
    errors = []

    for setter in (_comtypes_set_volume, _pycaw_set_volume, _powershell_set_volume):
        try:
            setter(target)
            verified = _get_volume()
            if abs(verified - target) <= 2:
                return verified
            errors.append(
                f"{setter.__name__}: verification mismatch "
                f"(target={target}, actual={verified})"
            )
        except Exception as exc:
            errors.append(f"{setter.__name__}: {exc}")

    raise AudioControlError("Unable to set Windows master volume: " + " | ".join(errors))


def _get_mute() -> bool:
    errors = []
    for backend in (_comtypes_get_mute, _pycaw_get_mute, _powershell_get_mute):
        try:
            return bool(backend())
        except Exception as exc:
            errors.append(f"{backend.__name__}: {exc}")
    raise AudioControlError("Unable to read Windows mute state: " + " | ".join(errors))


def _set_mute(muted: bool) -> bool:
    target = bool(muted)
    errors = []

    for setter in (_comtypes_set_mute, _pycaw_set_mute, _powershell_set_mute):
        try:
            setter(target)
            verified = _get_mute()
            if verified == target:
                return verified
            errors.append(
                f"{setter.__name__}: verification mismatch "
                f"(target={target}, actual={verified})"
            )
        except Exception as exc:
            errors.append(f"{setter.__name__}: {exc}")

    raise AudioControlError("Unable to set Windows mute state: " + " | ".join(errors))


def _clamp_percent(level: int) -> int:
    return max(0, min(100, int(level)))


def _comtypes_endpoint_volume():
    """
    Open the default render endpoint through Core Audio.

    This avoids the old PowerShell null-object failure by validating every COM
    object returned by endpoint discovery and activation before use.
    """
    import ctypes
    import comtypes
    from comtypes import CLSCTX_ALL, COMMETHOD, GUID, HRESULT, IUnknown
    from comtypes.client import CreateObject

    class IAudioEndpointVolume(IUnknown):
        _iid_ = GUID("{5CDF2C82-841E-4546-9722-0CF74078229A}")
        _methods_ = [
            COMMETHOD([], HRESULT, "RegisterControlChangeNotify",
                      (["in"], ctypes.c_void_p, "pNotify")),
            COMMETHOD([], HRESULT, "UnregisterControlChangeNotify",
                      (["in"], ctypes.c_void_p, "pNotify")),
            COMMETHOD([], HRESULT, "GetChannelCount",
                      (["out"], ctypes.POINTER(ctypes.c_uint), "pnChannelCount")),
            COMMETHOD([], HRESULT, "SetMasterVolumeLevel",
                      (["in"], ctypes.c_float, "fLevelDB"),
                      (["in"], ctypes.POINTER(GUID), "pguidEventContext")),
            COMMETHOD([], HRESULT, "SetMasterVolumeLevelScalar",
                      (["in"], ctypes.c_float, "fLevel"),
                      (["in"], ctypes.POINTER(GUID), "pguidEventContext")),
            COMMETHOD([], HRESULT, "GetMasterVolumeLevel",
                      (["out"], ctypes.POINTER(ctypes.c_float), "pfLevelDB")),
            COMMETHOD([], HRESULT, "GetMasterVolumeLevelScalar",
                      (["out"], ctypes.POINTER(ctypes.c_float), "pfLevel")),
            COMMETHOD([], HRESULT, "SetChannelVolumeLevel",
                      (["in"], ctypes.c_uint, "nChannel"),
                      (["in"], ctypes.c_float, "fLevelDB"),
                      (["in"], ctypes.POINTER(GUID), "pguidEventContext")),
            COMMETHOD([], HRESULT, "SetChannelVolumeLevelScalar",
                      (["in"], ctypes.c_uint, "nChannel"),
                      (["in"], ctypes.c_float, "fLevel"),
                      (["in"], ctypes.POINTER(GUID), "pguidEventContext")),
            COMMETHOD([], HRESULT, "GetChannelVolumeLevel",
                      (["in"], ctypes.c_uint, "nChannel"),
                      (["out"], ctypes.POINTER(ctypes.c_float), "pfLevelDB")),
            COMMETHOD([], HRESULT, "GetChannelVolumeLevelScalar",
                      (["in"], ctypes.c_uint, "nChannel"),
                      (["out"], ctypes.POINTER(ctypes.c_float), "pfLevel")),
            COMMETHOD([], HRESULT, "SetMute",
                      (["in"], ctypes.c_bool, "bMute"),
                      (["in"], ctypes.POINTER(GUID), "pguidEventContext")),
            COMMETHOD([], HRESULT, "GetMute",
                      (["out"], ctypes.POINTER(ctypes.c_bool), "pbMute")),
        ]

    class IMMDevice(IUnknown):
        _iid_ = GUID("{D666063F-1587-4E43-81F1-B948E807363F}")
        _methods_ = [
            COMMETHOD([], HRESULT, "Activate",
                      (["in"], ctypes.POINTER(GUID), "iid"),
                      (["in"], ctypes.c_uint, "dwClsCtx"),
                      (["in"], ctypes.c_void_p, "pActivationParams"),
                      (["out"], ctypes.POINTER(ctypes.POINTER(IUnknown)), "ppInterface")),
            COMMETHOD([], HRESULT, "OpenPropertyStore",
                      (["in"], ctypes.c_uint, "stgmAccess"),
                      (["out"], ctypes.POINTER(ctypes.POINTER(IUnknown)), "ppProperties")),
            COMMETHOD([], HRESULT, "GetId",
                      (["out"], ctypes.POINTER(ctypes.c_wchar_p), "ppstrId")),
            COMMETHOD([], HRESULT, "GetState",
                      (["out"], ctypes.POINTER(ctypes.c_uint), "pdwState")),
        ]

    class IMMDeviceEnumerator(IUnknown):
        _iid_ = GUID("{A95664D2-9614-4F35-A746-DE8DB63617E6}")
        _methods_ = [
            COMMETHOD([], HRESULT, "EnumAudioEndpoints",
                      (["in"], ctypes.c_int, "dataFlow"),
                      (["in"], ctypes.c_uint, "dwStateMask"),
                      (["out"], ctypes.POINTER(ctypes.POINTER(IUnknown)), "ppDevices")),
            COMMETHOD([], HRESULT, "GetDefaultAudioEndpoint",
                      (["in"], ctypes.c_int, "dataFlow"),
                      (["in"], ctypes.c_int, "role"),
                      (["out"], ctypes.POINTER(ctypes.POINTER(IMMDevice)), "ppEndpoint")),
            COMMETHOD([], HRESULT, "GetDevice",
                      (["in"], ctypes.c_wchar_p, "pwstrId"),
                      (["out"], ctypes.POINTER(ctypes.POINTER(IMMDevice)), "ppDevice")),
            COMMETHOD([], HRESULT, "RegisterEndpointNotificationCallback",
                      (["in"], ctypes.c_void_p, "pClient")),
            COMMETHOD([], HRESULT, "UnregisterEndpointNotificationCallback",
                      (["in"], ctypes.c_void_p, "pClient")),
        ]

    comtypes.CoInitialize()
    try:
        enumerator = CreateObject(
            GUID("{BCDE0395-E52F-467C-8E3D-C4579291692E}"),
            interface=IMMDeviceEnumerator,
        )
        if not enumerator:
            raise AudioControlError("Core Audio device enumerator is null")

        endpoint = enumerator.GetDefaultAudioEndpoint(0, 1)
        if not endpoint:
            raise AudioControlError("Default render audio endpoint is null")

        unknown = endpoint.Activate(
            ctypes.byref(IAudioEndpointVolume._iid_),
            CLSCTX_ALL,
            None,
        )
        if not unknown:
            raise AudioControlError("Audio endpoint activation returned null")

        volume = unknown.QueryInterface(IAudioEndpointVolume)
        if not volume:
            raise AudioControlError("IAudioEndpointVolume query returned null")

        return volume
    except Exception:
        try:
            comtypes.CoUninitialize()
        except Exception:
            pass
        raise


def _comtypes_get_volume() -> int:
    import comtypes
    volume = _comtypes_endpoint_volume()
    try:
        return int(round(float(volume.GetMasterVolumeLevelScalar()) * 100))
    finally:
        comtypes.CoUninitialize()


def _comtypes_set_volume(level: int):
    import comtypes
    volume = _comtypes_endpoint_volume()
    try:
        volume.SetMasterVolumeLevelScalar(_clamp_percent(level) / 100.0, None)
    finally:
        comtypes.CoUninitialize()


def _comtypes_get_mute() -> bool:
    import comtypes
    volume = _comtypes_endpoint_volume()
    try:
        return bool(volume.GetMute())
    finally:
        comtypes.CoUninitialize()


def _comtypes_set_mute(muted: bool):
    import comtypes
    volume = _comtypes_endpoint_volume()
    try:
        volume.SetMute(bool(muted), None)
    finally:
        comtypes.CoUninitialize()


def _pycaw_get_endpoint():
    try:
        import pythoncom
        pythoncom.CoInitialize()
    except ImportError:
        pythoncom = None

    try:
        from ctypes import POINTER, cast
        from comtypes import CLSCTX_ALL
        from pycaw.pycaw import AudioUtilities, IAudioEndpointVolume

        speakers = AudioUtilities.GetSpeakers()
        if not speakers:
            raise AudioControlError("pycaw returned no speaker endpoint")

        endpoint_volume = getattr(speakers, "EndpointVolume", None)
        if endpoint_volume:
            return endpoint_volume, pythoncom

        activate = getattr(speakers, "Activate", None)
        if not callable(activate) and hasattr(speakers, "_dev"):
            activate = getattr(speakers._dev, "Activate", None)
        if not callable(activate):
            raise AudioControlError("pycaw speaker endpoint has no Activate method")

        interface = activate(IAudioEndpointVolume._iid_, CLSCTX_ALL, None)
        if not interface:
            raise AudioControlError("pycaw endpoint activation returned null")
        return cast(interface, POINTER(IAudioEndpointVolume)), pythoncom
    except Exception:
        if pythoncom:
            try:
                pythoncom.CoUninitialize()
            except Exception:
                pass
        raise


def _pycaw_get_volume() -> int:
    volume, pythoncom = _pycaw_get_endpoint()
    try:
        return int(round(float(volume.GetMasterVolumeLevelScalar()) * 100))
    finally:
        if pythoncom:
            pythoncom.CoUninitialize()


def _pycaw_set_volume(level: int):
    volume, pythoncom = _pycaw_get_endpoint()
    try:
        volume.SetMasterVolumeLevelScalar(_clamp_percent(level) / 100.0, None)
    finally:
        if pythoncom:
            pythoncom.CoUninitialize()


def _pycaw_get_mute() -> bool:
    volume, pythoncom = _pycaw_get_endpoint()
    try:
        return bool(volume.GetMute())
    finally:
        if pythoncom:
            pythoncom.CoUninitialize()


def _pycaw_set_mute(muted: bool):
    volume, pythoncom = _pycaw_get_endpoint()
    try:
        volume.SetMute(bool(muted), None)
    finally:
        if pythoncom:
            pythoncom.CoUninitialize()


def _powershell_audio(action: str, value=None) -> str:
    if action == "get_volume":
        action_script = "Write-Output ([Audio.AudioEndpoint]::GetVolume())"
    elif action == "set_volume":
        action_script = f"[Audio.AudioEndpoint]::SetVolume({float(value) / 100.0:.4f})"
    elif action == "get_mute":
        action_script = "Write-Output ([Audio.AudioEndpoint]::GetMute())"
    elif action == "set_mute":
        action_script = f"[Audio.AudioEndpoint]::SetMute(${str(bool(value)).lower()})"
    else:
        raise ValueError(f"Unknown audio action: {action}")

    ps_script = f"""
$ErrorActionPreference = 'Stop'
$code = @'
using System;
using System.Runtime.InteropServices;

namespace Audio {{
    [Guid("5CDF2C82-841E-4546-9722-0CF74078229A")]
    [InterfaceType(ComInterfaceType.InterfaceIsIUnknown)]
    public interface IAudioEndpointVolume {{
        int RegisterControlChangeNotify(IntPtr pNotify);
        int UnregisterControlChangeNotify(IntPtr pNotify);
        int GetChannelCount(out uint pnChannelCount);
        int SetMasterVolumeLevel(float fLevelDB, Guid pguidEventContext);
        int SetMasterVolumeLevelScalar(float fLevel, Guid pguidEventContext);
        int GetMasterVolumeLevel(out float pfLevelDB);
        int GetMasterVolumeLevelScalar(out float pfLevel);
        int SetChannelVolumeLevel(uint nChannel, float fLevelDB, Guid pguidEventContext);
        int SetChannelVolumeLevelScalar(uint nChannel, float fLevel, Guid pguidEventContext);
        int GetChannelVolumeLevel(uint nChannel, out float pfLevelDB);
        int GetChannelVolumeLevelScalar(uint nChannel, out float pfLevel);
        int SetMute([MarshalAs(UnmanagedType.Bool)] bool bMute, Guid pguidEventContext);
        int GetMute(out bool pbMute);
    }}

    [Guid("D666063F-1587-4E43-81F1-B948E807363F")]
    [InterfaceType(ComInterfaceType.InterfaceIsIUnknown)]
    public interface IMMDevice {{
        int Activate(ref Guid iid, uint dwClsCtx, IntPtr pActivationParams,
                     [MarshalAs(UnmanagedType.Interface)] out object ppInterface);
        int OpenPropertyStore(uint stgmAccess, out object ppProperties);
        int GetId(out IntPtr ppstrId);
        int GetState(out uint pdwState);
    }}

    [Guid("A95664D2-9614-4F35-A746-DE8DB63617E6")]
    [InterfaceType(ComInterfaceType.InterfaceIsIUnknown)]
    public interface IMMDeviceEnumerator {{
        int EnumAudioEndpoints(int dataFlow, uint dwStateMask, out object ppDevices);
        int GetDefaultAudioEndpoint(int dataFlow, int role, out IMMDevice ppDevice);
        int GetDevice(string pwstrId, out IMMDevice ppDevice);
        int RegisterEndpointNotificationCallback(IntPtr pClient);
        int UnregisterEndpointNotificationCallback(IntPtr pClient);
    }}

    [ComImport]
    [Guid("BCDE0395-E52F-467C-8E3D-C4579291692E")]
    public class MMDeviceEnumeratorComObject {{ }}

    public static class AudioEndpoint {{
        private static IAudioEndpointVolume Volume() {{
            var enumerator = new MMDeviceEnumeratorComObject() as IMMDeviceEnumerator;
            if (enumerator == null) {{
                throw new InvalidOperationException("Core Audio enumerator is null.");
            }}

            IMMDevice device;
            int hr = enumerator.GetDefaultAudioEndpoint(0, 1, out device);
            if (hr != 0 || device == null) {{
                throw new InvalidOperationException("Default render endpoint is null. HRESULT=0x" + hr.ToString("X"));
            }}

            Guid iid = typeof(IAudioEndpointVolume).GUID;
            object endpoint;
            hr = device.Activate(ref iid, 23, IntPtr.Zero, out endpoint);
            if (hr != 0 || endpoint == null) {{
                throw new InvalidOperationException("Endpoint activation failed. HRESULT=0x" + hr.ToString("X"));
            }}

            var volume = endpoint as IAudioEndpointVolume;
            if (volume == null) {{
                throw new InvalidOperationException("IAudioEndpointVolume cast returned null.");
            }}
            return volume;
        }}

        public static float GetVolume() {{
            float value;
            int hr = Volume().GetMasterVolumeLevelScalar(out value);
            if (hr != 0) throw new InvalidOperationException("GetVolume failed. HRESULT=0x" + hr.ToString("X"));
            return value;
        }}

        public static void SetVolume(float value) {{
            value = Math.Max(0.0f, Math.Min(1.0f, value));
            int hr = Volume().SetMasterVolumeLevelScalar(value, Guid.Empty);
            if (hr != 0) throw new InvalidOperationException("SetVolume failed. HRESULT=0x" + hr.ToString("X"));
        }}

        public static bool GetMute() {{
            bool muted;
            int hr = Volume().GetMute(out muted);
            if (hr != 0) throw new InvalidOperationException("GetMute failed. HRESULT=0x" + hr.ToString("X"));
            return muted;
        }}

        public static void SetMute(bool muted) {{
            int hr = Volume().SetMute(muted, Guid.Empty);
            if (hr != 0) throw new InvalidOperationException("SetMute failed. HRESULT=0x" + hr.ToString("X"));
        }}
    }}
}}
'@
Add-Type -TypeDefinition $code -ErrorAction Stop
{action_script}
"""
    result = subprocess.run(
        ["powershell", "-NoProfile", "-NonInteractive", "-Command", ps_script],
        capture_output=True,
        text=True,
        timeout=10,
    )
    if result.returncode != 0:
        raise AudioControlError(
            (result.stderr or result.stdout or "PowerShell Core Audio command failed").strip()
        )
    return result.stdout.strip()


def _powershell_get_volume() -> int:
    return int(round(float(_powershell_audio("get_volume")) * 100))


def _powershell_set_volume(level: int):
    _powershell_audio("set_volume", _clamp_percent(level))


def _powershell_get_mute() -> bool:
    return _powershell_audio("get_mute").strip().lower() == "true"


def _powershell_set_mute(muted: bool):
    _powershell_audio("set_mute", bool(muted))


# ── Public volume API ────────────────────────────────────────────────────────

def volume_up() -> int:
    current = _get_volume()
    target = min(100, current + VOLUME_STEP)
    verified = _set_volume_scalar(target)
    logger.info(f"Volume up: {current}% -> {verified}%")
    return verified


def volume_down() -> int:
    current = _get_volume()
    target = max(0, current - VOLUME_STEP)
    verified = _set_volume_scalar(target)
    logger.info(f"Volume down: {current}% -> {verified}%")
    return verified


def set_volume(level: int) -> int:
    verified = _set_volume_scalar(level)
    logger.info(f"Volume set: {verified}%")
    return verified


def toggle_mute() -> bool:
    current = _get_mute()
    verified = _set_mute(not current)
    logger.info(f"Mute toggled -> {'muted' if verified else 'unmuted'}")
    return verified


def mute() -> bool:
    verified = _set_mute(True)
    logger.info("Muted")
    return verified


def unmute() -> bool:
    verified = _set_mute(False)
    logger.info("Unmuted")
    return verified


# ── Playback controls ────────────────────────────────────────────────────────

def play():
    if _send_media_key("play_pause"):
        logger.info("Media: play/resume")


def pause():
    if _send_media_key("play_pause"):
        logger.info("Media: pause")


def stop():
    if _send_media_key("stop"):
        logger.info("Media: stop")


def next_track():
    if _send_media_key("next_track"):
        logger.info("Media: next track")


def prev_track():
    if _send_media_key("prev_track"):
        logger.info("Media: previous track")


def pause_all():
    _send_media_key("play_pause")


def _send_media_key(key: str) -> bool:
    """Send a Windows virtual media key. Returns True only if dispatch succeeded."""
    vk_codes = {
        "play_pause": 0xB3,
        "stop":       0xB2,
        "next_track": 0xB0,
        "prev_track": 0xB1,
        "mute":       0xAD,
        "vol_up":     0xAF,
        "vol_down":   0xAE,
    }
    try:
        import ctypes
        vk = vk_codes.get(key)
        if not vk:
            return False
        ctypes.windll.user32.keybd_event(vk, 0, 0, 0)
        ctypes.windll.user32.keybd_event(vk, 0, 0x0002, 0)
        return True
    except Exception:
        try:
            import pyautogui
            key_map = {
                "play_pause": "playpause",
                "next_track": "nexttrack",
                "prev_track": "prevtrack",
                "stop":       "stop",
                "mute":       "volumemute",
                "vol_up":     "volumeup",
                "vol_down":   "volumedown",
            }
            pyautogui.press(key_map.get(key, key))
            return True
        except Exception as exc:
            logger.error(f"Media key failed: {exc}")
            return False
