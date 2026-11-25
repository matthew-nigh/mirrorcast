"""The window: pick a Chromecast and a screen, press Start."""

from __future__ import annotations

import logging
import os
import queue
import threading
import tkinter as tk
from tkinter import messagebox, ttk

from . import APP_NAME, __version__
from .platform_utils import app_data_dir, list_monitors, local_ip_towards
from .streamer import (
    AUDIO_LOOPBACK,
    AUDIO_NONE,
    DEFAULT_ENCODER,
    DEFAULT_QUALITY,
    ENCODER_CHOICES,
    QUALITY,
    MirrorStream,
    StreamError,
    list_dshow_audio_devices,
)

log = logging.getLogger("ccmirror")


class App:
    def __init__(self, root: tk.Tk):
        self.root = root
        self.ui_queue: "queue.Queue" = queue.Queue()
        self.stream = MirrorStream(log=self._log)
        self.busy = False
        self.mirroring = False
        self.device_ids: list[str] = []

        root.title(f"{APP_NAME} {__version__}")
        root.minsize(520, 440)
        root.protocol("WM_DELETE_WINDOW", self._on_close)
        self._build()

        from .caster import CastManager

        self.casts = CastManager(on_change=lambda: self._ui(self._refresh_devices),
                                 log=self._log)
        self._refresh_audio_list()
        self._start_discovery()
        root.after(100, self._pump)
        root.after(2000, self._watchdog)

    # ------------------------------------------------------------------ layout
    def _build(self) -> None:
        pad = {"padx": 8, "pady": 4}
        frm = ttk.Frame(self.root, padding=10)
        frm.pack(fill="both", expand=True)
        frm.columnconfigure(1, weight=1)

        ttk.Label(frm, text="Chromecast:").grid(row=0, column=0, sticky="w", **pad)
        self.device_var = tk.StringVar()
        self.device_box = ttk.Combobox(frm, textvariable=self.device_var, state="readonly")
        self.device_box.grid(row=0, column=1, sticky="ew", **pad)
        ttk.Button(frm, text="Refresh", command=self._start_discovery).grid(
            row=0, column=2, **pad)

        ttk.Label(frm, text="Not listed? IP:").grid(row=1, column=0, sticky="w", **pad)
        self.ip_var = tk.StringVar()
        ttk.Entry(frm, textvariable=self.ip_var).grid(row=1, column=1, sticky="ew", **pad)
        ttk.Button(frm, text="Add", command=self._add_ip).grid(row=1, column=2, **pad)

        ttk.Label(frm, text="Screen:").grid(row=2, column=0, sticky="w", **pad)
        self.monitors = list_monitors()
        self.screen_var = tk.StringVar(value=self.monitors[0].label)
        ttk.Combobox(frm, textvariable=self.screen_var, state="readonly",
                     values=[m.label for m in self.monitors]).grid(
            row=2, column=1, columnspan=2, sticky="ew", **pad)

        ttk.Label(frm, text="Quality:").grid(row=3, column=0, sticky="w", **pad)
        self.quality_var = tk.StringVar(value=DEFAULT_QUALITY)
        ttk.Combobox(frm, textvariable=self.quality_var, state="readonly",
                     values=list(QUALITY)).grid(row=3, column=1, columnspan=2,
                                                sticky="ew", **pad)

        ttk.Label(frm, text="Sound:").grid(row=4, column=0, sticky="w", **pad)
        self.audio_var = tk.StringVar(value=AUDIO_NONE)
        self.audio_box = ttk.Combobox(frm, textvariable=self.audio_var, state="readonly")
        self.audio_box.grid(row=4, column=1, columnspan=2, sticky="ew", **pad)

        ttk.Label(frm, text="Encoder:").grid(row=5, column=0, sticky="w", **pad)
        self.encoder_var = tk.StringVar(value=DEFAULT_ENCODER)
        ttk.Combobox(frm, textvariable=self.encoder_var, state="readonly",
                     values=list(ENCODER_CHOICES)).grid(row=5, column=1, columnspan=2,
                                                        sticky="ew", **pad)

        self.start_btn = ttk.Button(frm, text="Start mirroring", command=self._toggle)
        self.start_btn.grid(row=6, column=0, columnspan=3, sticky="ew", padx=8, pady=10)

        self.status_var = tk.StringVar(value="Looking for Chromecasts...")
        ttk.Label(frm, textvariable=self.status_var, wraplength=480).grid(
            row=7, column=0, columnspan=3, sticky="w", **pad)

        self.log_box = tk.Text(frm, height=8, state="disabled", wrap="word")
        self.log_box.grid(row=8, column=0, columnspan=3, sticky="nsew", **pad)
        frm.rowconfigure(8, weight=1)

    # ------------------------------------------------------------------ helpers
    def _ui(self, fn, *args) -> None:
        """Run fn on the Tk thread (safe to call from any thread)."""
        self.ui_queue.put((fn, args))

    def _pump(self) -> None:
        try:
            while True:
                fn, args = self.ui_queue.get_nowait()
                fn(*args)
        except queue.Empty:
            pass
        self.root.after(100, self._pump)

    def _log(self, msg: str) -> None:
        log.info(msg)
        self._ui(self._append_log, msg)

    def _append_log(self, msg: str) -> None:
        self.log_box.configure(state="normal")
        self.log_box.insert("end", msg + "\n")
        self.log_box.see("end")
        self.log_box.configure(state="disabled")

    def _status(self, msg: str) -> None:
        self._ui(self.status_var.set, msg)

    # ------------------------------------------------------------------ devices
    def _start_discovery(self) -> None:
        self.status_var.set("Looking for Chromecasts...")
        threading.Thread(target=self._safe, args=(self.casts.start_discovery,),
                         daemon=True).start()
        self.root.after(8000, self._discovery_hint)

    def _discovery_hint(self) -> None:
        if not self.device_ids and not self.mirroring:
            self.status_var.set(
                "No Chromecast found yet. Check the PC and TV are on the same Wi-Fi, "
                "allow the app in Windows Firewall, or type the Chromecast's IP above.")

    def _add_ip(self) -> None:
        ip = self.ip_var.get().strip()
        if ip:
            self._log(f"Looking for a Chromecast at {ip}...")
            threading.Thread(target=self._safe, args=(self.casts.add_host, ip),
                             daemon=True).start()

    def _refresh_devices(self) -> None:
        items = self.casts.device_list()
        current = self.device_var.get()
        self.device_ids = [u for u, _ in items]
        labels = [label for _, label in items]
        self.device_box["values"] = labels
        if labels and current not in labels:
            self.device_var.set(labels[0])
        if labels and not self.mirroring and not self.busy:
            self.status_var.set(f"Found {len(labels)} Chromecast(s). Ready.")

    def _refresh_audio_list(self) -> None:
        def work():
            from .audio_loopback import loopback_available

            values = [AUDIO_NONE]
            if loopback_available():
                values.append(AUDIO_LOOPBACK)
            values += list_dshow_audio_devices()
            self._ui(self.audio_box.configure, {"values": values})

        threading.Thread(target=self._safe, args=(work,), daemon=True).start()

    def _safe(self, fn, *args) -> None:
        try:
            fn(*args)
        except Exception as exc:
            log.exception("background task failed")
            self._log(f"Error: {exc}")

    # ------------------------------------------------------------------ start/stop
    def _toggle(self) -> None:
        if self.busy:
            return
        if self.mirroring:
            self._stop()
        else:
            self._start()

    def _selected_uuid(self):
        labels = list(self.device_box["values"])
        label = self.device_var.get()
        if label in labels:
            return self.device_ids[labels.index(label)]
        return None

    def _start(self) -> None:
        uuid = self._selected_uuid()
        if uuid is None:
            messagebox.showinfo(APP_NAME, "Pick a Chromecast first.")
            return
        monitor = next((m for m in self.monitors if m.label == self.screen_var.get()),
                       self.monitors[0])
        self._set_busy(True, "Starting...")
        threading.Thread(target=self._start_worker, daemon=True, args=(
            uuid, monitor, self.quality_var.get(), self.audio_var.get(),
            self.encoder_var.get())).start()

    def _start_worker(self, uuid, monitor, quality, audio, encoder) -> None:
        try:
            self._status("Starting screen capture...")
            self.stream.start(monitor, quality=quality, audio=audio,
                              encoder_choice=encoder)
            host = self.casts.host_of(uuid)
            url = self.stream.url(local_ip_towards(host))
            self._status("Sending to the Chromecast...")
            self.casts.play(uuid, url)
            self.mirroring = True
            self._status("Mirroring. Expect the TV to run a few seconds behind the PC.")
        except Exception as exc:
            log.exception("start failed")
            self.stream.stop()
            self.casts.stop_playback()
            self.mirroring = False
            self._status("Couldn't start. See the details below.")
            self._log(f"Error: {exc}")
            if isinstance(exc, StreamError):
                self._log(self.stream.recent_errors())
        finally:
            self._ui(self._set_busy, False)

    def _stop(self) -> None:
        self._set_busy(True, "Stopping...")

        def work():
            self.casts.stop_playback()
            self.stream.stop()
            self.mirroring = False
            self._status("Stopped.")
            self._ui(self._set_busy, False)

        threading.Thread(target=self._safe, args=(work,), daemon=True).start()

    def _set_busy(self, busy: bool, status: str | None = None) -> None:
        self.busy = busy
        if status:
            self.status_var.set(status)
        if busy:
            self.start_btn.configure(state="disabled")
        else:
            self.start_btn.configure(
                state="normal",
                text="Stop mirroring" if self.mirroring else "Start mirroring")

    def _watchdog(self) -> None:
        if self.mirroring and not self.busy and not self.stream.running:
            self._log("Screen capture stopped unexpectedly:")
            self._log(self.stream.recent_errors() or "(no details)")
            self._stop()
        self.root.after(2000, self._watchdog)

    def _on_close(self) -> None:
        self.status_var.set("Closing...")
        try:
            self.casts.shutdown()
        finally:
            self.stream.stop()
            self.root.destroy()


def setup_logging() -> str:
    path = os.path.join(app_data_dir(), "log.txt")
    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s",
        handlers=[logging.FileHandler(path, encoding="utf-8")])
    return path


def run() -> None:
    setup_logging()
    root = tk.Tk()
    try:
        ttk.Style(root).theme_use("vista")
    except tk.TclError:
        pass
    App(root)
    root.mainloop()
