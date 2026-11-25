# PyInstaller build file. build.bat runs: pyinstaller MirrorCast.spec
import os
import sys

from PyInstaller.utils.hooks import collect_all, collect_submodules

sys.path.insert(0, os.path.abspath(SPECPATH))
from mirrorcast import APP_NAME, PUBLISHER, __version__  # noqa: E402
from version_info import make as make_version_info  # noqa: E402

datas, binaries, hiddenimports = [], [], []
for pkg in ("imageio_ffmpeg", "soundcard"):
    d, b, h = collect_all(pkg)
    datas += d
    binaries += b
    hiddenimports += h
hiddenimports += collect_submodules("zeroconf") + collect_submodules("pychromecast")

a = Analysis(
    ["main.py"],
    datas=datas,
    binaries=binaries,
    hiddenimports=hiddenimports,
    excludes=["matplotlib", "PIL", "pytest"],
)
pyz = PYZ(a.pure)
exe = EXE(
    pyz, a.scripts, a.binaries, a.datas, [],
    name=APP_NAME,
    version=make_version_info(__version__, APP_NAME, PUBLISHER),
    console=False,
    upx=False,
)
