"""Finding Chromecasts (mDNS) and telling one to play the stream."""

from __future__ import annotations

import threading
import time
from typing import Callable, Optional

HLS_CONTENT_TYPE = "application/x-mpegURL"
SKIP_CAST_TYPES = {"audio", "group"}  # speakers and speaker groups can't show video


class CastError(RuntimeError):
    pass


class CastManager:
    def __init__(self, on_change: Callable[[], None], log: Callable[[str], None] = print,
                 backend=None):
        if backend is None:
            import pychromecast as backend  # imported late so tests can mock it
        self.pc = backend
        self.on_change = on_change
        self.log = log
        self.devices: dict[str, object] = {}
        self.known_hosts: list[str] = []
        self._browser = None
        self._lock = threading.Lock()
        self.active = None

    # -- discovery
    def start_discovery(self) -> None:
        self.stop_discovery()
        with self._lock:
            self.devices.clear()
        self.on_change()
        self._browser = self.pc.get_chromecasts(
            blocking=False, callback=self._found, tries=2, retry_wait=3, timeout=10,
            known_hosts=list(self.known_hosts) or None,
        )

    def _found(self, cast) -> None:
        if getattr(cast, "cast_type", None) in SKIP_CAST_TYPES:
            return
        with self._lock:
            self.devices[str(cast.uuid)] = cast
        self.on_change()

    def add_host(self, ip: str) -> None:
        ip = ip.strip()
        if ip and ip not in self.known_hosts:
            self.known_hosts.append(ip)
        self.start_discovery()

    def device_list(self) -> list[tuple[str, str]]:
        """(uuid, label) pairs sorted by name."""
        with self._lock:
            items = list(self.devices.items())
        out = []
        for uuid, cast in items:
            model = getattr(cast, "model_name", "") or ""
            out.append((uuid, f"{cast.name} ({model})" if model else cast.name))
        return sorted(out, key=lambda x: x[1].lower())

    def host_of(self, uuid: str) -> Optional[str]:
        cast = self.devices.get(uuid)
        info = getattr(cast, "cast_info", None)
        return getattr(info, "host", None)

    def stop_discovery(self) -> None:
        if self._browser is not None:
            try:
                self.pc.discovery.stop_discovery(self._browser)
            except Exception:
                pass
            self._browser = None

    # -- playback
    def play(self, uuid: str, url: str, title: str = "Windows screen",
             wait_s: float = 25.0) -> None:
        cast = self.devices.get(uuid)
        if cast is None:
            raise CastError("That Chromecast is no longer on the list. Press Refresh.")
        self.log(f"Connecting to {cast.name}...")
        try:
            cast.wait(timeout=15)
        except Exception as exc:
            raise CastError(f"Couldn't connect to {cast.name}: {exc}") from exc
        mc = cast.media_controller
        self.log(f"Asking {cast.name} to play {url}")
        mc.play_media(url, HLS_CONTENT_TYPE, title=title, stream_type="LIVE")
        try:
            mc.block_until_active(timeout=20)
        except Exception:
            pass
        self.active = cast
        deadline = time.monotonic() + wait_s
        while time.monotonic() < deadline:
            status = mc.status
            state = getattr(status, "player_state", None)
            if state in ("PLAYING", "BUFFERING"):
                self.log(f"{cast.name} is {state.lower()}.")
                return
            if state == "IDLE" and getattr(status, "idle_reason", None) == "ERROR":
                raise CastError(
                    f"{cast.name} couldn't load the stream. This usually means Windows "
                    "Firewall is blocking the app: allow it on Private networks.")
            time.sleep(0.5)
        self.log(f"{cast.name} hasn't confirmed playback yet; it may still start.")

    def stop_playback(self) -> None:
        cast, self.active = self.active, None
        if cast is None:
            return
        try:
            cast.media_controller.stop()
        except Exception:
            pass
        try:
            cast.quit_app()
        except Exception:
            pass

    def shutdown(self) -> None:
        self.stop_playback()
        self.stop_discovery()
        for cast in list(self.devices.values()):
            try:
                cast.disconnect(timeout=2)
            except Exception:
                pass
