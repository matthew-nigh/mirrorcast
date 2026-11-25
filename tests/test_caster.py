import types
import uuid as uuidlib

import pytest

from chromecast_mirror.caster import HLS_CONTENT_TYPE, CastError, CastManager


class FakeMedia:
    def __init__(self, states):
        self._states = list(states)
        self.played = None
        self.stopped = False

    def play_media(self, url, content_type, **kw):
        self.played = (url, content_type, kw)

    def block_until_active(self, timeout=None):
        pass

    @property
    def status(self):
        state, reason = self._states.pop(0) if len(self._states) > 1 else self._states[0]
        return types.SimpleNamespace(player_state=state, idle_reason=reason)

    def stop(self):
        self.stopped = True


class FakeCast:
    def __init__(self, name, cast_type="cast", states=(("PLAYING", None),)):
        self.name = name
        self.model_name = "Chromecast"
        self.cast_type = cast_type
        self.uuid = uuidlib.uuid4()
        self.cast_info = types.SimpleNamespace(host="192.168.1.50")
        self.media_controller = FakeMedia(states)
        self.quit = False

    def wait(self, timeout=None):
        pass

    def quit_app(self):
        self.quit = True

    def disconnect(self, timeout=None):
        pass


class FakeBackend:
    def __init__(self, casts):
        self.casts = casts
        self.calls = []
        self.discovery = types.SimpleNamespace(stop_discovery=lambda b: self.calls.append("stop"))

    def get_chromecasts(self, blocking, callback, known_hosts=None, **kw):
        assert blocking is False
        self.calls.append(("start", known_hosts))
        for c in self.casts:
            callback(c)
        return object()


def make(casts):
    backend = FakeBackend(casts)
    changes = []
    return CastManager(on_change=lambda: changes.append(1), log=lambda m: None,
                       backend=backend), backend


def test_discovery_skips_speakers_and_groups():
    tv = FakeCast("Living Room TV")
    mgr, _ = make([FakeCast("Kitchen speaker", "audio"), FakeCast("Group", "group"), tv])
    mgr.start_discovery()
    assert mgr.device_list() == [(str(tv.uuid), "Living Room TV (Chromecast)")]
    assert mgr.host_of(str(tv.uuid)) == "192.168.1.50"


def test_add_host_restarts_discovery_with_known_host():
    mgr, backend = make([])
    mgr.start_discovery()
    mgr.add_host(" 192.168.1.77 ")
    assert backend.calls[-1] == ("start", ["192.168.1.77"])
    assert "stop" in backend.calls


def test_play_sends_hls_live_stream_and_stop_quits():
    tv = FakeCast("TV")
    mgr, _ = make([tv])
    mgr.start_discovery()
    mgr.play(str(tv.uuid), "http://192.168.1.10:8765/stream.m3u8", wait_s=2)
    url, ctype, kw = tv.media_controller.played
    assert url.endswith("/stream.m3u8")
    assert ctype == HLS_CONTENT_TYPE
    assert kw["stream_type"] == "LIVE"
    mgr.stop_playback()
    assert tv.quit and tv.media_controller.stopped


def test_play_reports_load_error():
    tv = FakeCast("TV", states=[("IDLE", "ERROR")])
    mgr, _ = make([tv])
    mgr.start_discovery()
    with pytest.raises(CastError, match="Firewall"):
        mgr.play(str(tv.uuid), "http://x/stream.m3u8", wait_s=2)


def test_play_unknown_device():
    mgr, _ = make([])
    with pytest.raises(CastError):
        mgr.play("missing", "http://x")
