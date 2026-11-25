"""Record what the PC is playing (WASAPI loopback) and feed it to ffmpeg.

Windows sends no loopback data while nothing is playing, so this pads with
silence to keep the audio track running in real time. Without that, the video
would stall whenever the PC goes quiet.
"""

from __future__ import annotations

import threading
import time
from typing import BinaryIO, Callable

SAMPLE_RATE = 48000
CHANNELS = 2
BYTES_PER_FRAME = 4 * CHANNELS  # float32 stereo


def loopback_available() -> bool:
    try:
        import numpy  # noqa: F401
        import soundcard  # noqa: F401
    except Exception:
        return False
    return True


def pad_frames_needed(elapsed_s: float, frames_written: int, slack_s: float = 0.1) -> int:
    """How many silent frames to add to catch up with the wall clock."""
    expected = int(elapsed_s * SAMPLE_RATE)
    behind = expected - frames_written
    if behind > int(slack_s * SAMPLE_RATE):
        return behind - int(slack_s * SAMPLE_RATE / 2)
    return 0


class LoopbackAudio:
    def __init__(self, sink: BinaryIO, log: Callable[[str], None] = print):
        self.sink = sink
        self.log = log
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._run, daemon=True)

    def start(self) -> None:
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        try:
            self.sink.close()
        except Exception:
            pass

    def _write(self, data: bytes) -> bool:
        try:
            self.sink.write(data)
            self.sink.flush()
            return True
        except (BrokenPipeError, OSError, ValueError):
            return False

    def _run(self) -> None:
        try:
            import ctypes

            ctypes.windll.ole32.CoInitializeEx(None, 0)  # COM for this thread
        except Exception:
            pass
        silence_only = False
        try:
            import numpy as np
            import soundcard as sc

            speaker = sc.default_speaker()
            mic = sc.get_microphone(id=str(speaker.name), include_loopback=True)
            recorder = mic.recorder(samplerate=SAMPLE_RATE, channels=CHANNELS)
        except Exception as exc:  # keep the stream alive with silence
            self.log(f"PC sound capture unavailable ({exc!r}); sending silence.")
            silence_only = True
            recorder = None

        start = time.monotonic()
        written = 0
        if silence_only:
            while not self._stop.is_set():
                pad = pad_frames_needed(time.monotonic() - start, written, 0.02)
                if pad and not self._write(b"\x00" * pad * BYTES_PER_FRAME):
                    return
                written += pad
                time.sleep(0.02)
            return

        with recorder:
            while not self._stop.is_set():
                data = recorder.record(numframes=None)
                if len(data):
                    if data.ndim == 1:
                        data = np.repeat(data[:, None], CHANNELS, axis=1)
                    data = data[:, :CHANNELS]
                    if not self._write(data.astype("<f4").tobytes()):
                        return
                    written += len(data)
                pad = pad_frames_needed(time.monotonic() - start, written)
                if pad:
                    if not self._write(b"\x00" * pad * BYTES_PER_FRAME):
                        return
                    written += pad
                time.sleep(0.01)
