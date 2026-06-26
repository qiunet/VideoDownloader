# -*- mode: python ; coding: utf-8 -*-

import sys
from pathlib import Path

block_cipher = None
project_root = Path(SPECPATH)
icon_ico = str(project_root / 'assets' / 'icon.ico')
icon_icns = str(project_root / 'assets' / 'icon.icns')

ffmpeg_name = 'ffmpeg.exe' if sys.platform == 'win32' else 'ffmpeg'
ffmpeg_path = project_root / 'bin' / ffmpeg_name
extra_binaries = []
if ffmpeg_path.is_file():
    extra_binaries = [(str(ffmpeg_path), 'bin')]

a = Analysis(
    [str(project_root / 'main.py')],
    pathex=[str(project_root)],
    binaries=extra_binaries,
    datas=[(str(project_root / 'assets'), 'assets')],
    hiddenimports=['yt_dlp', 'PyQt6', 'PyQt6.QtCore', 'PyQt6.QtGui', 'PyQt6.QtWidgets'],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[],
    win_no_prefer_redirects=False,
    win_private_assemblies=False,
    cipher=block_cipher,
    noarchive=False,
)

pyz = PYZ(a.pure, a.zipped_data, cipher=block_cipher)

if sys.platform == 'darwin':
    exe = EXE(
        pyz,
        a.scripts,
        [],
        exclude_binaries=True,
        name='VideoDownload',
        debug=False,
        bootloader_ignore_signals=False,
        strip=False,
        upx=True,
        console=False,
        disable_windowed_traceback=False,
        argv_emulation=False,
        target_arch=None,
        codesign_identity=None,
        entitlements_file=None,
    )
    coll = COLLECT(
        exe,
        a.binaries,
        a.zipfiles,
        a.datas,
        strip=False,
        upx=True,
        upx_exclude=[],
        name='VideoDownload',
    )
    app = BUNDLE(
        coll,
        name='VideoDownload.app',
        icon=icon_icns,
        bundle_identifier='com.videodownload.app',
        info_plist={
            'CFBundleName': 'VideoDownload',
            'CFBundleDisplayName': 'VideoDownload',
            'NSHighResolutionCapable': True,
        },
    )
else:
    exe = EXE(
        pyz,
        a.scripts,
        a.binaries,
        a.zipfiles,
        a.datas,
        [],
        name='VideoDownload',
        debug=False,
        bootloader_ignore_signals=False,
        strip=False,
        upx=True,
        upx_exclude=[],
        runtime_tmpdir=None,
        console=False,
        disable_windowed_traceback=False,
        argv_emulation=False,
        target_arch=None,
        codesign_identity=None,
        entitlements_file=None,
        icon=icon_ico,
    )
