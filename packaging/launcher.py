# -*- coding: utf-8 -*-
"""PyInstaller GUI entry point for The Tower PC Bot."""

from __future__ import annotations

import sys
from pathlib import Path


def _self_test() -> int:
    """Hidden smoke test: verify bundled OCR models can load."""
    root = Path(sys.executable).resolve().parent
    out = root / "logs" / "self_test.txt"
    out.parent.mkdir(parents=True, exist_ok=True)
    try:
        from towerbot_pc.ocr import Ocr

        Ocr().engine  # force RapidOCR + ONNX model loading
        out.write_text("OK: OCR engine loaded\n", encoding="utf-8")
        return 0
    except Exception as exc:
        out.write_text(f"FAIL: {exc}\n", encoding="utf-8")
        return 1


def main() -> int:
    if "--self-test" in sys.argv:
        return _self_test()
    from towerbot_pc.gui import main as gui_main

    return gui_main(sys.argv[1:])


if __name__ == "__main__":
    raise SystemExit(main())
