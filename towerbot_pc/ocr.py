# -*- coding: utf-8 -*-
"""OCR 封装：RapidOCR + 数值解析。

只识别数字/倍率；属性名不参与识别，避免字体与语言误差。
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Optional

import cv2
import numpy as np

from .vision import Rect, crop


@dataclass
class OcrLine:
    text: str
    confidence: float
    box: list[list[int]]

    @property
    def center(self) -> tuple[int, int]:
        xs = [p[0] for p in self.box]
        ys = [p[1] for p in self.box]
        return sum(xs) // len(xs), sum(ys) // len(ys)


class Ocr:
    def __init__(self) -> None:
        self._engine = None

    @property
    def engine(self):
        if self._engine is None:
            from rapidocr_onnxruntime import RapidOCR

            self._engine = RapidOCR()
        return self._engine

    def recognize(self, img: np.ndarray, rect: Optional[Rect] = None,
                  scale: float = 2.0) -> list[OcrLine]:
        target = crop(img, rect) if rect else img
        if target.size == 0:
            return []
        if scale != 1.0:
            target = cv2.resize(
                target, None, fx=scale, fy=scale, interpolation=cv2.INTER_CUBIC
            )
        result, _ = self.engine(target)
        lines: list[OcrLine] = []
        for box, text, conf in result or []:
            pts = [[int(x / scale), int(y / scale)] for x, y in box]
            if rect:
                pts = [[x + rect.x, y + rect.y] for x, y in pts]
            lines.append(OcrLine(str(text).strip(), float(conf), pts))
        return lines

    def read_text(self, img: np.ndarray, rect: Optional[Rect] = None,
                  scale: float = 2.0) -> str:
        return " ".join(line.text for line in self.recognize(img, rect, scale))


_NUM_RE = re.compile(r"[-+]?\d[\d,]*(?:\.\d+)?\s*[KMBTkmbt]?")
_PRICE_TOKEN_RE = re.compile(r"(\d[\d,]*(?:\.\d+)?)\s*([KMBT])?", re.I)
_MULT_RE = re.compile(r"[xX×]\s*(\d+(?:\.\d+)?)")

_SCALE = {"": 1.0, "K": 1e3, "M": 1e6, "B": 1e9, "T": 1e12}


def parse_price(text: str) -> Optional[float]:
    """解析价格，优先取带 K/M/B/T 的完整数字。

    OCR 可能把 ``$ 828.65K`` 误读成 ``$8 828.65K``；
    旧逻辑取第一个数字会得到 8，这里改为优先取带单位的最大数字。
    """
    if not text:
        return None
    candidates: list[tuple[float, bool]] = []
    for match in _PRICE_TOKEN_RE.finditer(text):
        raw = match.group(1).replace(",", "")
        unit = (match.group(2) or "").upper()
        try:
            value = float(raw) * _SCALE.get(unit, 1.0)
        except ValueError:
            continue
        candidates.append((value, bool(unit)))
    if not candidates:
        return None
    suffixed = [value for value, has_unit in candidates if has_unit]
    if suffixed:
        return max(suffixed)
    return candidates[-1][0]


def parse_quantity(text: str) -> Optional[float]:
    """把 ``$ 625.18K`` / ``7.71K`` / ``4.46%`` 解析成数值。"""
    if not text:
        return None
    match = _NUM_RE.search(text.replace("$", " ").replace("%", " "))
    if not match:
        return None
    raw = match.group(0).replace(",", "").strip()
    unit = raw[-1].upper() if raw[-1].isalpha() else ""
    if unit:
        raw = raw[:-1].strip()
    try:
        value = float(raw)
    except ValueError:
        return None
    return value * {"": 1, "K": 1e3, "M": 1e6, "B": 1e9, "T": 1e12}.get(unit, 1)


def parse_level(text: str) -> Optional[int]:
    if not text:
        return None
    match = re.search(r"\d+", text.replace(",", ""))
    return int(match.group(0)) if match else None


def parse_multiplier(text: str) -> Optional[int]:
    if not text:
        return None
    match = _MULT_RE.search(text)
    if not match:
        return None
    value = float(match.group(1))
    return int(value) if value >= 1 else None


def read_level(
    ocr: Ocr,
    img: np.ndarray,
    region: Rect,
    tight: Optional[Rect] = None,
) -> Optional[int]:
    """等级弹窗中的当前等级。

    RapidOCR 对同一数字在不同缩放/裁剪下会给出 91 / L6 两种结果；
    这里只接受纯数字，多尺度投票，避免把 L6 当成 6。
    """
    rects = [region]
    if tight:
        rects.append(tight)
    candidates: list[tuple[int, float]] = []
    for rect in rects:
        for scale in (2.0, 3.0, 4.0):
            for line in ocr.recognize(img, rect, scale=scale):
                text = line.text.strip().replace(" ", "")
                if text.isdigit() and len(text) <= 6:
                    candidates.append((int(text), float(line.confidence)))
    if not candidates:
        return None
    # 多数票优先；票数相同取置信度高者
    counts: dict[int, list[float]] = {}
    for value, conf in candidates:
        counts.setdefault(value, []).append(conf)
    return max(counts, key=lambda v: (len(counts[v]), max(counts[v])))
