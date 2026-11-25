"""End-to-end: real ffmpeg -> HLS -> HTTP, using a test picture (or the real
screen when MIRRORCAST_REAL_CAPTURE=1, as on the Windows CI runner)."""

import os
import time
import urllib.request

from mirrorcast.platform_utils import list_monitors
from mirrorcast.streamer import AUDIO_LOOPBACK, PLAYLIST, MirrorStream, count_segments

REAL = os.environ.get("MIRRORCAST_REAL_CAPTURE") == "1"


def _run(**kw):
    stream = MirrorStream(log=print)
    try:
        stream.start(list_monitors()[0], encoder_choice="CPU (works everywhere)",
                     test_pattern=not REAL, **kw)
        time.sleep(2)
        assert stream.running
        with urllib.request.urlopen(stream.url("127.0.0.1"), timeout=5) as r:
            master = r.read().decode()  # what the Chromecast is given
        assert "CODECS=" in master and "avc1." in master and PLAYLIST in master
        with urllib.request.urlopen(stream.url("127.0.0.1", PLAYLIST), timeout=5) as r:
            body = r.read().decode()
        assert count_segments(body) >= 3
        assert "#EXT-X-TARGETDURATION:2" in body
        return stream.attempt
    finally:
        stream.stop()


def test_pipeline_produces_hls():
    attempt = _run()
    print("worked with", attempt)


def test_pipeline_with_loopback_audio_keeps_running():
    _run(audio=AUDIO_LOOPBACK)
