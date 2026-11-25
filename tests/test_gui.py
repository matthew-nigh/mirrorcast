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
