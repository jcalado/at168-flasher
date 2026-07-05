# -*- mode: python ; coding: utf-8 -*-
# PyInstaller spec — builds a single self-contained executable for the
# host OS. Run on each target platform (Windows/Linux/macOS) since
# PyInstaller cannot cross-compile:
#
#     pyinstaller at168-flasher.spec
#
# Output lands in dist/AT168-Flasher (or dist/AT168-Flasher.exe on Windows).

a = Analysis(
    ['at168-flasher-gui.py'],
    pathex=[],
    binaries=[],
    datas=[],
    # pyserial selects a platform-specific list_ports backend at import
    # time; make sure PyInstaller pulls in all of them.
    hiddenimports=[
        'serial.tools.list_ports_linux',
        'serial.tools.list_ports_windows',
        'serial.tools.list_ports_osx',
    ],
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
    a.binaries,
    a.datas,
    [],
    name='AT168-Flasher',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    runtime_tmpdir=None,
    console=False,          # windowed app — no terminal window
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)
