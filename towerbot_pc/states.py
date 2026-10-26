# -*- coding: utf-8 -*-
"""游戏状态识别与恢复动作选择。

只做「看到什么」和「该点哪个文字按钮」；实际点击由 Engine 执行。
状态识别优先用几何（标题条），其次用全屏 OCR 的关键词。
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Iterable, Optional

import numpy as np

from .ocr import Ocr, OcrLine
from .vision import Rect, find_title_band


class State(str, Enum):
    IN_GAME = "in_game"
    LEVEL_POPUP = "level_popup"
    RESUME = "resume"
    CLOUD_SAVE = "cloud_save"
    REWARD_MODAL = "reward_modal"
    GAME_OVER = "game_over"
    HOME = "home"
    LOADING = "loading"
    UNKNOWN = "unknown"


# 每个状态的按钮候选（按优先级）。只做精确匹配，避免把“退出战斗”当成“战斗”。
BUTTON_CANDIDATES: dict[State, list[str]] = {
    State.RESUME: ["继续"],
    State.CLOUD_SAVE: ["是", "同意"],
    State.GAME_OVER: ["重试", "再来一次", "重新开始"],
    State.HOME: ["接续战斗", "战斗", "开始战斗"],
}


@dataclass
class Detection:
    state: State
    text: str
    lines: list[OcrLine]
    reason: str = ""


def classify_text(text: str, has_title_band: bool = False) -> State:
    """纯文本分类，方便单测；真实检测还会先看标题条。"""
    compact = text.replace(" ", "")
    if has_title_band:
        return State.IN_GAME
    if "当前等级" in compact and "等级上限" in compact:
        return State.LEVEL_POPUP
    # 注意：主页有“总金币奖励”，不能只匹配“金币奖励”
    if "禁用广告" in compact or "金币1.5倍" in compact:
        return State.REWARD_MODAL
    if "欢迎回来" in compact or "继续先前的回合" in compact:
        return State.RESUME
    if "同意以加载" in compact or ("警告" in compact and "存档" in compact):
        return State.CLOUD_SAVE
    if (any(k in compact for k in ("游戏统计数据", "游戏统计", "游戏结束"))
            or ("重试" in compact and ("主页" in compact or "主菜单" in compact))
            or ("最高波次" in compact and "重试" in compact)):
        return State.GAME_OVER
    if "Loading" in compact or "99.0%" in compact:
        return State.LOADING
    # 主页也有“最高波次：3391”，不能只匹配“波次”
    if "波次" in compact and "最高波次" not in compact:
        return State.IN_GAME
    if "攻击升级" in compact or "防御升级" in compact or "功能升级" in compact:
        return State.IN_GAME
    if "接续战斗" in compact or "失调闯关" in compact:
        return State.HOME
    return State.UNKNOWN


def pick_button(lines: Iterable[OcrLine], labels: list[str],
                min_y: int = 0, max_y: int = 10**9) -> Optional[OcrLine]:
    """在 OCR 行中找精确匹配的按钮文字，优先靠下的（避开标题）。"""
    normalized = [(label.replace(" ", ""), label) for label in labels]
    matches: list[OcrLine] = []
    for line in lines:
        text = line.text.replace(" ", "").strip()
        y = line.center[1]
        if not (min_y <= y <= max_y):
            continue
        for compact, _ in normalized:
            if text == compact:
                matches.append(line)
                break
    if not matches:
        return None
    return max(matches, key=lambda l: l.center[1])


class StateDetector:
    def __init__(self, ocr: Ocr) -> None:
        self.ocr = ocr

    def detect(self, img: np.ndarray, full_ocr: bool = True) -> Detection:
        h = img.shape[0]
        try:
            band = find_title_band(img)
        except ValueError:
            band = None
        if band is not None:
            return Detection(State.IN_GAME, "", [], "title_band")

        if not full_ocr:
            return Detection(State.UNKNOWN, "", [], "no_title_band")

        lines = self.ocr.recognize(img, scale=1.2)
        text = " ".join(l.text for l in lines)
        state = classify_text(text, has_title_band=False)
        if state != State.UNKNOWN:
            return Detection(state, text, lines, "ocr")

        # 全屏 1.2 倍 OCR 可能漏掉弹窗文字；对中央弹窗区域做高倍率兜底。
        h, w = img.shape[:2]
        modal = Rect(int(w * 0.06), int(h * 0.18), int(w * 0.88), int(h * 0.58))
        for scale in (1.5, 2.0):
            modal_lines = self.ocr.recognize(img, modal, scale=scale)
            modal_text = " ".join(l.text for l in modal_lines)
            modal_state = classify_text(modal_text, has_title_band=False)
            if modal_state != State.UNKNOWN:
                return Detection(modal_state, modal_text, modal_lines, f"ocr_modal_{scale}")
        return Detection(State.UNKNOWN, text, lines, "ocr")

    # ------------------------------------------------------------------
    # 恢复动作
    # ------------------------------------------------------------------
    def action_point(self, detection: Detection, frame_w: int, frame_h: int,
                     game_over_action: str = "retry") -> Optional[tuple[int, int]]:
        """根据状态返回应点击的坐标；None 表示应使用返回键/等待。

        game_over_action:
        - "retry"：多局/无限模式，点“重试”继续下一局；
        - "home"：单局模式，点“主页”停留在主页。
        """
        if detection.state == State.LEVEL_POPUP:
            return None
        if detection.state == State.GAME_OVER:
            if game_over_action == "home":
                labels = ["主页", "主菜单"]
            else:
                labels = BUTTON_CANDIDATES[State.GAME_OVER]
        else:
            labels = BUTTON_CANDIDATES.get(detection.state)
        if labels:
            line = pick_button(
                detection.lines, labels,
                min_y=int(frame_h * 0.35), max_y=frame_h,
            )
            if line:
                return line.center
            # 已知对话框的兜底坐标（仅用于简历/云端/结束页）
            if detection.state == State.RESUME:
                return int(frame_w * 0.67), int(frame_h * 0.59)
            if detection.state == State.CLOUD_SAVE:
                return int(frame_w * 0.68), int(frame_h * 0.50)
            if detection.state == State.GAME_OVER and game_over_action == "home":
                return int(frame_w * 0.67), int(frame_h * 0.70)
            if detection.state == State.GAME_OVER:
                return int(frame_w * 0.32), int(frame_h * 0.70)
            if detection.state == State.HOME:
                return int(frame_w * 0.50), int(frame_h * 0.81)
        if detection.state == State.REWARD_MODAL:
            # 已知奖励弹窗右上角 X
            return int(frame_w * 0.843), int(frame_h * 0.317)
        return None
