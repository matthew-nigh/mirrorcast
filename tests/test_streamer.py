import os
import urllib.request

import pytest

from mirrorcast import streamer as st
from mirrorcast.platform_utils import Monitor

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
    # the default (Standard) mode: 2-second segments, known to work on Chromecasts
    assert cmd[cmd.index("-hls_time") + 1] == "2"
    flags = cmd[cmd.index("-hls_flags") + 1]
    assert "delete_segments" in flags and "temp_file" in flags
    assert cmd[cmd.index("-master_pl_name") + 1] == st.MASTER_PLAYLIST
    assert cmd[cmd.index("-g") + 1] == "60"  # a keyframe at every segment start
    assert "expr:gte(t,n_forced*2)" in cmd


@pytest.mark.parametrize("encoder,flag", [
    ("h264_nvenc", "-forced-idr"), ("h264_qsv", "-forced_idr"), ("h264_amf", "-forced_idr")])
def test_gpu_encoders_start_segments_with_idr_frames(encoder, flag):
    cmd = _cmd(attempt=st.Attempt("gdigrab", encoder))
    assert cmd[cmd.index(flag) + 1] == "1"


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
    server = st.StreamServer(str(tmp_path), port=0)
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
        log = server.fetch_log
        assert (log.playlists, log.segments, log.clients) == (1, 1, {"127.0.0.1"})
        log.reset()
        assert (log.playlists, log.segments) == (0, 0)
    finally:
        server.stop()


def test_fetch_log_ignores_other_files():
    log = st.FetchLog()
    log.record("10.0.0.5", "/index.m3u8?x=1")
    log.record("10.0.0.5", "/favicon.ico")
    assert (log.playlists, log.segments, log.clients) == (1, 0, {"10.0.0.5"})


def test_ffmpeg_path_override(monkeypatch):
    monkeypatch.setenv("MIRRORCAST_FFMPEG", "C:/x/ffmpeg.exe")
    assert st.ffmpeg_path() == "C:/x/ffmpeg.exe"


def test_bundled_ffmpeg_found(monkeypatch):
    monkeypatch.delenv("MIRRORCAST_FFMPEG", raising=False)
    assert os.path.exists(st.ffmpeg_path())


def test_delay_modes_and_auto_order():
    # the continuous-video mode lagged 30+ s on a real Chromecast: Auto skips it
    assert st.delay_attempts(st.DELAY_AUTO) == [st.DELAY_LOW, st.DELAY_STANDARD]
    assert st.delay_attempts(st.DELAY_LOWEST) == [st.DELAY_LOWEST]
    assert st.delay_attempts(st.DELAY_LOW) == [st.DELAY_LOW]
    assert st.DELAY_MODES[st.DELAY_LOWEST].kind == "mp4"
    assert st.DELAY_MODES[st.DELAY_LOW] == st.DelayMode("hls", 1)


def test_lowest_delay_writes_fragmented_mp4_to_stdout():
    cmd = _cmd(mode=st.DELAY_MODES[st.DELAY_LOWEST])
    assert cmd[-1] == "pipe:1"
    assert cmd[cmd.index("-f", cmd.index("-max_muxing_queue_size")) + 1] == "mp4"
    assert "empty_moov" in cmd[cmd.index("-movflags") + 1]
    assert cmd[cmd.index("-frag_duration") + 1] == "100000"  # 0.1 s fragments
    assert cmd[cmd.index("-g") + 1] == "30"  # a keyframe every second at 30 fps
    assert "-hls_time" not in cmd


def test_low_delay_uses_one_second_segments():
    cmd = _cmd(mode=st.DELAY_MODES[st.DELAY_LOW])
    assert cmd[cmd.index("-hls_time") + 1] == "1"
    assert cmd[cmd.index("-g") + 1] == "30"


@pytest.mark.parametrize("encoder,flags", [
    ("h264_nvenc", {"-tune": "ull", "-zerolatency": "1", "-delay": "0"}),
    ("h264_qsv", {"-async_depth": "1", "-look_ahead": "0"}),
    ("h264_amf", {"-usage": "ultralowlatency"}),
    ("libx264", {"-tune": "zerolatency", "-bf": "0"}),
])
def test_encoders_hand_frames_over_immediately(encoder, flags):
    cmd = _cmd(attempt=st.Attempt("gdigrab", encoder))
    for flag, value in flags.items():
        assert cmd[cmd.index(flag) + 1] == value
