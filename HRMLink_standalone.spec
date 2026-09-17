# -*- mode: python ; coding: utf-8 -*-
import os


a = Analysis(
    ['__main__.py'],
    pathex=[],
    binaries=[],
    datas=[],
    hiddenimports=[
        'winrt.windows.foundation.collections', 
        'paho.mqtt.client',
        'PyQt5.QtWinExtras',
        'qasync',
        'bleak',
        'winrt.windows.devices.bluetooth',
        'winrt.windows.devices.bluetooth.advertisement',
        'winrt.windows.devices.bluetooth.genericattributeprofile',
        'winrt.windows.devices.enumeration',
        'winrt.windows.foundation',
        'winrt.windows.storage.streams',
        'datetime',
        'json',
        'threading',
        'csv',
        'pathlib',
        'heart_rate_logger',
        'matplotlib',
        'matplotlib.backends.backend_qt5agg',
        'matplotlib.figure',
        'matplotlib.pyplot',
        'numpy',
        'numpy.core._multiarray_umath',
        'PIL',
        'PIL._imaging'
    ],
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
    icon=None
)
