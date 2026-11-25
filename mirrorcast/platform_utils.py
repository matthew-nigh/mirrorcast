"""Small Windows helpers: DPI awareness, monitor list, log folder, local IP."""

from __future__ import annotations

import os
import socket
import subprocess
import sys
from dataclasses import dataclass

IS_WINDOWS = os.name == "nt"
# Stops ffmpeg from flashing a console window when started from the GUI.
CREATE_NO_WINDOW = 0x08000000 if IS_WINDOWS else 0


@dataclass(frozen=True)
class Monitor:
    index: int
    x: int
    y: int
    width: int
    height: int
    primary: bool = False

    @property
    def label(self) -> str:
        tag = " (main)" if self.primary else ""
        return f"Screen {self.index + 1}: {self.width}x{self.height}{tag}"


def enable_dpi_awareness() -> None:
    """Make Windows report real pixel sizes (needed for correct screen capture)."""
    if not IS_WINDOWS:
        return
    import ctypes

    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(2)  # per-monitor aware
        return
    except Exception:
        pass
    try:
        ctypes.windll.user32.SetProcessDPIAware()
    except Exception:
        pass


def list_monitors() -> list[Monitor]:
    """Return the connected screens, main screen first."""
    if not IS_WINDOWS:
        return [Monitor(0, 0, 0, 1920, 1080, True)]

    import ctypes
    from ctypes import wintypes

    user32 = ctypes.windll.user32

    class MONITORINFOEXW(ctypes.Structure):
        _fields_ = [
            ("cbSize", wintypes.DWORD),
            ("rcMonitor", wintypes.RECT),
            ("rcWork", wintypes.RECT),
            ("dwFlags", wintypes.DWORD),
            ("szDevice", wintypes.WCHAR * 32),
        ]

    found: list[tuple[int, int, int, int, bool]] = []
    proc_type = ctypes.WINFUNCTYPE(
        ctypes.c_int, wintypes.HMONITOR, wintypes.HDC,
        ctypes.POINTER(wintypes.RECT), wintypes.LPARAM,
    )

    def _callback(hmon, _hdc, _rect, _lparam):
        info = MONITORINFOEXW()
        info.cbSize = ctypes.sizeof(MONITORINFOEXW)
        if user32.GetMonitorInfoW(hmon, ctypes.byref(info)):
            r = info.rcMonitor
            found.append((r.left, r.top, r.right - r.left, r.bottom - r.top,
                          bool(info.dwFlags & 1)))
        return 1

    callback = proc_type(_callback)
    user32.EnumDisplayMonitors(None, None, callback, 0)
    if not found:
        w, h = user32.GetSystemMetrics(0), user32.GetSystemMetrics(1)
        found = [(0, 0, w, h, True)]
    found.sort(key=lambda m: (not m[4], m[0], m[1]))
    return [Monitor(i, x, y, w, h, p) for i, (x, y, w, h, p) in enumerate(found)]


def app_data_dir() -> str:
    base = os.environ.get("LOCALAPPDATA") or os.path.expanduser("~")
    path = os.path.join(base, "MirrorCast")
    os.makedirs(path, exist_ok=True)
    return path


def local_ip_towards(remote_host: str | None) -> str:
    """The PC's own LAN address on the route to the Chromecast."""
    target = remote_host or "8.8.8.8"
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect((target, 9))  # no packet is sent for UDP connect
        return s.getsockname()[0]
    except OSError:
        return socket.gethostbyname(socket.gethostname())
    finally:
        s.close()


FIREWALL_RULE_NAME = "MirrorCast"  # no spaces: keeps the netsh quoting simple


def firewall_rule_commands(port: int) -> list[list[str]]:
    """netsh commands that let devices on the local network fetch the stream.

    Only the stream port, only from the local subnet (the Chromecast), on every
    network profile, because home Wi-Fi is often marked "Public" in Windows.
    Nothing listens on the port unless MirrorCast is mirroring.
    """
    base = ["netsh", "advfirewall", "firewall"]
    return [
        base + ["delete", "rule", f"name={FIREWALL_RULE_NAME}"],
        base + ["add", "rule", f"name={FIREWALL_RULE_NAME}", "dir=in", "action=allow",
                "protocol=TCP", f"localport={port}", "remoteip=localsubnet",
                "profile=any"],
    ]


def allow_through_firewall(port: int) -> bool:
    """Add the firewall rule. Windows shows its usual "allow changes?" prompt.
    Returns True if the rule was added."""
    if not IS_WINDOWS:
        return False
    # One elevated cmd.exe runs both netsh commands, so there is only one prompt.
    inner = " & ".join(" ".join(cmd) for cmd in firewall_rule_commands(port))
    script = ("$p = Start-Process -FilePath cmd.exe -Verb RunAs -Wait -PassThru "
              f"-WindowStyle Hidden -ArgumentList '/c {inner}'; exit $p.ExitCode")
    try:
        result = subprocess.run(
            ["powershell", "-NoProfile", "-NonInteractive", "-Command", script],
            capture_output=True, timeout=120, creationflags=CREATE_NO_WINDOW)
    except (OSError, subprocess.TimeoutExpired):
        return False
    return result.returncode == 0


def resource_frozen() -> bool:
    return bool(getattr(sys, "frozen", False))
