# -*- mode: python ; coding: utf-8 -*-


a = Analysis(
    ['main.py'],
    pathex=[],
    binaries=[],
    datas=[],
    hiddenimports=['diskmap', 'disktrend', 'sysreclaim', 'software_manager', 'registry_cleaner', 'startup_manager', 'hardware_info', 'network_tools', 'shredder', 'popup_blocker', 'health', 'drivers', 'virtual_devices', 'file_transfer', 'ws_server', 'link_service', 'mic_receiver', 'qrcode_gen', 'auto_clean_runner', 'upnp_nat', 'page_diskmap', 'page_disktrend', 'page_software', 'page_startup', 'page_registry', 'page_shredder', 'page_popup', 'page_systeminfo', 'page_drivers', 'page_virtual', 'page_transfer', 'page_link'],
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
    name='C盘清理大师',
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
    uac_admin=True,
)
