# -*- mode: python ; coding: utf-8 -*-


a = Analysis(
    ['__main__.py'],
    pathex=[],
    binaries=[],
    datas=[('bin/ffmpeg.exe', 'bin')],
    hiddenimports=['winrt.windows.foundation.collections', 'paho.mqtt.client', 'csv', 'pathlib', 'heart_rate_logger',
                   'miservice', 'miservice.miaccount', 'miservice.minaservice', 'miservice.miiocommand',
                   'miservice.miioservice', 'miservice.biohttp', 'xiaomi_tts', 'aiohttp'],
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
    name='HRMLink',
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
    version='version.txt',
    icon='icon.ico'
)
