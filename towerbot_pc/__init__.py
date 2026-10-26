"""The Tower PC automation package."""

import sys
from pathlib import Path

__version__ = "0.1.0"

# 项目内 vendored OpenCV（沙箱/免安装场景）
_vendor = Path(__file__).resolve().parent.parent / ".vendor"
if _vendor.exists() and str(_vendor) not in sys.path:
    sys.path.insert(0, str(_vendor))
