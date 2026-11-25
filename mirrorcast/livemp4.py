"""Experimental continuous-video mode: one never-ending fragmented MP4 served over HTTP.

ffmpeg writes a fragmented MP4 to a pipe: a header (ftyp + moov) and then a
fragment (moof + mdat) every ~0.1 s. Each Chromecast that connects gets the
header and then fragments starting at the next keyframe, with timestamps
rewritten so its copy starts at 0. That avoids HLS's big built-in delay (a
receiver starts an HLS live stream about three whole segments behind).

If a TV falls behind (slow Wi-Fi), fragments queue up here. Instead of letting
the delay grow for good, the queue is cut back to the newest keyframe and the
timestamps are shifted so the TV's playback stays continuous.
"""

from __future__ import annotations

import collections
import socket
import struct
import threading
import time
from dataclasses import dataclass, field
from typing import BinaryIO, Callable, Iterator, Optional

Log = Callable[[str], None]

FRAGMENT_SECONDS = 0.1     # ffmpeg -frag_duration
KEYFRAME_SECONDS = 1.0     # how often a TV can join or skip ahead
MAX_BACKLOG_SECONDS = 1.0  # a TV this far behind skips ahead to the newest keyframe

_NON_SYNC_SAMPLE = 0x00010000  # ISO/IEC 14496-12 sample_is_non_sync_sample


# --------------------------------------------------------------------------- parsing

def iter_boxes(buf: bytes, start: int = 0, end: Optional[int] = None
               ) -> Iterator[tuple[bytes, int, int, int]]:
    """Yields (type, offset, total size, header size) for the boxes in buf[start:end]."""
    end = len(buf) if end is None else end
    pos = start
    while pos + 8 <= end:
        size, typ = struct.unpack_from(">I4s", buf, pos)
        header = 8
        if size == 1:
            size = struct.unpack_from(">Q", buf, pos + 8)[0]
            header = 16
        elif size == 0:
            size = end - pos
        if size < header or pos + size > end:
            raise ValueError(f"bad MP4 box {typ!r} at {pos}")
        yield typ, pos, size, header
        pos += size


def _children(buf: bytes, box: tuple[bytes, int, int, int], skip: int = 0):
    _typ, pos, size, header = box
    return iter_boxes(buf, pos + header + skip, pos + size)


def _find(buf: bytes, box, typ: bytes, skip: int = 0):
    return next((b for b in _children(buf, box, skip) if b[0] == typ), None)


@dataclass
class Track:
    track_id: int
    timescale: int = 1000
    is_video: bool = False
    default_sample_flags: int = 0


def parse_moov(moov: bytes) -> dict[int, Track]:
    """Track ids, timescales and types from the header."""
    tracks: dict[int, Track] = {}
    top = next(iter_boxes(moov))
    for box in _children(moov, top):
        if box[0] == b"trak":
            tkhd = _find(moov, box, b"tkhd")
            version = moov[tkhd[1] + tkhd[3]]
            track_id = struct.unpack_from(
                ">I", moov, tkhd[1] + tkhd[3] + (20 if version == 1 else 12))[0]
            track = tracks.setdefault(track_id, Track(track_id))
            mdia = _find(moov, box, b"mdia")
            mdhd = _find(moov, mdia, b"mdhd")
            body = mdhd[1] + mdhd[3]
            track.timescale = struct.unpack_from(
                ">I", moov, body + (20 if moov[body] == 1 else 12))[0]
            hdlr = _find(moov, mdia, b"hdlr")
            track.is_video = moov[hdlr[1] + hdlr[3] + 8:hdlr[1] + hdlr[3] + 12] == b"vide"
        elif box[0] == b"mvex":
            for trex in _children(moov, box):
                if trex[0] == b"trex":
                    body = trex[1] + trex[3]
                    track_id, = struct.unpack_from(">I", moov, body + 4)
                    flags, = struct.unpack_from(">I", moov, body + 20)
                    tracks.setdefault(track_id, Track(track_id)).default_sample_flags = flags
    return tracks


@dataclass
class Fragment:
    data: bytes                      # moof + mdat
    start: float                     # seconds (earliest track)
    keyframe: bool                   # video starts with a keyframe: a TV can start here
    # (offset of the tfdt time field in data, field size, track id)
    times: list[tuple[int, int, int]] = field(default_factory=list)
    decode_times: dict[int, int] = field(default_factory=dict)  # track id -> tfdt


def parse_fragment(data: bytes, tracks: dict[int, Track]) -> Fragment:
    moof = next(iter_boxes(data))
    if moof[0] != b"moof":
        raise ValueError("fragment doesn't start with moof")
    frag = Fragment(data=data, start=0.0, keyframe=False)
    starts = []
    for traf in _children(data, moof):
        if traf[0] != b"traf":
            continue
        tfhd = _find(data, traf, b"tfhd")
        body = tfhd[1] + tfhd[3]
        tf_flags = struct.unpack_from(">I", data, body)[0] & 0xFFFFFF
        track_id = struct.unpack_from(">I", data, body + 4)[0]
        track = tracks.get(track_id, Track(track_id))
        default_flags = track.default_sample_flags
        pos = body + 8
        if tf_flags & 0x01:
            pos += 8  # base_data_offset
        if tf_flags & 0x02:
            pos += 4  # sample_description_index
        if tf_flags & 0x08:
            pos += 4  # default_sample_duration
        if tf_flags & 0x10:
            pos += 4  # default_sample_size
        if tf_flags & 0x20:
            default_flags = struct.unpack_from(">I", data, pos)[0]

        tfdt = _find(data, traf, b"tfdt")
        if tfdt is not None:
            body = tfdt[1] + tfdt[3]
            width = 8 if data[body] == 1 else 4
            value = int.from_bytes(data[body + 4:body + 4 + width], "big")
            frag.times.append((body + 4, width, track_id))
            frag.decode_times[track_id] = value
            starts.append(value / track.timescale)

        if track.is_video:
            first_flags = default_flags
            trun = _find(data, traf, b"trun")
            if trun is not None:
                body = trun[1] + trun[3]
                tr_flags = struct.unpack_from(">I", data, body)[0] & 0xFFFFFF
                count = struct.unpack_from(">I", data, body + 4)[0]
                pos = body + 8
                if tr_flags & 0x001:
                    pos += 4  # data_offset
                if tr_flags & 0x004:
                    first_flags = struct.unpack_from(">I", data, pos)[0]
                elif tr_flags & 0x400 and count:
                    pos += 4 * bool(tr_flags & 0x100) + 4 * bool(tr_flags & 0x200)
                    first_flags = struct.unpack_from(">I", data, pos)[0]
            frag.keyframe = not first_flags & _NON_SYNC_SAMPLE
    frag.start = min(starts) if starts else 0.0
    return frag


def retimed(frag: Fragment, offset_s: float, tracks: dict[int, Track]) -> bytes:
    """The fragment with offset_s seconds taken off every track's decode time."""
    out = bytearray(frag.data)
    for pos, width, track_id in frag.times:
        scale = tracks.get(track_id, Track(track_id)).timescale
        value = max(0, frag.decode_times[track_id] - round(offset_s * scale))
        out[pos:pos + width] = value.to_bytes(width, "big")
    return bytes(out)


def read_boxes(source: BinaryIO) -> Iterator[tuple[bytes, bytes]]:
    """Reads whole top-level boxes from a pipe: yields (type, raw bytes)."""
    while True:
        head = _read_exact(source, 8)
        if head is None:
            return
        size, typ = struct.unpack(">I4s", head)
        if size == 1:
            ext = _read_exact(source, 8)
            if ext is None:
                return
            head += ext
            size = struct.unpack(">Q", ext)[0]
        if size < len(head):
            raise ValueError(f"bad MP4 box {typ!r} from ffmpeg")
        body = _read_exact(source, size - len(head))
        if body is None:
            return
        yield typ, head + body


def _read_exact(source: BinaryIO, n: int) -> Optional[bytes]:
    chunks, got = [], 0
    while got < n:
        chunk = source.read(n - got)
        if not chunk:
            return None
        chunks.append(chunk)
        got += len(chunk)
    return b"".join(chunks)


# --------------------------------------------------------------------------- serving

class _Client:
    def __init__(self, address: str):
        self.address = address
        self.queue: collections.deque[Fragment] = collections.deque()
        self.cond = threading.Condition()
        self.offset: Optional[float] = None  # seconds taken off timestamps
        self.skips = 0
        self.closed = False

    def push(self, frag: Fragment) -> None:
        with self.cond:
            if self.offset is None:
                if not frag.keyframe:
                    return  # wait for a keyframe to start on
                self.offset = frag.start
            self.queue.append(frag)
            if self.queue[-1].start - self.queue[0].start > MAX_BACKLOG_SECONDS:
                self._skip_ahead()
            self.cond.notify()

    def _skip_ahead(self) -> None:
        """Drop queued fragments up to the newest keyframe, keeping playback continuous."""
        newest_key = max((i for i, f in enumerate(self.queue) if f.keyframe), default=0)
        if newest_key == 0:
            return
        first_dropped = self.queue[0].start
        resume = self.queue[newest_key].start
        for _ in range(newest_key):
            self.queue.popleft()
        self.offset += resume - first_dropped
        self.skips += 1


class LiveMp4:
    """Reads ffmpeg's fragmented MP4 and serves it live to any number of TVs."""

    def __init__(self, source: BinaryIO, log: Log = print):
        self.source = source
        self.log = log
        self.init: Optional[bytes] = None
        self.tracks: dict[int, Track] = {}
        self.ready = threading.Event()       # header and a keyframe have arrived
        self.latest_end = 0.0                # newest media time, seconds
        self._clients: list[_Client] = []
        self._lock = threading.Lock()
        self._stopped = threading.Event()
        self._moof: Optional[bytes] = None
        self._thread = threading.Thread(target=self._read, daemon=True)

    def start(self) -> None:
        self._thread.start()

    def stop(self) -> None:
        self._stopped.set()
        with self._lock:
            clients = list(self._clients)
        for c in clients:
            with c.cond:
                c.closed = True
                c.cond.notify_all()

    def _read(self) -> None:
        header = b""
        try:
            for typ, raw in read_boxes(self.source):
                if self._stopped.is_set():
                    return
                if self.init is None:
                    header += raw
                    if typ == b"moov":
                        self.init = header
                        self.tracks = parse_moov(raw)
                elif typ == b"moof":
                    self._moof = raw
                elif typ == b"mdat" and self._moof is not None:
                    self._publish(parse_fragment(self._moof + raw, self.tracks))
                    self._moof = None
        except (OSError, ValueError) as exc:
            if not self._stopped.is_set():
                self.log(f"Live stream reader stopped: {exc}")
        finally:
            self.stop()

    def _publish(self, frag: Fragment) -> None:
        self.latest_end = max(self.latest_end, frag.start + FRAGMENT_SECONDS)
        if frag.keyframe:
            self.ready.set()
        with self._lock:
            clients = list(self._clients)
        for c in clients:
            c.push(frag)

    # -- per TV
    def serve(self, wfile, address: str = "", connection: Optional[socket.socket] = None
              ) -> None:
        """Streams to one TV until it disconnects or the stream stops."""
        if connection is not None:
            try:
                connection.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
            except OSError:
                pass
        client = _Client(address)
        with self._lock:
            self._clients.append(client)
        try:
            if not self.ready.wait(timeout=15) or self.init is None:
                return
            wfile.write(self.init)
            wfile.flush()
            while True:
                with client.cond:
                    while not client.queue and not client.closed:
                        client.cond.wait(timeout=1.0)
                        if self._stopped.is_set():
                            client.closed = True
                    if not client.queue:  # closed, and everything queued was sent
                        return
                    frag = client.queue.popleft()
                    offset = client.offset
                wfile.write(retimed(frag, offset, self.tracks))
                wfile.flush()
        except (ConnectionError, OSError, ValueError):
            pass  # the TV hung up
        finally:
            with self._lock:
                if client in self._clients:
                    self._clients.remove(client)

    def edge_for_latest_client(self) -> Optional[float]:
        """Newest media time in the most recently connected TV's own timeline."""
        with self._lock:
            client = self._clients[-1] if self._clients else None
        if client is None or client.offset is None:
            return None
        return self.latest_end - client.offset

    @property
    def clients(self) -> int:
        with self._lock:
            return len(self._clients)

    @property
    def skips(self) -> int:
        with self._lock:
            return sum(c.skips for c in self._clients)


def wait_ready(live: LiveMp4, proc_alive: Callable[[], bool], timeout: float) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if live.ready.wait(timeout=0.2):
            return True
        if not proc_alive():
            return False
    return False
