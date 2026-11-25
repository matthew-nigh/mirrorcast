# Windows file properties for MirrorCast.exe (Details tab: Company, Product, Copyright).
# Used by MirrorCast.spec. Keep the version in step with mirrorcast/__init__.py.
from PyInstaller.utils.win32.versioninfo import (
    FixedFileInfo,
    StringFileInfo,
    StringStruct,
    StringTable,
    VarFileInfo,
    VarStruct,
    VSVersionInfo,
)


def make(version: str, app_name: str, publisher: str) -> VSVersionInfo:
    parts = tuple(int(p) for p in (version.split(".") + ["0", "0", "0"])[:4])
    strings = [
        StringStruct("CompanyName", publisher),
        StringStruct("FileDescription", f"{app_name} - mirror your screen to a Chromecast"),
        StringStruct("FileVersion", version),
        StringStruct("InternalName", app_name),
        StringStruct("LegalCopyright", f"Copyright (c) {publisher}"),
        StringStruct("OriginalFilename", f"{app_name}.exe"),
        StringStruct("ProductName", app_name),
        StringStruct("ProductVersion", version),
    ]
    return VSVersionInfo(
        ffi=FixedFileInfo(filevers=parts, prodvers=parts),
        kids=[
            StringFileInfo([StringTable("040904B0", strings)]),
            VarFileInfo([VarStruct("Translation", [0x0409, 1200])]),
        ],
    )
