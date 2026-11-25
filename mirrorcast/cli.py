"""Entry point. `--selftest` checks capture + encoding + HTTP without a Chromecast."""

from __future__ import annotations

import argparse
import sys
import time
import urllib.request

from .platform_utils import IS_WINDOWS, enable_dpi_awareness, list_monitors, local_ip_towards


def selftest(seconds: float, test_pattern: bool, encoder: str) -> int:
    from .streamer import PLAYLIST, MirrorStream, StreamError, count_segments

    stream = MirrorStream(log=print)
    monitor = list_monitors()[0]
    print(f"Capturing {monitor.label} ...")
    try:
        stream.start(monitor, encoder_choice=encoder, test_pattern=test_pattern)
        master_url = stream.url(local_ip_towards(None))
        print(f"Stream URL: {master_url}")
        time.sleep(seconds)
        with urllib.request.urlopen(master_url, timeout=5) as resp:
            master = resp.read().decode()
        codecs_ok = "avc1." in master and "mp4a." in master and PLAYLIST in master
        url = stream.url(local_ip_towards(None), PLAYLIST)
        with urllib.request.urlopen(url, timeout=5) as resp:
            body = resp.read().decode()
            cors = resp.headers.get("Access-Control-Allow-Origin")
        n = count_segments(body)
        first_seg = [ln for ln in body.splitlines() if ln.endswith(".ts")][0]
        seg_url = url.rsplit("/", 1)[0] + "/" + first_seg
        with urllib.request.urlopen(seg_url, timeout=5) as resp:
            size = len(resp.read())
        print(f"Playlist has {n} segments, CORS={cors}, first segment {size} bytes, "
              f"codecs listed={codecs_ok}")
        ok = n >= 2 and cors == "*" and size > 1000 and codecs_ok and stream.running
        print("SELFTEST PASSED" if ok else "SELFTEST FAILED")
        return 0 if ok else 1
    except (StreamError, OSError, IndexError) as exc:
        print(f"SELFTEST FAILED: {exc}")
        print(stream.recent_errors())
        return 1
    finally:
        stream.stop()


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(prog="MirrorCast")
    parser.add_argument("--selftest", action="store_true",
                        help="check screen capture and the stream server, then exit")
    parser.add_argument("--test-pattern", action="store_true",
                        help="with --selftest: use a test picture instead of the screen")
    parser.add_argument("--encoder", default=None, help="with --selftest: encoder choice")
    parser.add_argument("--seconds", type=float, default=4.0)
    args = parser.parse_args(argv)

    enable_dpi_awareness()
    if args.selftest:
        from .streamer import DEFAULT_ENCODER

        return selftest(args.seconds, args.test_pattern or not IS_WINDOWS,
                        args.encoder or DEFAULT_ENCODER)
    from .gui import run

    run()
    return 0


if __name__ == "__main__":
    sys.exit(main())
