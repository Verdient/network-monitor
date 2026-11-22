# Build with: python -m PyInstaller --clean --noconfirm network-monitor.spec
import certifi

a = Analysis(
    ['network_monitor.py'],
    pathex=[],
    binaries=[],
    datas=[('geo_data/territories.zh.json', 'geo_data'),
           ('geo_data/cities.zh.json', 'geo_data'),
           ('geo_data/LICENSE.unicode', 'geo_data'),
           ('geo_data/opencc', 'geo_data/opencc'),
           (certifi.where(), 'geo_data')],
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
    [],
    name='network-monitor',
    debug=False,
    strip=False,
    upx=False,
    console=True,
    exclude_binaries=True,
    contents_directory='network-monitor-runtime',
)

bundle = COLLECT(exe, a.binaries, a.datas, name='network-monitor')
