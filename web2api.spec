# -*- mode: python ; coding: utf-8 -*-
from PyInstaller.utils.hooks import collect_all

datas = [('D:/Software/AI-OPENAI/webchat2api-main/gateway/static', 'gateway/static'), ('D:/Software/AI-OPENAI/webchat2api-main/services/deepseek2api/deepseek2api.py', 'services/deepseek2api'), ('D:/Software/AI-OPENAI/webchat2api-main/services/doubao2api/doubao2api.py', 'services/doubao2api'), ('D:/Software/AI-OPENAI/webchat2api-main/services/mimo2api/mimo2api.py', 'services/mimo2api'), ('D:/Software/AI-OPENAI/webchat2api-main/services/qwen2api/qwen2api.py', 'services/qwen2api')]
binaries = []
hiddenimports = ['uvicorn.loops.auto', 'uvicorn.protocols.http.auto', 'uvicorn.protocols.http.h11_impl', 'uvicorn.protocols.websockets.auto', 'uvicorn.lifespan.on']
tmp_ret = collect_all('playwright')
datas += tmp_ret[0]; binaries += tmp_ret[1]; hiddenimports += tmp_ret[2]
tmp_ret = collect_all('uvicorn')
datas += tmp_ret[0]; binaries += tmp_ret[1]; hiddenimports += tmp_ret[2]


a = Analysis(
    ['D:/Software/AI-OPENAI/webchat2api-main/manager.py'],
    pathex=[],
    binaries=binaries,
    datas=datas,
    hiddenimports=hiddenimports,
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
    name='web2api',
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
)
