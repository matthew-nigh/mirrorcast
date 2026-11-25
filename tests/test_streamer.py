import os
import urllib.request

import pytest

from chromecast_mirror import streamer as st
from chromecast_mirror.platform_utils import Monitor

MON = Monitor(1, 1920, 0, 2560, 1440, False)

DSHOW_SAMPLE = '''
[dshow @ 000001] "Integrated Camera" (video)
[dshow @ 000001]   Alternative name "@device_pnp_\\\\?\\usb#vid"
[dshow @ 000001] "Microphone (Realtek(R) Audio)" (audio)
[dshow @ 000001]   Alternative name "@device_cm_{33D9A762}\\wave_{ABC}"
[dshow @ 000001] "Stereo Mix (Realtek(R) Audio)" (audio)
[dshow @ 000001] "Microphone (Realtek(R) Audio)" (audio)
'''


def test_parse_dshow_audio_devices():
    assert st.parse_dshow_audio_devices(DSHOW_SAMPLE) == [
        "Microphone (Realtek(R) Audio)", "Stereo Mix (Realtek(R) Audio)"]


def test_candidates_order_and_fallback():
    auto = st.candidate_attempts(st.DEFAULT_ENCODER)
    assert auto[0] == st.Attempt("ddagrab", "h264_nvenc")
    assert auto[-1] == st.Attempt("gdigrab", "libx264")
    assert st.candidate_attempts("CPU (works everywhere)") == [
        st.Attempt("ddagrab", "libx264"), st.Attempt("gdigrab", "libx264")]
    assert st.candidate_attempts("nonsense") == auto
    assert all(a.capture == "test" for a in st.candidate_attempts(st.DEFAULT_ENCODER, True))


def _cmd(**kw):
    defaults = dict(ffmpeg="ffmpeg", out_dir="OUT", monitor=MON,
                    attempt=st.Attempt("gdigrab", "libx264"))
    defaults.update(kw)
    return st.build_ffmpeg_command(**defaults)


def test_gdigrab_uses_monitor_rectangle():
    cmd = " ".join(_cmd())
    assert "-f gdigrab" in cmd
    assert "-offset_x 1920 -offset_y 0 -video_size 2560x1440 -i desktop" in cmd


def test_ddagrab_uses_output_index_and_downloads_frames():
    cmd = _cmd(attempt=st.Attempt("ddagrab", "libx264"))
    assert "ddagrab=output_idx=1:framerate=30:draw_mouse=1" in cmd
    vf = cmd[cmd.index("-vf") + 1]
    assert vf.startswith("hwdownload,format=bgra,scale=w=1280:h=720")


def test_quality_and_encoder_settings():
    cmd = _cmd(quality="1080p (sharpest)")
    vf = cmd[cmd.index("-vf") + 1]
    assert "scale=w=1920:h=1080:force_original_aspect_ratio=decrease" in vf
    assert vf.endswith("format=yuv420p")
    assert cmd[cmd.index("-b:v") + 1] == "7000k"
    assert "-tune" in cmd and "zerolatency" in cmd
    qsv = _cmd(attempt=st.Attempt("gdigrab", "h264_qsv"))
    assert qsv[qsv.index("-vf") + 1].endswith("format=nv12")


def test_hls_output_settings():
    cmd = _cmd()
    assert cmd[-1] == os.path.join("OUT", st.PLAYLIST)
    assert cmd[cmd.index("-f", cmd.index("-max_muxing_queue_size")) + 1] == "hls"
    assert cmd[cmd.index("-hls_time") + 1] == "1"
    assert "delete_segments" in cmd[cmd.index("-hls_flags") + 1]


@pytest.mark.parametrize("audio,expect", [
    (st.AUDIO_NONE, "anullsrc"),
    (st.AUDIO_LOOPBACK, "pipe:0"),
    ("Stereo Mix (Realtek(R) Audio)", "audio=Stereo Mix (Realtek(R) Audio)"),
])
def test_audio_inputs(audio, expect):
    cmd = _cmd(audio=audio)
    assert any(expect in part for part in cmd)
    assert cmd[cmd.index("-c:a") + 1] == "aac"


def test_count_segments():
    assert st.count_segments("#EXTM3U\n#EXTINF:1.0,\na.ts\n#EXTINF:1.0,\nb.ts\n") == 2


def test_hls_server_serves_with_cors(tmp_path):
    (tmp_path / "stream.m3u8").write_text("#EXTM3U\n")
    (tmp_path / "seg_00000.ts").write_bytes(b"\x47" * 188)
    server = st.HlsServer(str(tmp_path), port=0)
    server.start()
    try:
        base = f"http://127.0.0.1:{server.port}/"
        with urllib.request.urlopen(base + "stream.m3u8", timeout=5) as r:
            assert r.headers["Access-Control-Allow-Origin"] == "*"
            assert r.headers["Content-Type"] == "application/vnd.apple.mpegurl"
            assert "no-cache" in r.headers["Cache-Control"]
        with urllib.request.urlopen(base + "seg_00000.ts", timeout=5) as r:
            assert r.headers["Content-Type"] == "video/mp2t"
            assert len(r.read()) == 188
    finally:
        server.stop()


def test_ffmpeg_path_override(monkeypatch):
    monkeypatch.setenv("CCMIRROR_FFMPEG", "C:/x/ffmpeg.exe")
    assert st.ffmpeg_path() == "C:/x/ffmpeg.exe"


def test_bundled_ffmpeg_found(monkeypatch):
    monkeypatch.delenv("CCMIRROR_FFMPEG", raising=False)
    assert os.path.exists(st.ffmpeg_path())
