# PyInstaller entry point for the independent Modern portable executable.
from PyInstaller.utils.hooks import collect_all

ctk_datas, ctk_binaries, ctk_hiddenimports = collect_all("customtkinter")

a = Analysis(
    ["gui.py"],
    pathex=["."],
    binaries=ctk_binaries,
    datas=ctk_datas + [("misc/gui/icons/icon.ico", "misc/gui/icons")],
    hiddenimports=ctk_hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[],
    noarchive=False,
    optimize=0,
)
pyz = PYZ(a.pure)
exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.datas,
    [],
    name="BunkrDownloaderPortableGUI_Modern",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    console=False,
    disable_windowed_traceback=False,
    icon="misc/gui/icons/icon.ico",
)
