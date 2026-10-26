# -*- mode: python ; coding: utf-8 -*-
"""Build a single-file Windows GUI executable."""

import os

from PyInstaller.utils.hooks import collect_all

root = os.path.abspath(os.path.join(SPECPATH, ".."))
launcher = os.path.join(root, "packaging", "launcher.py")

datas = []
binaries = []
hiddenimports = []
for package in ("rapidocr_onnxruntime", "onnxruntime"):
    pkg_datas, pkg_binaries, pkg_hidden = collect_all(package)
    datas += pkg_datas
    binaries += pkg_binaries
    hiddenimports += pkg_hidden

a = Analysis(
    [launcher],
    pathex=[root],
    binaries=binaries,
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    # RapidOCR 走 onnxruntime，不需要 PyTorch 等训练依赖。
    excludes=["torch", "torchvision", "torchaudio", "matplotlib", "pandas", "scipy"],
    noarchive=False,
)

pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.datas,
    [],
    name="TheTowerBot",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=False,
    disable_windowed_traceback=False,
)
