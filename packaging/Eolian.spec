# -*- mode: python ; coding: utf-8 -*-

from pathlib import Path

ROOT = Path(SPECPATH).parent

a = Analysis(
    [str(ROOT / "tools" / "floater.py")],
    pathex=[str(ROOT)],
    binaries=[],
    datas=[
        (str(ROOT / "vendor"), "vendor"),
        (str(ROOT / "data"), "data"),
        (str(ROOT / "resources"), "resources"),
        (str(ROOT / "packaging" / "app.ico"), "packaging"),
        (str(ROOT / "eolian" / "ui" / "web"), "eolian/ui/web"),
    ],
    hiddenimports=[
        "zstandard",
        "webview.platforms.edgechromium",
        "webview.platforms.winforms",
    ],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=["reference", "tests"],
    noarchive=False,
    optimize=1,
)

pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="Eolian",
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
    icon=str(ROOT / "packaging" / "app.ico"),
    version=str(ROOT / "packaging" / "version_info.txt"),
    uac_admin=True,
)

# onedir：vendor/ 与 resources/ 落在固定目录（_internal\vendor\cslol\...），
# 不再每次启动解包到 %TEMP%\_MEIxxxx —— 注入时游戏模块表里的路径也就固定了。
coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=True,
    upx_exclude=[],
    name="Eolian",
)
