# -*- mode: python ; coding: utf-8 -*-
"""
PyInstaller onedir spec for Serenity Desktop App.
Build: scripts/build_desktop.ps1
"""
import sys
from pathlib import Path

ROOT = Path(SPECPATH).parent  # repo root（spec 在 packaging/ 子目錄）

block_cipher = None

a = Analysis(
    [str(ROOT / "packaging" / "desktop_entry.py")],
    pathex=[str(ROOT)],
    binaries=[],
    datas=[
        # 儀表板靜態檔
        (str(ROOT / "dashboard"), "dashboard"),
        # scripts/ 下的 .py 檔（僅 .py，排除 __pycache__）
        (str(ROOT / "scripts"), "scripts"),
        # 量化評分 skill
        (str(ROOT / "skills" / "serenity-stock-scorer"), "skills/serenity-stock-scorer"),
    ],
    hiddenimports=[
        "serenity",
        "serenity.config",
        "serenity.db",
        "serenity.keypool",
        "serenity.gemini",
        "serenity.quant",
        "serenity.app",
        "serenity.background",
        "serenity.desktop",
        "serenity.api",
        "serenity.api.handler",
        "serenity.services",
        "serenity.services.market",
        "serenity.services.signal",
        "serenity.services.regime",
        "serenity.services.hitrate",
        "serenity.services.experts",
        "serenity.services.dossier",
        "serenity.services.scorecard",
        "serenity.services.chat",
        "serenity.services.translate",
        "serenity.services.arena_views",
        "serenity.services.settings",
        "serenity.services.bootstrap",
        "webview",
    ],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[
        # 絕不打包機密與大型資料目錄
        ".env",
        "x_curl",
        "data",
        "docs",
        # 不需要的大型套件
        "matplotlib",
        "numpy",
        "pandas",
        "scipy",
        "PIL",
        "tkinter",
    ],
    win_no_prefer_redirects=False,
    win_private_assemblies=False,
    cipher=block_cipher,
    noarchive=False,
)

pyz = PYZ(a.pure, a.zipped_data, cipher=block_cipher)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="Serenity",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    console=False,  # windowed app（不開 cmd 視窗）
    disable_windowed_traceback=False,
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
    name="Serenity",
)
