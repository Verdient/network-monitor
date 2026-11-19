# Build with: python -m PyInstaller --clean --noconfirm network-monitor.spec

a = Analysis(
    ['network_monitor.py'],
    pathex=[],
    binaries=[],
    datas=[],
    hiddenimports=[],
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
    name='network-monitor',
    debug=False,
    strip=False,
    upx=False,
    console=True,
)
