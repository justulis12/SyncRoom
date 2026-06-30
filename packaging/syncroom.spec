# PyInstaller spec for the desktop client.

from pathlib import Path

ROOT = Path.cwd().resolve()
APP_ICON = ROOT / "packaging" / "windows" / "syncroom.ico"
APP_ICON_PNG = ROOT / "assets" / "syncroom.png"
hiddenimports = [
    "PySide6.QtCore",
    "PySide6.QtGui",
    "PySide6.QtNetwork",
    "PySide6.QtWidgets",
]

a = Analysis(
    [str(ROOT / "src" / "syncroom" / "client.py")],
    pathex=[str(ROOT / "src")],
    binaries=[],
    datas=[(str(APP_ICON_PNG), "assets")],
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[],
    noarchive=False,
)
pyz = PYZ(a.pure)
exe = EXE(
    pyz,
    a.scripts,
    name="SyncRoom",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    console=False,
    icon=str(APP_ICON),
)
u = Analysis(
    [str(ROOT / "src" / "syncroom" / "updater.py")],
    pathex=[str(ROOT / "src")],
    binaries=[],
    datas=[],
    hiddenimports=[
        "PySide6.QtCore",
        "PySide6.QtGui",
        "PySide6.QtWidgets",
    ],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[],
    noarchive=False,
)
updater_pyz = PYZ(u.pure)
updater = EXE(
    updater_pyz,
    u.scripts,
    u.binaries,
    u.datas,
    [],
    name="SyncRoomUpdate",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    console=False,
)
coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    updater,
    strip=False,
    upx=True,
    name="SyncRoom",
)
