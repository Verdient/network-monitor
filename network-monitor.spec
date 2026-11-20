# Build with: python -m PyInstaller --clean --noconfirm network-monitor.spec
import sys

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
macos = sys.platform == 'darwin'
exe = EXE(
    pyz,
    a.scripts,
    [] if macos else a.binaries,
    [] if macos else a.datas,
    [],
    name='network-monitor',
    debug=False,
    strip=False,
    upx=False,
    console=True,
    exclude_binaries=macos,
    contents_directory='network-monitor-runtime',
)

if macos:
    bundle = COLLECT(exe, a.binaries, a.datas, name='network-monitor')
