# MirrorCast

*Published by M. Nigh.*

A small Windows app that shows your PC screen on a Chromecast (or a TV with
Chromecast built in). Pick the Chromecast, pick a screen, press **Start mirroring**.

> Chrome can already do this: open Chrome, menu **⋮ > Cast... > Sources > Cast screen**.
> Try it to compare; it usually has less delay than this app.

## How it works

The app records your screen with ffmpeg (bundled, no separate install),
turns it into a live HLS video stream, serves that stream on your home network,
and tells the Chromecast to play it. Expect the TV to run **about 6 to 10 seconds
behind** the PC. That's fine for slides, photos and videos, but not for games
or typing.

## Option A: build a one-file .exe (recommended)

1. Install **Python 3.10 or newer** from <https://www.python.org/downloads/>.
   In the installer, tick **Add python.exe to PATH**.
2. Download this folder and double-click **build.bat**. It takes a few minutes
   the first time.
3. Your app is at `dist\MirrorCast.exe`. Copy it anywhere and double-click it.
   It takes a few seconds to open because it unpacks itself.

If Windows SmartScreen says "Windows protected your PC", click **More info >
Run anyway** (the app isn't code-signed).

The GitHub Actions build (if you use the repo) also produces the .exe: open
the latest CI run and download **MirrorCast-exe** under Artifacts.

## Option B: run from source

Install Python as above, then double-click **run.bat**. The first run installs
the libraries it needs.

## Using it

1. Make sure the PC and the Chromecast are on the **same Wi-Fi / network**.
2. Open the app. The first time, Windows Firewall asks whether to allow it:
   tick **Private networks** and click **Allow**. Without this the Chromecast
   can't be found or can't fetch the video.
3. Choose your Chromecast. If it doesn't appear after ~10 seconds, type its IP
   address (Google Home app > device > Settings > Device information) into
   **Not listed? IP** and click **Add**.
4. Choose the screen, quality and sound, then click **Start mirroring**.
5. Click **Stop mirroring** when done (closing the app also stops it).

### Settings

- **Quality:** 720p is smoother and lighter on the PC and Wi-Fi; 1080p is sharper.
- **Sound:**
  - *No sound* (a silent track is still sent, which Chromecasts like better).
  - *PC sound (what you hear)* sends whatever is playing on the PC.
  - Any microphone or "Stereo Mix" device listed.
- **Encoder:** *Auto* tries your graphics card first (NVIDIA, Intel, AMD) and
  falls back to the CPU. Pick *CPU* if Auto gives a broken picture.

## Troubleshooting

- **No Chromecasts found:** check the firewall step above. If your Wi-Fi is set
  as a *Public* network in Windows, either switch it to *Private* (Settings >
  Network & internet > Wi-Fi > your network) or allow the app on Public networks
  in *Windows Security > Firewall > Allow an app through firewall*. Guest Wi-Fi
  networks and some mesh routers block device discovery: use the IP box instead.
- **The TV shows the cast icon or a black screen, or says it is buffering:**
  MirrorCast checks whether the TV actually downloads the video. If it doesn't
  within 15 seconds, that is the firewall: the app says so and offers to add a
  Windows Firewall rule for you (Windows asks for permission). The rule, named
  *MirrorCast*, only opens the stream port (TCP 8765) to devices on your local
  network. If the TV *is* downloading but stays black, set **Encoder** to *CPU*
  and try again.
- **Wrong screen shown:** pick the other screen in the list.
- **Choppy video:** choose 720p, or try a different Encoder option.
- **Check capture without a Chromecast:** run `MirrorCast.exe --selftest`
  (or `run.bat --selftest`); it records 4 seconds, checks the stream, and
  exits with code 0 if all is well.
- Logs are in `%LOCALAPPDATA%\MirrorCast\log.txt`.
- The app's file properties (right-click the .exe > Properties > Details) list
  M. Nigh as the company. SmartScreen will still say "Unknown publisher" until
  the .exe is code-signed with a certificate in that name.

## Developer notes

- `mirrorcast/streamer.py`: ffmpeg command, HLS HTTP server (with the
  CORS headers Cast receivers need), capture fallback (ddagrab, then gdigrab).
  Segments are 2 seconds: Cast receivers start a live stream ~3 segments behind
  the newest one, and with 1-second segments they never buffered enough to
  start. The Chromecast is given a master playlist (`index.m3u8`) that names the
  codecs, and every segment starts with an IDR frame (also on GPU encoders).
  The server counts what it serves (`FetchLog`) so playback problems can be
  told apart from firewall problems.
- `mirrorcast/caster.py`: discovery and playback via PyChromecast
  (Default Media Receiver, HLS with `hlsSegmentFormat: ts`, `LIVE`). It waits for
  `PLAYING`, not just `BUFFERING`.
- `mirrorcast/platform_utils.py`: monitors, DPI, local IP, firewall rule.
- `version_info.py`: Windows file properties (company M. Nigh) for the .exe.
- `mirrorcast/audio_loopback.py`: WASAPI loopback via `soundcard`,
  padded with silence so the stream never stalls when the PC is quiet.
- Tests: `pip install -r requirements-dev.txt` then `pytest`. On Windows,
  `set MIRRORCAST_REAL_CAPTURE=1` makes the pipeline tests use the real screen.
