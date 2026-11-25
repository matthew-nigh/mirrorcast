"""Screen capture -> H.264/AAC -> HLS files -> served over HTTP on the LAN.

ffmpeg does the capture and encoding. The bundled ffmpeg from the
imageio-ffmpeg package is used, so nothing has to be installed separately.
"""

from __future__ import annotations

import collections
import functools
import http.server
import os
import re
import shutil
import subprocess
import tempfile
import threading
import time
from dataclasses import dataclass, field
from typing import Callable, Optional

from .livemp4 import FRAGMENT_SECONDS, KEYFRAME_SECONDS, LiveMp4, wait_ready
from .platform_utils import CREATE_NO_WINDOW, IS_WINDOWS, Monitor

Log = Callable[[str], None]

PLAYLIST = "stream.m3u8"        # media playlist (the list of video segments)
MASTER_PLAYLIST = "index.m3u8"  # what the Chromecast is given: names the codecs
LIVE_MP4 = "live.mp4"           # the lowest-delay stream (see livemp4.py)
HLS_CONTENT_TYPE = "application/x-mpegURL"
MP4_CONTENT_TYPE = "video/mp4"
PLAYLIST_SEGMENTS = 8           # segments listed in the live HLS playlist


@dataclass(frozen=True)
class DelayMode:
    """How the video reaches the TV.

    "mp4": one live MP4 (lowest delay; segment_seconds is the keyframe interval).
    "hls": HLS segments. A Cast receiver starts an HLS live stream about three
    segments behind the newest one, so the delay is roughly 4x segment_seconds.
    """
    kind: str
    segment_seconds: float


DELAY_LOWEST = "Lowest (about 1-3 s)"
DELAY_LOW = "Low (about 3-5 s)"
DELAY_STANDARD = "Standard (about 6-10 s, most reliable)"
DELAY_MODES = {
    DELAY_LOWEST: DelayMode("mp4", KEYFRAME_SECONDS),
    DELAY_LOW: DelayMode("hls", 1),
    DELAY_STANDARD: DelayMode("hls", 2),
}
DELAY_AUTO = "Auto (lowest your TV can play)"
DELAY_CHOICES = [DELAY_AUTO, *DELAY_MODES]
DEFAULT_DELAY = DELAY_AUTO


def delay_attempts(choice: str) -> list[str]:
    """Delay modes to try, lowest delay first."""
    if choice in DELAY_MODES:
        return [choice]
    return list(DELAY_MODES)

# Labels shown in the GUI -> (max width, max height, video bitrate kbit/s)
QUALITY = {
    "720p (smoothest)": (1280, 720, 4000),
    "1080p (sharpest)": (1920, 1080, 7000),
}
DEFAULT_QUALITY = "720p (smoothest)"

ENCODER_CHOICES = {
    "Auto (try graphics card, then CPU)": ["h264_nvenc", "h264_qsv", "h264_amf", "libx264"],
    "CPU (works everywhere)": ["libx264"],
    "NVIDIA graphics card": ["h264_nvenc", "libx264"],
    "Intel graphics": ["h264_qsv", "libx264"],
    "AMD graphics card": ["h264_amf", "libx264"],
}
DEFAULT_ENCODER = "Auto (try graphics card, then CPU)"

DEFAULT_PORT = 8765  # the stream server's port (the firewall rule opens this one)

AUDIO_NONE = "No sound"
AUDIO_LOOPBACK = "PC sound (what you hear)"


# --------------------------------------------------------------------------- ffmpeg

def ffmpeg_path() -> str:
    override = os.environ.get("MIRRORCAST_FFMPEG")
    if override:
        return override
    try:
        import imageio_ffmpeg

        return imageio_ffmpeg.get_ffmpeg_exe()
    except Exception:
        pass
    found = shutil.which("ffmpeg")
    if found:
        return found
    raise RuntimeError(
        "ffmpeg was not found. Run 'pip install imageio-ffmpeg' or install ffmpeg.")


def parse_dshow_audio_devices(stderr_text: str) -> list[str]:
    """Pull audio device names out of `ffmpeg -list_devices true -f dshow` output."""
    names: list[str] = []
    for name in re.findall(r'"([^"]+)"\s+\(audio\)', stderr_text):
        if name not in names:
            names.append(name)
    return names


def list_dshow_audio_devices(ffmpeg: Optional[str] = None) -> list[str]:
    """Microphones and 'Stereo Mix'-style inputs that ffmpeg can record."""
    if not IS_WINDOWS:
        return []
    try:
        result = subprocess.run(
            [ffmpeg or ffmpeg_path(), "-hide_banner", "-list_devices", "true",
             "-f", "dshow", "-i", "dummy"],
            capture_output=True, text=True, errors="replace", timeout=20,
            creationflags=CREATE_NO_WINDOW,
        )
    except Exception:
        return []
    return parse_dshow_audio_devices(result.stderr)


@dataclass(frozen=True)
class Attempt:
    capture: str   # "ddagrab", "gdigrab" or "test"
    encoder: str   # ffmpeg encoder name


def candidate_attempts(encoder_choice: str, test_pattern: bool = False) -> list[Attempt]:
    """Capture/encoder combinations to try, best first."""
    encoders = ENCODER_CHOICES.get(encoder_choice, ENCODER_CHOICES[DEFAULT_ENCODER])
    captures = ["test"] if test_pattern else ["ddagrab", "gdigrab"]
    return [Attempt(c, e) for c in captures for e in encoders]


def _encoder_args(encoder: str, bitrate_k: int, fps: int,
                  keyframe_s: float) -> tuple[list[str], str]:
    """Returns (ffmpeg args, pixel format the encoder wants).

    All encoders are set up to hand each frame over as soon as it is encoded
    (no B-frames, no look-ahead, no queue of frames in flight).
    """
    gop = str(max(1, round(fps * keyframe_s)))
    rate = [
        "-b:v", f"{bitrate_k}k", "-maxrate", f"{bitrate_k}k",
        "-bufsize", f"{bitrate_k * 2}k", "-g", gop, "-bf", "0",
    ]
    # Every segment must start with an IDR frame (plus SPS/PPS) so the Chromecast
    # can start decoding mid-stream. libx264 does that for forced keyframes; the
    # graphics-card encoders turn forced keyframes into plain I-frames unless told.
    if encoder == "h264_nvenc":
        return ["-c:v", "h264_nvenc", "-preset", "p3", "-tune", "ull",
                "-zerolatency", "1", "-delay", "0", "-rc-lookahead", "0",
                "-profile:v", "high", "-rc", "cbr", "-forced-idr", "1"] + rate, "yuv420p"
    if encoder == "h264_qsv":
        return ["-c:v", "h264_qsv", "-preset", "veryfast", "-async_depth", "1",
                "-look_ahead", "0", "-low_delay_brc", "1",
                "-profile:v", "high", "-forced_idr", "1", "-idr_interval", "0"] + rate, "nv12"
    if encoder == "h264_amf":
        return ["-c:v", "h264_amf", "-usage", "ultralowlatency", "-quality", "speed",
                "-profile:v", "high", "-forced_idr", "1",
                "-header_spacing", gop] + rate, "yuv420p"
    return ["-c:v", "libx264", "-preset", "veryfast", "-tune", "zerolatency",
            "-profile:v", "high", "-level:v", "4.1"] + rate, "yuv420p"


def build_ffmpeg_command(
    ffmpeg: str,
    out_dir: str,
    monitor: Monitor,
    attempt: Attempt,
    quality: str = DEFAULT_QUALITY,
    fps: int = 30,
    audio: str = AUDIO_NONE,
    mode: DelayMode = DELAY_MODES[DELAY_STANDARD],
) -> list[str]:
    """The full ffmpeg command for one capture/encoder attempt."""
    max_w, max_h, bitrate_k = QUALITY.get(quality, QUALITY[DEFAULT_QUALITY])
    cmd = [ffmpeg, "-hide_banner", "-loglevel", "error", "-nostats", "-y"]

    # Input 0: the screen
    if attempt.capture == "ddagrab":
        cmd += ["-f", "lavfi", "-i",
                f"ddagrab=output_idx={monitor.index}:framerate={fps}:draw_mouse=1"]
        pre = "hwdownload,format=bgra,"
    elif attempt.capture == "gdigrab":
        cmd += ["-f", "gdigrab", "-framerate", str(fps), "-draw_mouse", "1",
                "-offset_x", str(monitor.x), "-offset_y", str(monitor.y),
                "-video_size", f"{monitor.width}x{monitor.height}", "-i", "desktop"]
        pre = ""
    elif attempt.capture == "test":
        # paced by the wall clock, like a real screen (-re runs ~0.5 s ahead)
        cmd += ["-f", "lavfi", "-i", f"testsrc2=size=1280x720:rate={fps},realtime"]
        pre = ""
    else:
        raise ValueError(f"unknown capture method {attempt.capture!r}")

    # Input 1: sound. A silent track is always added because some Cast
    # receivers are unreliable with video-only HLS.
    if audio == AUDIO_LOOPBACK:
        cmd += ["-thread_queue_size", "1024", "-f", "f32le", "-ar", "48000",
                "-ac", "2", "-i", "pipe:0"]
    elif audio and audio != AUDIO_NONE:
        cmd += ["-thread_queue_size", "1024", "-f", "dshow",
                "-audio_buffer_size", "50", "-i", f"audio={audio}"]
    else:
        cmd += ["-f", "lavfi", "-i", "anullsrc=channel_layout=stereo:sample_rate=48000"]

    keyframe_s = mode.segment_seconds
    enc_args, pix_fmt = _encoder_args(attempt.encoder, bitrate_k, fps, keyframe_s)
    vf = (f"{pre}scale=w={max_w}:h={max_h}:force_original_aspect_ratio=decrease:"
          f"force_divisible_by=2,format={pix_fmt}")

    cmd += ["-map", "0:v:0", "-map", "1:a:0", "-vf", vf, "-fps_mode", "cfr",
            "-r", str(fps)]
    cmd += enc_args
    cmd += ["-force_key_frames", f"expr:gte(t,n_forced*{keyframe_s:g})"]
    cmd += ["-c:a", "aac", "-b:a", "128k", "-ar", "48000", "-ac", "2"]
    cmd += ["-max_muxing_queue_size", "1024"]
    if mode.kind == "mp4":
        # A small fragment every FRAGMENT_SECONDS, a new one at each keyframe,
        # written to stdout the moment it is complete.
        cmd += ["-f", "mp4", "-movflags", "empty_moov+default_base_moof+frag_keyframe",
                "-frag_duration", str(int(FRAGMENT_SECONDS * 1_000_000)),
                "-flush_packets", "1", "pipe:1"]
        return cmd
    cmd += ["-f", "hls", "-hls_time", f"{mode.segment_seconds:g}",
            "-hls_list_size", str(PLAYLIST_SEGMENTS), "-hls_delete_threshold", "4",
            "-hls_flags", "delete_segments+independent_segments+omit_endlist+temp_file",
            "-hls_segment_type", "mpegts", "-master_pl_name", MASTER_PLAYLIST,
            "-hls_segment_filename", os.path.join(out_dir, "seg_%05d.ts"),
            os.path.join(out_dir, PLAYLIST)]
    return cmd


def count_segments(playlist_text: str) -> int:
    return playlist_text.count("#EXTINF")


# --------------------------------------------------------------------------- HTTP

class FetchLog:
    """Counts what has been downloaded from the stream server, so we can tell
    "the TV never reached the PC" (firewall) apart from "it reached it but
    won't play" (format problem)."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self.reset()

    def reset(self) -> None:
        with self._lock:
            self.playlists = 0
            self.segments = 0
            self.clients: set[str] = set()

    def record(self, client: str, path: str) -> None:
        name = path.split("?", 1)[0].rsplit("/", 1)[-1]
        with self._lock:
            if name.endswith(".mp4"):  # live MP4: the TV connected and gets video
                self.playlists += 1
                self.segments += 1
            elif name.endswith(".m3u8"):
                self.playlists += 1
            elif name.endswith(".ts"):
                self.segments += 1
            else:
                return
            self.clients.add(client)


class _Handler(http.server.SimpleHTTPRequestHandler):
    extensions_map = {
        **http.server.SimpleHTTPRequestHandler.extensions_map,
        ".m3u8": "application/vnd.apple.mpegurl",
        ".ts": "video/mp2t",
    }

    def end_headers(self) -> None:
        # Cast receivers fetch HLS with XHR, so CORS headers are required.
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Headers", "*")
        self.send_header("Cache-Control", "no-cache, no-store")
        super().end_headers()

    def _live(self):
        if self.path.split("?", 1)[0] == "/" + LIVE_MP4:
            return getattr(self.server, "live", None)
        return None

    def _send_live_headers(self) -> None:
        self.send_response(200)
        self.send_header("Content-Type", MP4_CONTENT_TYPE)
        self.send_header("Connection", "close")
        self.end_headers()

    def do_GET(self) -> None:
        live = self._live()
        if live is None:
            return super().do_GET()
        fetch_log = getattr(self.server, "fetch_log", None)
        if fetch_log is not None:
            fetch_log.record(self.client_address[0], self.path)
        self._send_live_headers()
        live.serve(self.wfile, self.client_address[0], self.connection)

    def do_HEAD(self) -> None:
        if self._live() is None:
            return super().do_HEAD()
        self._send_live_headers()

    def send_head(self):
        fetch_log = getattr(self.server, "fetch_log", None)
        if fetch_log is not None:
            fetch_log.record(self.client_address[0], self.path)
        return super().send_head()

    def do_OPTIONS(self) -> None:  # CORS preflight
        self.send_response(204)
        self.end_headers()

    def log_message(self, *_args) -> None:
        pass

    def handle(self) -> None:
        try:
            super().handle()
        except (ConnectionError, OSError):
            pass  # the Chromecast dropped a connection; harmless


class StreamServer:
    """Serves the HLS folder, or the live MP4, on all network interfaces."""

    def __init__(self, directory: str, port: int = DEFAULT_PORT):
        handler = functools.partial(_Handler, directory=directory)
        try:
            self.httpd = http.server.ThreadingHTTPServer(("0.0.0.0", port), handler)
        except OSError:
            self.httpd = http.server.ThreadingHTTPServer(("0.0.0.0", 0), handler)
        self.httpd.daemon_threads = True
        self.fetch_log = self.httpd.fetch_log = FetchLog()
        self.httpd.live = None  # set to a LiveMp4 in the lowest-delay mode
        self.port = self.httpd.server_address[1]
        self._thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)

    def start(self) -> None:
        self._thread.start()

    def stop(self) -> None:
        self.httpd.shutdown()
        self.httpd.server_close()


# --------------------------------------------------------------------------- stream

class StreamError(RuntimeError):
    pass


@dataclass
class MirrorStream:
    """Runs ffmpeg + the HTTP server. start() returns once the stream is playable."""

    log: Log = print
    ready_segments: int = 3
    attempt_timeout: float = 12.0
    last_working: Optional[Attempt] = None
    out_dir: Optional[str] = field(default=None, init=False)
    server: Optional[StreamServer] = field(default=None, init=False)
    live: Optional[LiveMp4] = field(default=None, init=False)
    mode: DelayMode = field(default=DELAY_MODES[DELAY_STANDARD], init=False)
    proc: Optional[subprocess.Popen] = field(default=None, init=False)
    attempt: Optional[Attempt] = field(default=None, init=False)
    _stderr: collections.deque = field(default_factory=lambda: collections.deque(maxlen=30),
                                       init=False)
    _audio = None

    def start(self, monitor: Monitor, quality: str = DEFAULT_QUALITY,
              audio: str = AUDIO_NONE, encoder_choice: str = DEFAULT_ENCODER,
              fps: int = 30, test_pattern: bool = False,
              delay: str = DELAY_STANDARD) -> None:
        self.stop()
        self.mode = DELAY_MODES.get(delay, DELAY_MODES[DELAY_STANDARD])
        ffmpeg = ffmpeg_path()
        self.out_dir = tempfile.mkdtemp(prefix="mirrorcast_")
        self.server = StreamServer(self.out_dir)
        self.server.start()

        attempts = candidate_attempts(encoder_choice, test_pattern)
        if self.last_working in attempts:  # try what worked last time first
            attempts.remove(self.last_working)
            attempts.insert(0, self.last_working)

        errors = []
        for attempt in attempts:
            self.log(f"Starting capture ({attempt.capture}, {attempt.encoder})...")
            ok, reason = self._try(ffmpeg, monitor, attempt, quality, fps, audio)
            if ok:
                self.attempt = self.last_working = attempt
                self.log(f"Stream ready using {attempt.capture} + {attempt.encoder}.")
                return
            errors.append(f"{attempt.capture}/{attempt.encoder}: {reason}")
            self.log(f"  that didn't work: {reason}")
        self.stop()
        raise StreamError("Could not start screen capture.\n" + "\n".join(errors[-3:]))

    def _try(self, ffmpeg, monitor, attempt, quality, fps, audio) -> tuple[bool, str]:
        self._kill_proc()
        for name in os.listdir(self.out_dir):
            try:
                os.remove(os.path.join(self.out_dir, name))
            except OSError:
                pass
        mode = self.mode
        cmd = build_ffmpeg_command(ffmpeg, self.out_dir, monitor, attempt, quality, fps,
                                   audio, mode)
        self._stderr.clear()
        loopback = audio == AUDIO_LOOPBACK
        self.proc = subprocess.Popen(
            cmd,
            stdin=subprocess.PIPE if loopback else subprocess.DEVNULL,
            stdout=subprocess.PIPE if mode.kind == "mp4" else subprocess.DEVNULL,
            stderr=subprocess.PIPE,
            creationflags=CREATE_NO_WINDOW,
        )
        threading.Thread(target=self._drain_stderr, args=(self.proc,), daemon=True).start()
        if loopback:
            from .audio_loopback import LoopbackAudio

            self._audio = LoopbackAudio(self.proc.stdin, self.log)
            self._audio.start()

        if mode.kind == "mp4":
            self.live = LiveMp4(self.proc.stdout, self.log)
            self.live.start()
            self.server.httpd.live = self.live
            proc = self.proc
            if wait_ready(self.live, lambda: proc.poll() is None,
                          self.attempt_timeout + mode.segment_seconds):
                return True, ""
            if self.proc.poll() is not None:
                return False, self._last_error() or f"ffmpeg exited ({self.proc.returncode})"
            return False, self._last_error() or "timed out waiting for video"

        playlist = os.path.join(self.out_dir, PLAYLIST)
        deadline = (time.monotonic() + self.attempt_timeout
                    + mode.segment_seconds * self.ready_segments)
        while time.monotonic() < deadline:
            if self.proc.poll() is not None:
                return False, self._last_error() or f"ffmpeg exited ({self.proc.returncode})"
            try:
                with open(playlist, encoding="utf-8", errors="replace") as fh:
                    if (count_segments(fh.read()) >= self.ready_segments
                            and os.path.exists(os.path.join(self.out_dir, MASTER_PLAYLIST))):
                        return True, ""
            except OSError:
                pass
            time.sleep(0.2)
        return False, self._last_error() or "timed out waiting for video"

    def _drain_stderr(self, proc: subprocess.Popen) -> None:
        for raw in iter(proc.stderr.readline, b""):
            line = raw.decode("utf-8", "replace").strip()
            if line:
                self._stderr.append(line)

    def _last_error(self) -> str:
        return " | ".join(list(self._stderr)[-2:])

    @property
    def running(self) -> bool:
        return self.proc is not None and self.proc.poll() is None

    def url(self, host_ip: str, path: Optional[str] = None) -> str:
        """Address of the stream on the LAN: the live MP4, or the HLS master playlist."""
        if not self.server:
            raise StreamError("stream not started")
        if path is None:
            path = LIVE_MP4 if self.mode.kind == "mp4" else MASTER_PLAYLIST
        return f"http://{host_ip}:{self.server.port}/{path}"

    @property
    def content_type(self) -> str:
        return MP4_CONTENT_TYPE if self.mode.kind == "mp4" else HLS_CONTENT_TYPE

    @property
    def fetch_log(self) -> Optional[FetchLog]:
        return self.server.fetch_log if self.server else None

    def recent_errors(self) -> str:
        return "\n".join(self._stderr)

    def _kill_proc(self) -> None:
        if self.live is not None:
            self.live.stop()
            if self.server is not None:
                self.server.httpd.live = None
            self.live = None
        if self._audio is not None:
            self._audio.stop()
            self._audio = None
        if self.proc is not None:
            if self.proc.poll() is None:
                self.proc.terminate()
                try:
                    self.proc.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    self.proc.kill()
            self.proc = None

    def stop(self) -> None:
        self._kill_proc()
        if self.server is not None:
            self.server.stop()
            self.server = None
        if self.out_dir:
            shutil.rmtree(self.out_dir, ignore_errors=True)
            self.out_dir = None
