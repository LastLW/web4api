# -*- mode: python ; coding: utf-8 -*-
from PyInstaller.utils.hooks import collect_all

datas = [('D:/Software/AI-OPENAI/webchat2api-main/gateway/static', 'gateway/static'), ('D:/Software/AI-OPENAI/webchat2api-main/services/deepseek4api/deepseek4api.py', 'services/deepseek4api'), ('D:/Software/AI-OPENAI/webchat2api-main/services/doubao4api/doubao4api.py', 'services/doubao4api'), ('D:/Software/AI-OPENAI/webchat2api-main/services/mimo4api/mimo4api.py', 'services/mimo4api'), ('D:/Software/AI-OPENAI/webchat2api-main/services/qwen4api/qwen4api.py', 'services/qwen4api')]
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
    name='web4api',
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
