"""Opens the real window for a few seconds and closes it (needs a desktop)."""

import os

import pytest

tk = pytest.importorskip("tkinter")


def test_window_opens_and_closes():
    try:
        root = tk.Tk()
    except tk.TclError:
        pytest.skip("no display")
    from mirrorcast.gui import App

    app = App(root)
    assert app.start_btn["text"] == "Start mirroring"
    assert app.monitors and app.screen_var.get() == app.monitors[0].label
    root.after(3000, app._on_close)
    root.mainloop()


@pytest.mark.skipif(os.name != "nt", reason="Windows only")
def test_monitor_list_on_windows():
    from mirrorcast.platform_utils import list_monitors

    mons = list_monitors()
    assert mons[0].primary and mons[0].width > 0 and mons[0].height > 0


class _FakeStream:
    """Stands in for MirrorStream: records which delay mode was started."""

    def __init__(self):
        self.started, self.live, self.fetch_log, self.running = [], None, None, True
        self.content_type = None

    def start(self, monitor, delay, **kw):
        from mirrorcast.streamer import DELAY_MODES, HLS_CONTENT_TYPE, MP4_CONTENT_TYPE

        self.started.append(delay)
        mp4 = DELAY_MODES[delay].kind == "mp4"
        self.content_type = MP4_CONTENT_TYPE if mp4 else HLS_CONTENT_TYPE

    def url(self, host):
        return f"http://{host}/x"

    def stop(self):
        pass


def test_auto_delay_falls_back_and_remembers_what_worked(monkeypatch, tmp_path):
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path))
    try:
        root = tk.Tk()
    except tk.TclError:
        pytest.skip("no display")
    from mirrorcast.caster import CastError
    from mirrorcast.gui import App
    from mirrorcast.streamer import DELAY_AUTO, DELAY_LOW, DELAY_STANDARD

    app = App(root)
    try:
        app.stream = _FakeStream()

        def play(uuid, url, content_type, **kw):
            if app.stream.started[-1] == DELAY_LOW:
                raise CastError("this TV can't play that")
            return "PLAYING"

        monkeypatch.setattr(app.casts, "play", play)
        monkeypatch.setattr(app.casts, "host_of", lambda uuid: "127.0.0.1")
        args = ("tv-1", app.monitors[0], app.quality_var.get(), app.audio_var.get(),
                app.encoder_var.get(), DELAY_AUTO)
        app._start_worker(*args)
        assert app.stream.started == [DELAY_LOW, DELAY_STANDARD] and app.mirroring
        assert app.settings["delay_by_device"]["tv-1"] == DELAY_STANDARD

        app.stream.started.clear()
        app._start_worker(*args)  # next time: straight to what worked
        assert app.stream.started == [DELAY_STANDARD]
    finally:
        app._on_close()
