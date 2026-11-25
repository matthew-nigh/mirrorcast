"""The lowest-delay stream: MP4 parsing, retiming, joining and skipping ahead."""

import io
import subprocess
import threading
import time

import pytest

from mirrorcast import livemp4 as lm
from mirrorcast.streamer import ffmpeg_path


@pytest.fixture(scope="module")
def sample():
    """3 seconds of real fragmented MP4 from ffmpeg (test picture + silence)."""
    cmd = [ffmpeg_path(), "-hide_banner", "-loglevel", "error",
           "-f", "lavfi", "-i", "testsrc2=size=320x240:rate=30",
           "-f", "lavfi", "-i", "anullsrc=channel_layout=stereo:sample_rate=48000",
           "-map", "0:v", "-map", "1:a", "-t", "3",
           "-c:v", "libx264", "-preset", "ultrafast", "-g", "30", "-bf", "0",
           "-c:a", "aac", "-f", "mp4",
           "-movflags", "empty_moov+default_base_moof+frag_keyframe",
           "-frag_duration", "100000", "pipe:1"]
    return subprocess.run(cmd, capture_output=True, check=True).stdout


def _split(data):
    boxes = list(lm.read_boxes(io.BytesIO(data)))
    assert [t for t, _ in boxes[:2]] == [b"ftyp", b"moov"]
    tracks = lm.parse_moov(boxes[1][1])
    frags = [lm.parse_fragment(boxes[i][1] + boxes[i + 1][1], tracks)
             for i in range(2, len(boxes) - 1, 2)]
    return boxes[0][1] + boxes[1][1], tracks, frags


def _decode(data):
    r = subprocess.run([ffmpeg_path(), "-hide_banner", "-loglevel", "warning", "-i", "pipe:0",
                        "-map", "0:v", "-f", "framemd5", "-"],
                       input=data, capture_output=True, check=True)
    pts = [int(line.split(b",")[2]) for line in r.stdout.splitlines()
           if line and not line.startswith(b"#")]
    return pts, r.stderr.decode().strip()


def test_parses_tracks_and_finds_a_keyframe_every_second(sample):
    _init, tracks, frags = _split(sample)
    video = [t for t in tracks.values() if t.is_video]
    assert len(video) == 1 and len(tracks) == 2
    keys = [f.start for f in frags if f.keyframe]
    assert len(keys) == 3 and frags[0].keyframe
    assert all(abs((b - a) - 1.0) < 0.1 for a, b in zip(keys, keys[1:], strict=False))
    assert all(f.start < 0.15 * (i + 1) + 0.05 for i, f in enumerate(frags[:5]))


def test_joining_mid_stream_plays_from_zero(sample):
    init, tracks, frags = _split(sample)
    k = [i for i, f in enumerate(frags) if f.keyframe][1]
    joined = init + b"".join(lm.retimed(f, frags[k].start, tracks) for f in frags[k:])
    pts, warnings = _decode(joined)
    assert warnings == ""
    assert pts[0] == 0 and len(pts) >= 55


def _fake(start, key):
    return lm.Fragment(data=b"", start=start, keyframe=key)


def test_client_waits_for_a_keyframe_then_queues():
    c = lm._Client("tv")
    c.push(_fake(0.0, False))
    assert not c.queue
    c.push(_fake(0.1, True))
    c.push(_fake(0.2, False))
    assert [f.start for f in c.queue] == [0.1, 0.2] and c.offset == 0.1


def test_slow_client_skips_ahead_and_keeps_timestamps_continuous():
    c = lm._Client("tv")
    t = 0.0
    for i in range(16):  # 1.5 s of fragments the TV hasn't taken yet
        c.push(_fake(round(t, 1), i % 10 == 0))
        t += 0.1
    assert c.skips == 1
    assert c.queue[0].keyframe and c.queue[0].start == 1.0
    # what the TV would have got next (0.0 after the offset) is what it gets now
    assert c.queue[0].start - c.offset == pytest.approx(0.0)


class _Paced(io.BytesIO):
    """A pipe that delivers the sample at roughly real time, like ffmpeg does."""

    def read(self, n=-1):
        time.sleep(0.01)
        return super().read(n)


def _serve_one_tv(source):
    live = lm.LiveMp4(source, log=lambda m: None)
    out = io.BytesIO()
    done = threading.Event()

    def tv():
        live.serve(out, "tv")
        done.set()

    threading.Thread(target=tv, daemon=True).start()
    while live.clients == 0:
        time.sleep(0.01)
    live.start()  # the "ffmpeg" pipe ends after 3 s of video, which ends the stream
    assert done.wait(15)
    return live, out.getvalue()


def test_serves_header_then_live_fragments(sample):
    live, data = _serve_one_tv(_Paced(sample))
    pts, warnings = _decode(data)
    assert warnings == ""
    assert pts == _decode(sample)[0]  # every frame, same timing as ffmpeg's own file


def test_tv_that_falls_behind_skips_ahead_without_a_gap(sample):
    # everything arrives at once: to the server the TV looks 2+ seconds behind
    live, data = _serve_one_tv(io.BytesIO(sample))
    pts, warnings = _decode(data)
    assert warnings == ""
    assert len(pts) < len(_decode(sample)[0]) - 20  # some of the backlog was dropped...
    assert pts[0] == 0
    assert max(b - a for a, b in zip(pts, pts[1:], strict=False)) <= 2  # ...without a gap
