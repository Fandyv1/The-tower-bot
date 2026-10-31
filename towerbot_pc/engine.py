# -*- coding: utf-8 -*-
"""购买引擎：按有序策略读取等级、判断金币、点击购买、校验结果。

v1 的安全边界：
- 只点击已识别的页签、价格框、属性名入口；
- 不认识的状态（主页/弹窗/游戏结束）直接停止，不盲点；
- 每次购买后必须看到价格变化，否则计入失败；
- 支持 dry-run，只读不买。
"""

from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

import numpy as np

from .adb import Adb, AdbError
from .catalog import display_name, slot_count
from .config import log_dir
from .ocr import Ocr, parse_multiplier, parse_price, parse_quantity, read_level as ocr_level
from .states import Detection, State, StateDetector
from .strategy import (
    MODE_ABSOLUTE,
    Loop,
    Step,
    plan_from_config,
)
from .vision import (
    Layout,
    Rect,
    build_layout,
    card_points,
    decode_png,
    find_title_band,
    guess_tab,
)

LOG = logging.getLogger("towerbot")


class EngineError(RuntimeError):
    pass


class RuntimeLimit(EngineError):
    pass


class Cancelled(EngineError):
    pass


class RunEnded(EngineError):
    """本局已结束（Game Over / 回到主页），由外层多局循环处理。"""


@dataclass
class StepResult:
    step: Step
    level: int
    purchases: int
    completed: bool
    message: str
    goal: int = 0
    maxed: bool = False


@dataclass
class RunResult:
    index: int
    results: list[StepResult]
    completed: bool
    message: str


def setup_logging() -> Path:
    log_path = log_dir() / "engine.log"
    LOG.setLevel(logging.INFO)
    if not LOG.handlers:
        fmt = logging.Formatter("%(asctime)s %(levelname)s %(message)s", "%H:%M:%S")
        fh = logging.FileHandler(log_path, encoding="utf-8")
        fh.setFormatter(fmt)
        sh = logging.StreamHandler()
        sh.setFormatter(fmt)
        LOG.addHandler(fh)
        LOG.addHandler(sh)
    return log_path


class Engine:
    def __init__(self, cfg: dict, adb: Adb, dry_run: bool = False) -> None:
        self.cfg = cfg
        self.adb = adb
        self.dry_run = dry_run
        self.ocr = Ocr()
        self.delays = cfg.get("delays", {})
        self.limits = cfg.get("limits", {})
        self.failures = 0
        self.purchases = 0
        self.max_failures = int(self.limits.get("max_consecutive_failures", 8))
        self.max_purchases = int(self.limits.get("max_purchases_per_step", 500))
        self.detector = StateDetector(self.ocr)
        self.deadline: Optional[float] = None
        self.last_detection: Optional[Detection] = None
        self.stop_event = threading.Event()
        self.pause_event = threading.Event()
        self._pause_started: Optional[float] = None
        self.game_over_action = "retry"  # single 模式改为 home
        self.current_results: list[StepResult] = []

    def stop(self) -> None:
        self.stop_event.set()

    @property
    def is_paused(self) -> bool:
        return self.pause_event.is_set()

    def pause(self) -> None:
        if not self.pause_event.is_set():
            self.pause_event.set()
            LOG.info("引擎暂停")

    def resume(self) -> None:
        if self.pause_event.is_set():
            self.pause_event.clear()
            LOG.info("引擎继续")

    def _check_pause(self) -> None:
        if not self.pause_event.is_set():
            return
        self._pause_started = time.monotonic()
        LOG.info("已暂停，等待继续")
        while self.pause_event.is_set():
            if self.stop_event.wait(0.2):
                raise Cancelled("暂停期间收到停止请求")
        if self._pause_started and self.deadline is not None:
            self.deadline += time.monotonic() - self._pause_started
        self._pause_started = None

    # ------------------------------------------------------------------
    # 基础读取
    # ------------------------------------------------------------------
    def screenshot(self) -> np.ndarray:
        return decode_png(self.adb.screencap())

    def capture(self) -> tuple[np.ndarray, Layout]:
        img = self.screenshot()
        try:
            layout = build_layout(img)
        except ValueError as exc:
            # Game Over 弹窗会遮住升级面板标题条，build_layout 会报
            # “找到彩色横带，但尺寸/覆盖率不像标题条”。先识别状态，
            # 若是本局结束就按单局/多局规则处理，避免冒泡成未预期异常。
            det = self.detector.detect(img)
            self.last_detection = det
            if det.state in (State.GAME_OVER, State.HOME):
                self._raise_run_ended(det, img)
            raise EngineError(f"界面布局识别失败: {exc}") from exc
        return img, layout

    def _raise_run_ended(self, det: Detection,
                         img: Optional[np.ndarray] = None) -> None:
        """本局结束：单局点主页后抛 RunEnded；多局交给外层重开。"""
        if det.state == State.GAME_OVER and self.game_over_action == "home":
            frame = img if img is not None else self.screenshot()
            h, w = frame.shape[:2]
            point = self.detector.action_point(
                det, w, h, game_over_action="home",
            )
            if point is not None:
                LOG.info("单局结束 → 点击主页 %s", point)
                self.adb.tap(*point)
                time.sleep(2.5)
        raise RunEnded(f"本局结束: {det.state.value}")

    def _sleep(self, key: str, default: float) -> None:
        self._check_pause()
        self._check_deadline()
        if self.stop_event.wait(float(self.delays.get(key, default))):
            raise Cancelled("收到停止请求")

    def _pace(self, started: float) -> None:
        """保证每次购买循环至少间隔 action_interval 秒；OCR 较慢时不会额外拖慢。"""
        interval = float(self.delays.get("action_interval", 1.0))
        remain = interval - (time.monotonic() - started)
        if remain <= 0:
            return
        self._check_pause()
        self._check_deadline()
        if self.stop_event.wait(remain):
            raise Cancelled("收到停止请求")

    def _check_deadline(self) -> None:
        if self.stop_event.is_set():
            raise Cancelled("收到停止请求")
        if self.deadline is not None and time.time() >= self.deadline:
            raise RuntimeLimit("达到最大运行时长，安全退出")

    def read_multiplier(self, img: np.ndarray, layout: Layout) -> int:
        if not layout.multiplier:
            return 1
        texts = []
        for scale in (2.0, 3.0):
            text = self.ocr.read_text(img, layout.multiplier, scale=scale)
            texts.append(text)
            value = parse_multiplier(text)
            if value is not None:
                return value
        LOG.warning("倍率识别失败（%r），按 1x 处理", texts)
        return 1

    def read_cash(self, img: np.ndarray, layout: Layout) -> Optional[float]:
        if not layout.hud_cash:
            return None
        text = self.ocr.read_text(img, layout.hud_cash, scale=2.0)
        value = parse_quantity(text)
        if value is not None and value >= 1000:
            return value
        # 低额或疑似切掉 M 后缀时再做一次高倍率 OCR
        text2 = self.ocr.read_text(img, layout.hud_cash, scale=3.0)
        value2 = parse_quantity(text2)
        if value is None:
            return value2
        if value2 is None:
            return value
        return max(value, value2)

    def price_region(self, card: Rect) -> Rect:
        # 必须留足上下留白：裁太紧会把 $ 切成 S（尤其早期小额价格字体更小）
        return Rect(
            card.x + int(card.w * 0.45),
            card.y + int(card.h * 0.48),
            int(card.w * 0.54),
            int(card.h * 0.42),
        )

    def is_maxed(self, img: np.ndarray, card: Rect) -> bool:
        """价格框显示“上限”时，该属性已无法继续购买。"""
        region = self.price_region(card)
        text = self.ocr.read_text(img, region, scale=2.0).replace(" ", "")
        return "上限" in text

    def read_price(self, img: np.ndarray, card: Rect) -> Optional[float]:
        """读取价格；OCR 偶尔会丢掉 ``$``，此时仍按数字/K/M 解析。

        先用紧凑价格框，失败后再用整张卡片下半部作为兜底。
        """
        tight = self.price_region(card)
        wide = Rect(
            card.x + int(card.w * 0.08),
            card.y + int(card.h * 0.50),
            int(card.w * 0.84),
            int(card.h * 0.45),
        )
        for region, scale in ((tight, 2.0), (wide, 2.0), (tight, 3.0)):
            lines = self.ocr.recognize(img, region, scale=scale)
            lines.sort(key=lambda l: (min(p[1] for p in l.box), min(p[0] for p in l.box)))
            text = " ".join(l.text for l in lines)
            value = parse_price(text)
            if value is not None:
                return value
        return None

    def _panel_lines(self, img: np.ndarray, layout: Layout) -> list:
        region = Rect(0, layout.title_band.bottom + 10, layout.frame_w,
                      layout.frame_h - layout.title_band.bottom - 10)
        return self.ocr.recognize(img, region, scale=1.2)

    @staticmethod
    def _find_label(lines: list, label: str, min_y: int, max_y: int):
        target = label.replace(" ", "").strip()
        matches = [
            line for line in lines
            if line.text.replace(" ", "").strip() == target
            and min_y <= line.center[1] <= max_y
        ]
        return max(matches, key=lambda l: l.center[1]) if matches else None

    def _card_from_label(self, line, layout: Layout):
        cx, cy = line.center
        left = cx < layout.frame_w // 2
        card = Rect(
            layout.cards[0].x if left else layout.cards[1].x,
            cy - 85,
            layout.cards[0].w,
            layout.cards[0].h,
        )
        info = (cx, cy)
        buy = (416 if left else 932, cy + 47)
        return card, info, buy

    def _safe_label_y(self, layout: Layout) -> tuple[int, int]:
        # 低于页签的卡片不可点；高于标题条的会被遮住
        return layout.title_band.bottom + 30, int(layout.frame_h * 0.92)

    def _slow_scroll(self, direction: str, distance: int = 110) -> None:
        """慢速拖动，避免惯性一次跳两行。direction=down 表示看更下面的属性。"""
        self._check_deadline()
        if direction == "down":
            self.adb.swipe(540, 2150, 540, 2150 - distance, 900)
        else:
            self.adb.swipe(540, 1850, 540, 1850 + distance, 900)
        time.sleep(0.55)

    def scroll_to_top(self, tab: str, attempts: int = 5) -> None:
        label = display_name(tab, 0)
        for i in range(attempts):
            try:
                img, layout = self.capture()
            except ValueError:
                # 面板可能被误触收起：点页签重新打开后继续
                LOG.warning("回顶时升级面板不可见，重新打开")
                x, y = self._tab_point(tab)
                self.adb.tap(x, y)
                time.sleep(0.7)
                continue
            lo, hi = self._safe_label_y(layout)
            line = self._find_label(self._panel_lines(img, layout), label, lo, hi)
            if line is not None:
                if tab not in ("attack", "defense"):
                    return
                # 属性名可见不代表面板在顶部：滚动后名称可能上移，
                # 此时固定槽位坐标会点到别的卡。要求名称回到第一张卡附近。
                expected = layout.cards[0].y + int(layout.cards[0].h * 0.50)
                if abs(line.center[1] - expected) <= 70:
                    return
                LOG.info("回顶校正「%s」位置 %s → 目标 %s", label,
                         line.center[1], expected)
                if line.center[1] < expected:
                    self.adb.swipe(540, 1850, 540, 2150, 500)
                else:
                    self.adb.swipe(540, 2150, 540, 1850, 500)
                time.sleep(0.55)
                continue
            LOG.info("回顶滚动 %s/%s", i + 1, attempts)
            # 终点不要落到最底部页签，否则会被游戏识别为切换页签
            self.adb.swipe(540, 1850, 540, 2150, 500)
            time.sleep(0.55)
        LOG.warning("回顶后仍未看到「%s」，继续按当前位置搜索", label)

    @staticmethod
    def _tab_point(tab: str) -> tuple[int, int]:
        points = {
            "attack": (110, 2320),
            "defense": (354, 2320),
            "utility": (606, 2320),
        }
        return points.get(tab, (354, 2320))

    def locate_card(self, tab: str, slot: int) -> tuple[Rect, tuple[int, int], tuple[int, int]]:
        """返回 (卡片区域, 等级入口, 购买点)。

        前 4 格用固定几何；第 5 格起滚动，用 OCR 找属性名后再推导购买点。
        """
        img, layout = self.capture()
        if not 0 <= slot < slot_count(tab):
            raise EngineError(f"slot 超出范围: {tab}#{slot}")
        if tab in ("attack", "defense") and slot <= 3:
            card = layout.cards[slot]
            buy, info = card_points(card)
            return card, info, buy

        label = display_name(tab, slot)
        lo, hi = self._safe_label_y(layout)
        line = self._find_label(self._panel_lines(img, layout), label, lo, hi)
        if line is not None:
            return self._card_from_label(line, layout)

        LOG.info("属性「%s」不在当前视野，回顶后逐步滚动查找", label)
        self.scroll_to_top(tab)
        for step in range(14):
            img, layout = self.capture()
            lo, hi = self._safe_label_y(layout)
            line = self._find_label(self._panel_lines(img, layout), label, lo, hi)
            if line is not None:
                LOG.info("滚动 %s 步找到「%s」: %s", step, label, line.center)
                return self._card_from_label(line, layout)
            self._slow_scroll("down")
        self._save_failure(f"scroll_not_found_{tab}_{slot}", img)
        raise EngineError(f"滚动后仍找不到属性「{label}」")

    def _popup_attribute_matches(self, img: np.ndarray, layout: Layout,
                                 tab: str, slot: int) -> bool:
        """确认等级弹窗确实是目标属性，避免把别的卡等级当成当前卡。"""
        expected = display_name(tab, slot).replace(" ", "").strip()
        try:
            lines = self.ocr.recognize(img, layout.popup_region, scale=1.5)
        except Exception:
            return True
        texts = {l.text.replace(" ", "").strip() for l in lines}
        known = {
            display_name(name, i).replace(" ", "").strip()
            for name in ("attack", "defense", "utility")
            for i in range(slot_count(name))
        }
        found = {name for name in known if name in texts}
        if expected in found:
            return True
        if found:
            LOG.warning("等级弹窗属性不匹配: 期望「%s」，识别到 %s",
                        expected, "、".join(sorted(found)))
            return False
        # 名称 OCR 没读出来时不拦，继续用等级数字投票。
        return True

    def read_level(self, tab: str, slot: int) -> int:
        """打开等级弹窗读取真实等级；属性不匹配时回顶重试一次。"""
        card, info, _ = self.locate_card(tab, slot)
        _, layout = self.capture()
        for attempt in range(1, 3):
            LOG.info("读取等级: 点击属性名 %s（第 %s 次）", info, attempt)
            self.adb.tap(*info)
            self._sleep("after_popup_open", 0.55)
            img2 = decode_png(self.adb.screencap())
            if not self._level_popup_open(img2, layout):
                # 弹窗消失可能是本局刚好结束；先看状态，绝不能盲点 Game Over 页面。
                det = self.detector.detect(img2)
                self.last_detection = det
                if det.state in (State.GAME_OVER, State.HOME):
                    LOG.warning("等级弹窗期间本局结束: %s", det.state.value)
                    self._raise_run_ended(det, img2)
                # 仍确认是局内，才重试点击属性名
                LOG.warning("等级弹窗未出现，重试一次")
                self.adb.tap(*info)
                self._sleep("after_popup_open", 0.75)
                img2 = decode_png(self.adb.screencap())

            if not self._level_popup_open(img2, layout):
                det = self.detector.detect(img2)
                self.last_detection = det
                if det.state in (State.GAME_OVER, State.HOME):
                    LOG.warning("等级弹窗期间本局结束: %s", det.state.value)
                    self._raise_run_ended(det, img2)
                self._save_failure("level_popup_failed", img2)
                raise EngineError("等级弹窗识别失败；已停止，避免误点")

            if not self._popup_attribute_matches(img2, layout, tab, slot):
                self._save_failure("level_wrong_attribute", img2)
                self.adb.keyevent(4)
                self._sleep("after_popup_close", 0.55)
                if attempt == 1:
                    LOG.warning("等级弹窗属性错误，回顶重新定位后重试")
                    self.scroll_to_top(tab)
                    card, info, _ = self.locate_card(tab, slot)
                    _, layout = self.capture()
                    continue
                raise EngineError("等级弹窗属性不匹配；已停止，避免误读")

            level = ocr_level(self.ocr, img2, layout.level_popup, layout.level_number)
            if level is None:
                self._save_failure("level_popup_failed", img2)
                raise EngineError("等级弹窗已打开但读不到等级；已停止")
            self.adb.keyevent(4)
            self._sleep("after_popup_close", 0.45)
            # 返回键偶尔会被动画吞掉；确认弹窗真的关掉，避免后续截图被遮罩
            for _ in range(2):
                img3 = decode_png(self.adb.screencap())
                if not self._level_popup_open(img3, layout):
                    break
                LOG.warning("等级弹窗仍在，再按一次返回")
                self.adb.keyevent(4)
                self._sleep("after_popup_close", 0.60)
            return level

        raise EngineError("等级弹窗属性连续不匹配；已停止")

    def _level_popup_open(self, img: np.ndarray, layout: Layout) -> bool:
        """必须看到“当前等级/等级上限”才算弹窗，避免把局内文字误判。"""
        region = layout.popup_region
        if region is None:
            return False
        text = self.ocr.read_text(img, region, scale=1.5).replace(" ", "")
        return ("当前等级" in text) or ("等级上限" in text)

    def _safe_band(self, img: np.ndarray) -> Optional[Rect]:
        """找标题条；失败时先识别状态，Game Over/主页转成本局结束。"""
        try:
            return find_title_band(img)
        except ValueError:
            det = self.detector.detect(img)
            self.last_detection = det
            if det.state in (State.GAME_OVER, State.HOME):
                self._raise_run_ended(det, img)
            return None

    def switch_tab(self, tab: str) -> None:
        """确保升级面板打开且位于目标页签。

        注意：点击当前已激活的页签会收起面板，所以不能盲点。
        """
        img = self.screenshot()
        band = self._safe_band(img)

        if band is None:
            LOG.info("升级面板未打开，点击页签打开: %s", tab)
            layout = build_layout(img, Rect(0, int(img.shape[0] * 0.68), img.shape[1], 100))
            self.adb.tap(*layout.tabs[tab])
            self._sleep("after_tab", 0.65)
            img = self.screenshot()
            band = self._safe_band(img)
            if band is None:
                raise EngineError("打开升级面板失败：未找到标题条")
            current = guess_tab(img, band)
            if current != tab:
                self.adb.tap(*layout.tabs[tab])
                self._sleep("after_tab", 0.55)
                img = self.screenshot()
                band = self._safe_band(img)
                if band is None:
                    raise EngineError("切换页签失败：未找到标题条")
                current = guess_tab(img, band)
            LOG.info("当前页签: %s（目标 %s）", current, tab)
            if current != tab:
                raise EngineError(f"切换页签失败: 目标 {tab}，当前 {current}")
            return

        current = guess_tab(img, band)
        LOG.info("当前页签: %s（目标 %s）", current, tab)
        if current == tab:
            return
        layout = build_layout(img, band)
        self.adb.tap(*layout.tabs[tab])
        self._sleep("after_tab", 0.55)
        img = self.screenshot()
        band = self._safe_band(img)
        if band is None:
            raise EngineError("切换页签失败：未找到标题条")
        current = guess_tab(img, band)
        if current != tab:
            raise EngineError(f"切换页签失败: 目标 {tab}，当前 {current}")

    def _save_failure(self, tag: str, img: np.ndarray) -> Path:
        import cv2

        path = log_dir() / f"fail_{tag}_{int(time.time())}.png"
        cv2.imwrite(str(path), img)
        LOG.error("失败截图: %s", path)
        return path

    # ------------------------------------------------------------------
    # 状态恢复
    # ------------------------------------------------------------------
    def ensure_foreground(self) -> None:
        package = self.cfg["adb"]["package"]
        fg = self.adb.foreground()
        if package in fg:
            return
        LOG.warning("游戏不在前台，尝试启动: %s", fg)
        self.adb.launch(package)
        time.sleep(8.0)
        fg = self.adb.foreground()
        if package not in fg:
            raise EngineError(f"无法把游戏拉到前台: {fg}")

    def recover_to_game(self, max_attempts: int = 8,
                        allow_restart: bool = True,
                        game_over_action: str = "retry") -> Detection:
        """从主页/云存档/继续上局/奖励弹窗/游戏结束恢复到局内。

        allow_restart=False 时，遇到 Game Over/主页立即抛 RunEnded，
        由多局循环负责计数与重开，避免在步骤中途偷偷重开。
        """
        self.ensure_foreground()
        unknown_count = 0
        last: Optional[Detection] = None

        for attempt in range(1, max_attempts + 1):
            self._check_pause()
            self._check_deadline()
            img = self.screenshot()
            det = self.detector.detect(img)
            self.last_detection = det
            last = det
            LOG.info("状态检查 %s/%s: %s (%s)", attempt, max_attempts,
                     det.state.value, det.reason)
            if det.state == State.IN_GAME:
                return det
            if det.state in (State.GAME_OVER, State.HOME) and not allow_restart:
                if det.state == State.GAME_OVER and game_over_action == "home":
                    h, w = img.shape[:2]
                    point = self.detector.action_point(
                        det, w, h, game_over_action="home",
                    )
                    if point is not None:
                        LOG.info("单局结束 → 点击主页 %s", point)
                        self.adb.tap(*point)
                        time.sleep(2.5)
                raise RunEnded(f"本局结束: {det.state.value}")
            if det.state == State.LOADING:
                LOG.info("游戏加载中，等待")
                time.sleep(4.0)
                continue
            if det.state == State.LEVEL_POPUP:
                LOG.info("关闭等级弹窗")
                self.adb.keyevent(4)
                time.sleep(0.8)
                continue
            if det.state == State.UNKNOWN:
                unknown_count += 1
                self._save_failure(f"unknown_state_{unknown_count}", img)
                if unknown_count >= 2:
                    raise EngineError(
                        "无法识别当前界面（已保存截图）。请人工确认后再运行。"
                    )
                LOG.warning("未知界面，尝试返回键")
                self.adb.keyevent(4)
                time.sleep(1.2)
                continue

            h, w = img.shape[:2]
            point = self.detector.action_point(
                det, w, h, game_over_action=game_over_action,
            )
            if point is None:
                LOG.warning("%s 未找到按钮，尝试返回键", det.state.value)
                self.adb.keyevent(4)
            else:
                LOG.info("%s → 点击 %s", det.state.value, point)
                self.adb.tap(*point)
            time.sleep(1.2 if det.state != State.GAME_OVER else 2.5)

        state = last.state.value if last else "unknown"
        raise EngineError(f"连续 {max_attempts} 次未能恢复到局内（最后状态 {state}）")

    def _detect_state(self) -> Detection:
        img = self.screenshot()
        det = self.detector.detect(img)
        self.last_detection = det
        return det

    def _ensure_run_alive(self) -> None:
        det = self._detect_state()
        if det.state in (State.GAME_OVER, State.HOME):
            self._raise_run_ended(det)
        if det.state != State.IN_GAME:
            self.recover_to_game(
                allow_restart=False,
                game_over_action=self.game_over_action,
            )

    def wait_for_run_end(self) -> Detection:
        """策略完成后等待本局自然结束；不主动送死、不重开。"""
        LOG.info("策略已完成，等待本局自然结束…")
        polls = 0
        while True:
            self._check_pause()
            self._check_deadline()
            if self.stop_event.wait(2.0):
                raise Cancelled("收到停止请求")
            det = self._detect_state()
            polls += 1
            if det.state in (State.GAME_OVER, State.HOME):
                LOG.info("本局结束: %s", det.state.value)
                return det
            if det.state != State.IN_GAME:
                try:
                    self.recover_to_game(
                        allow_restart=False,
                        game_over_action=self.game_over_action,
                    )
                except RunEnded:
                    return det
            if polls % 15 == 0:
                LOG.info("仍在等待本局结束（%s 次检查）", polls)

    # ------------------------------------------------------------------
    # 购买
    # ------------------------------------------------------------------
    def buy_once(
        self,
        tab: str,
        slot: int,
        *,
        card: Optional[Rect] = None,
        buy: Optional[tuple[int, int]] = None,
        before: Optional[float] = None,
    ) -> tuple[Optional[float], Optional[float]]:
        # 调用方通常已经定位并读过价格，复用这些结果，避免重复截图/OCR
        if card is None or buy is None:
            card, _, buy = self.locate_card(tab, slot)
        if before is None:
            before = self.read_price(self.screenshot(), card)
        LOG.info("购买: %s/%s 点击=%s 价格=%s", tab, slot, buy, before)
        self.adb.tap(*buy)
        self._sleep("after_tap", 0.55)
        after = None
        for _ in range(2):
            img2 = decode_png(self.adb.screencap())
            after = self.read_price(img2, card)
            if after is not None:
                break
            time.sleep(0.30)
        return before, after

    # ------------------------------------------------------------------
    # 策略执行
    # ------------------------------------------------------------------
    def execute_step(self, step: Step, goal_override: Optional[int] = None) -> StepResult:
        last_error: Optional[AdbError] = None
        for attempt in range(1, 4):
            try:
                return self._execute_step_once(step, goal_override)
            except RunEnded:
                raise
            except AdbError as exc:
                last_error = exc
                LOG.warning("ADB 异常（%s/3）: %s", attempt, exc)
                time.sleep(2.0)
                self.adb.ensure_connected()
                self.recover_to_game(
                    allow_restart=False,
                    game_over_action=self.game_over_action,
                )
        raise last_error or EngineError("步骤执行失败")

    @staticmethod
    def _price_confirmed(before: Optional[float], after: Optional[float]) -> bool:
        """价格必须出现有意义的上涨，避免 OCR 抖动的小幅变化被当成购买成功。"""
        if before is None or after is None:
            return False
        if after <= before:
            return False
        return after >= before + max(1.0, before * 0.02)

    def _execute_step_once(self, step: Step,
                           goal_override: Optional[int] = None) -> StepResult:
        LOG.info("===== 步骤: %s =====", step.label())
        self._check_pause()
        self._ensure_run_alive()
        self.switch_tab(step.tab)
        self.scroll_to_top(step.tab)
        img, layout = self.capture()
        batch = self.read_multiplier(img, layout)
        LOG.info("当前页签批量倍率: x%s", batch)

        level = self.read_level(step.tab, step.slot)
        goal = goal_override if goal_override is not None else step.goal_for(level)
        mode_cn = "相对 +" if step.mode == "relative" else "绝对"
        LOG.info("真实等级: %s / %s目标 %s", level, mode_cn, goal)
        if level >= goal:
            return StepResult(step, level, 0, True, "已达目标", goal)

        initial_level = level
        purchases = 0
        verify_rounds = 0
        while True:
            while level < goal:
                cycle_started = time.monotonic()
                self._check_pause()
                self._ensure_run_alive()
                # 关闭等级弹窗后升级面板可能被一并收起，先确保面板打开
                self.switch_tab(step.tab)
                if self.dry_run:
                    card, _, _ = self.locate_card(step.tab, step.slot)
                    img, layout = self.capture()
                    price = self.read_price(img, card)
                    cash = self.read_cash(img, layout)
                    LOG.info("[dry-run] 计划购买: 当前=%s 目标=%s 价格=%s 现金=%s",
                             level, goal, price, cash)
                    return StepResult(step, level, 0, False, "dry-run 未购买", goal)

                card, _, buy = self.locate_card(step.tab, step.slot)
                img, layout = self.capture()
                # 功能页/滚动后的卡片位置与常规页不同，先看满级再读价格，
                # 避免 OCR 从旁边读到 x7 之类的小数字后反复点击满级卡。
                if step.tab == "utility" or step.slot >= 6:
                    if self.is_maxed(img, card):
                        LOG.warning("%s 已达上限，跳过", step.label())
                        return StepResult(step, level, purchases, True, "已上限，跳过", goal, True)
                price = self.read_price(img, card)
                for _ in range(2):
                    if price is not None:
                        break
                    time.sleep(0.4)
                    img = self.screenshot()
                    price = self.read_price(img, card)
                if price is None:
                    if self.is_maxed(img, card):
                        LOG.warning("%s 已达上限，跳过", step.label())
                        return StepResult(step, level, purchases, True, "已上限，跳过", goal, True)
                    self._save_failure("no_price", img)
                    raise EngineError(f"{step.label()} 读不到价格（可能已上限/布局变化）")

                # 满级卡的价格框显示“上限”；OCR 可能从旁边读到 x7 之类的小数字，
                # 不能把它当价格继续点击。
                if price < 1000 and self.is_maxed(img, card):
                    LOG.warning("%s 已达上限，跳过", step.label())
                    return StepResult(step, level, purchases, True, "已上限，跳过", goal, True)

                cash = self.read_cash(img, layout)
                if cash is not None and price > cash:
                    LOG.info("现金不足: 需要 %.0f，持有 %.0f；等待", price, cash)
                    self._sleep("insufficient_gold", 5.0)
                    continue

                before, after = self.buy_once(
                    step.tab, step.slot, card=card, buy=buy, before=price,
                )
                confirmed = self._price_confirmed(before, after)
                if not confirmed and after is None:
                    try:
                        if self.is_maxed(self.screenshot(), card):
                            LOG.warning("%s 已达上限，跳过", step.label())
                            return StepResult(step, level, purchases, True, "已上限，跳过", goal, True)
                    except Exception:
                        pass

                if not confirmed:
                    # 价格 OCR 不可靠时，用等级弹窗做最终裁决
                    real: Optional[int] = None
                    try:
                        real = self.read_level(step.tab, step.slot)
                    except EngineError as exc:
                        LOG.warning("购买未确认，等级复核失败: %s", exc)
                    if real is not None and real > level:
                        purchases += 1
                        self.purchases += 1
                        self.failures = 0
                        LOG.info("价格未确认（%s → %s），但等级 %s → %s，视为购买成功",
                                 before, after, level, real)
                        level = real
                        continue

                if confirmed:
                    purchases += 1
                    self.purchases += 1
                    self.failures = 0
                    level += batch
                    LOG.info("购买成功: 价格 %s → %s，估算等级 %s", before, after, level)
                else:
                    self.failures += 1
                    LOG.warning("购买未确认: 价格 %s → %s（连续失败 %s/%s）",
                                before, after, self.failures, self.max_failures)
                    if self.failures >= self.max_failures:
                        self._save_failure("buy_failed", img)
                        raise EngineError("连续购买失败，已停止")
                    self._sleep("idle_poll", 1.0)
                if purchases > self.max_purchases:
                    raise EngineError("单步购买次数超过上限")
                self._pace(cycle_started)

            final = self.read_level(step.tab, step.slot)
            for _ in range(2):
                if final >= goal:
                    break
                time.sleep(0.6)
                final = self.read_level(step.tab, step.slot)

            # 同一局内等级不可能下降；出现说明面板/弹窗点错或游戏已重开
            if final < initial_level:
                LOG.warning("等级回退异常: 初始 %s → 当前 %s，重新核对",
                            initial_level, final)
                for _ in range(2):
                    time.sleep(0.6)
                    self.scroll_to_top(step.tab)
                    final = self.read_level(step.tab, step.slot)
                    if final >= initial_level:
                        break
                if final < initial_level:
                    self._save_failure("level_regressed", self.screenshot())
                    LOG.warning(
                        "等级回退无法复核: 初始 %s → 当前 %s；按本局结束处理",
                        initial_level, final,
                    )
                    # 长时间运行后本局可能已经结束并自动进入新一局；
                    # 交给多局/无限循环计数并从头执行，而不是杀死整个进程。
                    raise RunEnded(
                        f"等级回退，疑似本局已重置: {initial_level} → {final}"
                    )

            LOG.info("步骤结束校验: 真实等级 %s（目标 %s）", final, goal)
            if final >= goal:
                return StepResult(step, final, purchases, True, "完成", goal)

            verify_rounds += 1
            self._save_failure("verify_short", self.screenshot())
            LOG.error("校验未达目标: %s < %s（第 %s 次），继续补买",
                      final, goal, verify_rounds)
            if verify_rounds >= 3:
                raise EngineError(f"校验连续未达目标: {final} < {goal}")
            level = final

    def execute_loop(self, loop: Loop) -> list[StepResult]:
        LOG.info("===== %s =====", loop.label())
        results: list[StepResult] = []
        rounds = 0
        absolute_children = [s for s in loop.steps if s.mode == MODE_ABSOLUTE]
        while True:
            self._check_pause()
            self._check_deadline()
            round_results: list[StepResult] = []
            for child in loop.steps:
                round_results.append(self.execute_step(child))
            results.extend(round_results)
            rounds += 1
            LOG.info("%s 第 %s 轮完成", loop.name, rounds)

            if self.dry_run:
                break
            if loop.repeat and rounds >= loop.repeat:
                break
            if loop.stop_on_all_targets and absolute_children:
                abs_results = [
                    r for r in round_results if r.step.mode == MODE_ABSOLUTE
                ]
                if abs_results and all(r.completed for r in abs_results):
                    LOG.info("%s 绝对目标已全部达成，提前结束", loop.name)
                    break
            if rounds >= 1000:
                raise EngineError(f"{loop.name} 轮次超过安全上限")
        return results

    def execute_plan(self, plan: list, max_steps: Optional[int] = None) -> list[StepResult]:
        self.current_results = []
        count = 0
        for item in plan:
            if max_steps is not None and count >= max_steps:
                break
            self._ensure_run_alive()
            if isinstance(item, Loop):
                results = self.execute_loop(item)
            else:
                results = [self.execute_step(item)]
            self.current_results.extend(results)
            count += 1
        return list(self.current_results)

    def run(self, max_steps: Optional[int] = None,
            max_runtime_min: Optional[float] = None,
            runs_mode: Optional[str] = None,
            runs_count: Optional[int] = None) -> list[RunResult]:
        setup_logging()
        runtime = max_runtime_min if max_runtime_min is not None else self.limits.get("max_runtime_min")
        if runtime:
            self.deadline = time.time() + float(runtime) * 60.0
        LOG.info("引擎启动 dry_run=%s 最长运行=%s 分钟", self.dry_run, runtime)

        device = self.adb.ensure_connected()
        LOG.info("设备: %s", device.serial)

        expected = self.cfg.get("display", {}).get("expected_frame", [1080, 2400])
        probe = self.screenshot()
        if [probe.shape[1], probe.shape[0]] != expected:
            LOG.info("截图尺寸 %sx%s 不符，设置分辨率并等待重排",
                     probe.shape[1], probe.shape[0])
            self.adb.set_portrait_2400x1080()
            time.sleep(2.0)
            probe = self.screenshot()
            if [probe.shape[1], probe.shape[0]] != expected:
                raise EngineError(
                    f"无法得到期望分辨率 {expected}，当前 {probe.shape[1]}x{probe.shape[0]}"
                )
        runs_cfg = self.cfg.get("runs", {})
        mode = runs_mode or str(runs_cfg.get("mode", "single"))
        count = int(runs_count if runs_count is not None else runs_cfg.get("count", 1))
        if mode not in ("single", "count", "infinite"):
            raise EngineError(f"未知对局模式: {mode}")
        if mode == "count" and count < 1:
            raise EngineError("对局次数必须 >=1")
        LOG.info("对局模式: %s (%s)", mode, count if mode == "count" else "-")
        if mode == "infinite":
            # 无限模式只受停止键控制，忽略配置/CLI 的局数和最长运行时间
            runtime = 0
            self.deadline = None
            LOG.info("无限模式：忽略局数上限（配置 count=%s）和最长运行时间", count)
        self.game_over_action = "home" if mode == "single" else "retry"
        self.recover_to_game(
            allow_restart=True,
            game_over_action=self.game_over_action,
        )

        plan = plan_from_config(self.cfg)
        if not plan:
            raise EngineError("策略为空：请先添加步骤或循环")

        summaries: list[RunResult] = []
        run_index = 1
        while True:
            self._check_pause()
            self._check_deadline()
            LOG.info("===== 第 %s 局开始（%s）=====", run_index, mode)
            try:
                results = self.execute_plan(plan, max_steps=max_steps)
                if mode == "single" or self.dry_run:
                    summaries.append(RunResult(run_index, results, True, "策略完成"))
                    break
                self.wait_for_run_end()
                summaries.append(RunResult(run_index, results, True, "本局自然结束"))
            except RunEnded as exc:
                LOG.info("第 %s 局提前结束: %s", run_index, exc)
                summaries.append(
                    RunResult(run_index, list(self.current_results), True, str(exc))
                )
            except (RuntimeLimit, Cancelled):
                raise
            except EngineError as exc:
                det = None
                try:
                    det = self._detect_state()
                except Exception:
                    pass
                if det is not None and det.state in (State.GAME_OVER, State.HOME):
                    LOG.info("第 %s 局结束（%s）: %s", run_index, det.state.value, exc)
                    summaries.append(
                        RunResult(run_index, list(self.current_results), True, str(exc))
                    )
                else:
                    raise

            if mode == "single":
                break
            if mode == "count" and run_index >= count:
                LOG.info("已完成 %s 局，停止", run_index)
                break
            if self.stop_event.is_set():
                raise Cancelled("收到停止请求")
            self._check_deadline()
            LOG.info("准备重开第 %s 局", run_index + 1)
            self.recover_to_game(
                allow_restart=True,
                game_over_action=self.game_over_action,
            )
            run_index += 1

        LOG.info("多局任务结束: 共 %s 局", len(summaries))
        return summaries
