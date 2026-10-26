# -*- coding: utf-8 -*-
"""视觉：截图解码、升级面板定位、卡片/页签/坐标推导。

v1 只做几何定位；数值识别在 ocr.py。
布局参数集中在 Layout 中，换分辨率只需重新校准/改比例。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

import cv2
import numpy as np


@dataclass
class Rect:
    x: int
    y: int
    w: int
    h: int

    @property
    def center(self) -> tuple[int, int]:
        return self.x + self.w // 2, self.y + self.h // 2

    @property
    def right(self) -> int:
        return self.x + self.w

    @property
    def bottom(self) -> int:
        return self.y + self.h

    def as_list(self) -> list[int]:
        return [self.x, self.y, self.w, self.h]


@dataclass
class Layout:
    frame_w: int
    frame_h: int
    title_band: Rect
    cards: list[Rect] = field(default_factory=list)
    tabs: dict[str, tuple[int, int]] = field(default_factory=dict)
    hud_cash: Optional[Rect] = None
    hud_gold: Optional[Rect] = None
    multiplier: Optional[Rect] = None
    level_popup: Optional[Rect] = None
    level_number: Optional[Rect] = None
    popup_region: Optional[Rect] = None


def decode_png(data: bytes) -> np.ndarray:
    arr = np.frombuffer(data, dtype=np.uint8)
    img = cv2.imdecode(arr, cv2.IMREAD_COLOR)
    if img is None:
        raise ValueError("PNG 解码失败")
    return img


def load_image(path: str | Path) -> np.ndarray:
    p = Path(path)
    data = np.frombuffer(p.read_bytes(), dtype=np.uint8)
    img = cv2.imdecode(data, cv2.IMREAD_COLOR)
    if img is None:
        raise ValueError(f"图片解码失败: {p}")
    return img


def crop(img: np.ndarray, rect: Rect) -> np.ndarray:
    h, w = img.shape[:2]
    x1 = max(0, min(w, rect.x))
    y1 = max(0, min(h, rect.y))
    x2 = max(x1, min(w, rect.right))
    y2 = max(y1, min(h, rect.bottom))
    return img[y1:y2, x1:x2].copy()


def find_title_band(img: np.ndarray) -> Rect:
    """找升级面板标题条：屏幕下半部、横跨大部分宽度的青蓝/红粉色条。"""
    h, w = img.shape[:2]
    hsv = cv2.cvtColor(img, cv2.COLOR_BGR2HSV)
    hue, sat, val = cv2.split(hsv)

    cyan = (hue >= 80) & (hue <= 105) & (sat > 110) & (val > 120)
    red = (((hue >= 155) & (hue <= 179)) | (hue <= 8)) & (sat > 110) & (val > 110)
    # 功能升级页是黄色/金色标题条
    yellow = (hue >= 15) & (hue <= 40) & (sat > 110) & (val > 120)
    mask = (cyan | red | yellow).astype(np.uint8)

    # 只统计下半屏
    start = int(h * 0.45)
    row_counts = mask[start:].sum(axis=1)
    threshold = max(40, int(w * 0.55))
    rows = np.where(row_counts >= threshold)[0] + start
    if len(rows) == 0:
        raise ValueError("找不到升级面板标题条（请确认在游戏局内且升级面板可见）")

    groups: list[list[int]] = []
    current = [int(rows[0])]
    for y in rows[1:]:
        y = int(y)
        if y <= current[-1] + 3:
            current.append(y)
        else:
            groups.append(current)
            current = [y]
    groups.append(current)

    best = None
    best_score = -1
    for group in groups:
        y1, y2 = group[0], group[-1]
        height = y2 - y1 + 1
        if not (30 <= height <= 180):
            continue
        band = mask[y1:y2 + 1]
        coverage = float(band.any(axis=0).mean())
        if coverage < 0.7:
            continue
        score = coverage * height
        if score > best_score:
            best_score = score
            best = Rect(0, y1, w, height)
    if best is None:
        raise ValueError("找到彩色横带，但尺寸/覆盖率不像标题条")

    # 向下扩展几像素，覆盖标题条下沿
    best.h = min(h - best.y, best.h + 6)
    return best


def band_color(img: np.ndarray, band: Rect) -> tuple[float, float, float]:
    """返回标题条的平均 BGR。"""
    patch = crop(img, band)
    if patch.size == 0:
        return (0.0, 0.0, 0.0)
    mean = patch.reshape(-1, 3).mean(axis=0)
    return float(mean[0]), float(mean[1]), float(mean[2])


def guess_tab(img: np.ndarray, band: Optional[Rect] = None) -> str:
    """按标题条颜色判断当前页签：青蓝=攻击，红=防御，黄=功能。"""
    band = band or find_title_band(img)
    b, g, r = band_color(img, band)
    # 功能页金色：G、R 都明显高于 B
    if g > b * 1.5 and r > b * 1.5:
        return "utility"
    if b > r * 1.45:
        return "attack"
    if r > b * 1.45:
        return "defense"
    return "utility"


def build_layout(img: np.ndarray, band: Optional[Rect] = None) -> Layout:
    h, w = img.shape[:2]
    band = band or find_title_band(img)

    # 基于 v29.1.1 1080x2400 实测比例；按宽/高分别缩放。
    card_top = band.bottom + round(0.00625 * h)
    card_w = round(0.464 * w)
    card_h = round(0.075 * h)
    x0 = round(0.0287 * w)
    col_pitch = round(0.4778 * w)
    row_pitch = round(0.0871 * h)

    cards: list[Rect] = []
    for row in range(3):
        for col in range(2):
            cards.append(
                Rect(x0 + col * col_pitch, card_top + row * row_pitch, card_w, card_h)
            )

    tabs = {
        "attack": (round(0.102 * w), round(0.967 * h)),
        "defense": (round(0.328 * w), round(0.967 * h)),
        "utility": (round(0.561 * w), round(0.967 * h)),
        "shop": (round(0.954 * w), round(0.967 * h)),
    }

    layout = Layout(
        frame_w=w,
        frame_h=h,
        title_band=band,
        cards=cards,
        tabs=tabs,
        hud_cash=Rect(round(0.018 * w), round(0.008 * h), round(0.32 * w), round(0.038 * h)),
        hud_gold=Rect(round(0.018 * w), round(0.042 * h), round(0.32 * w), round(0.038 * h)),
        multiplier=Rect(
            round(0.80 * w), band.y,
            round(0.19 * w), band.h,
        ),
        level_popup=Rect(round(0.57 * w), round(0.425 * h), round(0.16 * w), round(0.05 * h)),
        level_number=Rect(round(0.64 * w), round(0.445 * h), round(0.10 * w), round(0.035 * h)),
        popup_region=Rect(round(0.10 * w), round(0.21 * h), round(0.80 * w), round(0.29 * h)),
    )
    return layout


def card_points(card: Rect) -> tuple[tuple[int, int], tuple[int, int]]:
    """返回 (购买点, 等级弹窗入口点)。

    购买点取价格框中心（实测价格框在卡片内 y≈0.72..0.86）。
    等级入口取属性名区域（左侧，y≈0.42）。
    """
    buy = (card.x + int(card.w * 0.77), card.y + int(card.h * 0.80))
    info = (card.x + int(card.w * 0.17), card.y + int(card.h * 0.52))
    return buy, info


def draw_layout(img: np.ndarray, layout: Layout) -> np.ndarray:
    out = img.copy()
    cv2.rectangle(
        out,
        (layout.title_band.x, layout.title_band.y),
        (layout.title_band.right, layout.title_band.bottom),
        (0, 255, 255), 3,
    )
    for idx, card in enumerate(layout.cards):
        cv2.rectangle(out, (card.x, card.y), (card.right, card.bottom), (0, 255, 0), 2)
        buy, info = card_points(card)
        cv2.circle(out, buy, 7, (0, 0, 255), -1)
        cv2.circle(out, info, 7, (255, 0, 255), -1)
        cv2.putText(
            out, f"{idx}", (card.x + 8, card.y + 28),
            cv2.FONT_HERSHEY_SIMPLEX, 0.8, (255, 255, 255), 2,
        )
    for name, (x, y) in layout.tabs.items():
        cv2.circle(out, (x, y), 10, (255, 255, 0), 2)
        cv2.putText(out, name, (x - 30, y - 18), cv2.FONT_HERSHEY_SIMPLEX, 0.5, (255, 255, 0), 1)
    for rect, color in ((layout.hud_cash, (0, 165, 255)), (layout.hud_gold, (0, 255, 0)),
                        (layout.multiplier, (255, 0, 0)), (layout.level_popup, (255, 255, 0))):
        if rect:
            cv2.rectangle(out, (rect.x, rect.y), (rect.right, rect.bottom), color, 2)
    return out
