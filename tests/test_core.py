# -*- coding: utf-8 -*-
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / ".vendor"))

from towerbot_pc.catalog import display_name, slot_count
from towerbot_pc.config import validate
from towerbot_pc.ocr import OcrLine, parse_level, parse_multiplier, parse_price, parse_quantity
from towerbot_pc.strategy import Loop, MODE_RELATIVE, Step, parse_plan
from towerbot_pc.states import State, classify_text, pick_button
from towerbot_pc.vision import Layout, Rect, build_layout, card_points, guess_tab, load_image


class ParseTests(unittest.TestCase):
    def test_quantity(self):
        self.assertEqual(parse_quantity("$ 625.18K"), 625180)
        self.assertEqual(parse_quantity("7.71K"), 7710)
        self.assertEqual(parse_quantity("1.23M"), 1_230_000)
        self.assertEqual(parse_quantity("4.46%"), 4.46)
        self.assertIsNone(parse_quantity("上限"))

    def test_price_prefers_suffixed_number(self):
        self.assertEqual(parse_price("$8 828.65K"), 828650)
        self.assertEqual(parse_price("$ 828.65K"), 828650)
        self.assertEqual(parse_price("$415"), 415)
        self.assertEqual(parse_price("819.22K"), 819220)  # OCR 偶尔丢 $
        self.assertIsNone(parse_price("上限"))

    def test_multiplier(self):
        self.assertEqual(parse_multiplier("x5"), 5)
        self.assertEqual(parse_multiplier("x10"), 10)
        self.assertIsNone(parse_multiplier("倍率"))

    def test_level(self):
        self.assertEqual(parse_level("91"), 91)
        self.assertEqual(parse_level("L6"), 6)


class LayoutTests(unittest.TestCase):
    def test_attack_layout(self):
        img = load_image(ROOT / "tests" / "frames" / "ingame_attack.png")
        layout = build_layout(img)
        self.assertEqual((layout.frame_w, layout.frame_h), (1080, 2400))
        self.assertEqual(len(layout.cards), 6)
        self.assertEqual(layout.cards[0].as_list(), [31, 1750, 501, 180])
        self.assertEqual(guess_tab(img), "attack")
        buy, info = card_points(layout.cards[0])
        self.assertEqual(buy, (416, 1894))

    def test_defense_layout(self):
        img = load_image(ROOT / "tests" / "frames" / "ingame_defense.png")
        layout = build_layout(img)
        self.assertEqual(guess_tab(img), "defense")
        self.assertEqual(layout.cards[1].x, 547)

    def test_utility_layout(self):
        img = load_image(ROOT / "tests" / "frames" / "ingame_utility.png")
        layout = build_layout(img)
        self.assertEqual(guess_tab(img), "utility")
        self.assertEqual(len(layout.cards), 6)


class CatalogTests(unittest.TestCase):
    def test_counts(self):
        self.assertEqual(slot_count("attack"), 13)
        self.assertEqual(slot_count("defense"), 16)
        self.assertEqual(slot_count("utility"), 11)

    def test_last_names(self):
        self.assertEqual(display_name("attack", 12), "弹射射程")
        self.assertEqual(display_name("defense", 15), "死里逃生")
        self.assertEqual(display_name("utility", 10), "包几率")


class ScrollTests(unittest.TestCase):
    def test_label_geometry(self):
        from towerbot_pc.engine import Engine

        engine = Engine.__new__(Engine)
        layout = Layout(
            frame_w=1080, frame_h=2400,
            title_band=Rect(0, 1629, 1080, 106),
            cards=[Rect(31, 1750, 501, 180), Rect(547, 1750, 501, 180)],
        )
        line = OcrLine("射程", 1.0, [[54, 2100], [172, 2100], [172, 2140], [54, 2140]])
        card, info, buy = engine._card_from_label(line, layout)
        self.assertEqual(info, (113, 2120))
        self.assertEqual(buy, (416, 2167))
        self.assertEqual((card.x, card.y, card.w, card.h), (31, 2035, 501, 180))

    def test_find_label_filters_safe_y(self):
        from towerbot_pc.engine import Engine

        low = OcrLine("射程", 1.0, [[0, 100], [10, 100], [10, 110], [0, 110]])
        safe = OcrLine("射程", 1.0, [[0, 2100], [10, 2100], [10, 2110], [0, 2110]])
        found = Engine._find_label([low, safe], "射程", 1800, 2200)
        self.assertIsNotNone(found)
        self.assertEqual(found.center[1], 2105)


class StateTests(unittest.TestCase):
    def test_classify(self):
        self.assertEqual(classify_text("防御升级 波次 661"), State.IN_GAME)
        self.assertEqual(classify_text("当前等级 91 等级上限 6000"), State.LEVEL_POPUP)
        self.assertEqual(classify_text("欢迎回来 继续先前的回合？ 继续"), State.RESUME)
        self.assertEqual(classify_text("警告 同意以加载 是 否"), State.CLOUD_SAVE)
        self.assertEqual(classify_text("金币奖励 金币1.5倍 禁用广告"), State.REWARD_MODAL)
        self.assertEqual(classify_text("总金币奖励 接续战斗 失调闯关"), State.HOME)
        self.assertEqual(classify_text("最高波次：3391 级别1 接续战斗"), State.HOME)
        self.assertEqual(classify_text("游戏统计数据 重试 主页"), State.GAME_OVER)
        self.assertEqual(classify_text("游戏统计 最高波次 重试"), State.GAME_OVER)
        self.assertEqual(classify_text("游戏结束 重新开始"), State.GAME_OVER)
        self.assertEqual(classify_text("接续战斗 失调闯关"), State.HOME)
        self.assertEqual(classify_text("Loading Main Scene 99.0%"), State.LOADING)
        self.assertEqual(classify_text("完全看不懂的界面"), State.UNKNOWN)

    def test_pick_button_prefers_lower(self):
        def line(text, y):
            return OcrLine(text, 1.0, [[0, y], [10, y], [10, y + 10], [0, y + 10]])

        lines = [line("战斗", 100), line("战斗", 1900), line("退出战斗", 1950)]
        chosen = pick_button(lines, ["战斗"], min_y=1000)
        self.assertIsNotNone(chosen)
        self.assertEqual(chosen.center[1], 1905)

    def test_action_points(self):
        from towerbot_pc.states import Detection, StateDetector

        def line(text, x, y, w=100, h=50):
            return OcrLine(text, 1.0, [[x, y], [x + w, y], [x + w, y + h], [x, y + h]])

        detector = StateDetector.__new__(StateDetector)  # 不初始化 OCR
        game_over = Detection(
            State.GAME_OVER, "",
            [line("重试", 248, 1677), line("主页", 650, 1677)],
        )
        self.assertEqual(
            detector.action_point(game_over, 1080, 2400), (298, 1702),
        )
        self.assertEqual(
            detector.action_point(game_over, 1080, 2400, game_over_action="home"),
            (700, 1702),
        )

        resume = Detection(State.RESUME, "", [line("继续", 666, 1388)])
        self.assertEqual(detector.action_point(resume, 1080, 2400), (716, 1413))


class GameOverFrameTests(unittest.TestCase):
    def test_provided_game_over_frame(self):
        from towerbot_pc.ocr import Ocr
        from towerbot_pc.states import Detection, StateDetector

        img = load_image(ROOT / "tests" / "frames" / "game_over_stats.png")
        detector = StateDetector(Ocr())
        det = detector.detect(img)
        self.assertEqual(det.state, State.GAME_OVER)
        self.assertIn("重试", det.text)
        self.assertIn("主页", det.text)
        action = detector.action_point(det, img.shape[1], img.shape[0], "home")
        self.assertIsNotNone(action)
        self.assertGreater(action[0], img.shape[1] * 0.5)
        self.assertGreater(action[1], img.shape[0] * 0.5)


class GameOverActionTests(unittest.TestCase):
    def _engine(self, action: str):
        import numpy as np
        from towerbot_pc.engine import Engine
        from towerbot_pc.states import Detection, State, StateDetector

        def line(text, x, y, w=100, h=50):
            return OcrLine(text, 1.0, [[x, y], [x + w, y], [x + w, y + h], [x, y + h]])

        class FakeAdb:
            def __init__(self):
                self.taps = []

            def tap(self, x, y):
                self.taps.append((x, y))

        det = Detection(State.GAME_OVER, "", [line("重试", 248, 1677), line("主页", 650, 1677)])
        engine = Engine.__new__(Engine)
        engine.adb = FakeAdb()
        engine.detector = StateDetector.__new__(StateDetector)
        engine.detector.action_point = StateDetector.action_point.__get__(engine.detector)
        engine.game_over_action = action
        engine._detect_state = lambda: det
        engine.screenshot = lambda: np.zeros((2400, 1080, 3), dtype=np.uint8)
        engine.pause_event = None
        return engine

    def test_single_clicks_home(self):
        from towerbot_pc.engine import RunEnded

        engine = self._engine("home")
        with self.assertRaises(RunEnded):
            engine._ensure_run_alive()
        self.assertEqual(engine.adb.taps, [(700, 1702)])

    def test_multi_does_not_click_on_mid_run_end(self):
        from towerbot_pc.engine import RunEnded

        engine = self._engine("retry")
        with self.assertRaises(RunEnded):
            engine._ensure_run_alive()
        self.assertEqual(engine.adb.taps, [])

    def test_capture_converts_title_band_error_to_run_end(self):
        from towerbot_pc.engine import Engine, RunEnded
        from towerbot_pc.ocr import Ocr
        from towerbot_pc.states import StateDetector

        class FakeAdb:
            def __init__(self):
                self.taps = []

            def tap(self, x, y):
                self.taps.append((x, y))

        img = load_image(ROOT / "tests" / "frames" / "game_over_stats.png")
        engine = Engine.__new__(Engine)
        engine.screenshot = lambda: img
        engine.detector = StateDetector(Ocr())
        engine.adb = FakeAdb()
        engine.game_over_action = "home"

        with self.assertRaises(RunEnded):
            engine.capture()
        self.assertEqual(len(engine.adb.taps), 1)
        self.assertGreater(engine.adb.taps[0][0], img.shape[1] * 0.5)

    def test_switch_tab_converts_title_band_error_to_run_end(self):
        from towerbot_pc.engine import Engine, RunEnded
        from towerbot_pc.ocr import Ocr
        from towerbot_pc.states import StateDetector

        class FakeAdb:
            def __init__(self):
                self.taps = []

            def tap(self, x, y):
                self.taps.append((x, y))

        img = load_image(ROOT / "tests" / "frames" / "game_over_stats.png")
        engine = Engine.__new__(Engine)
        engine.screenshot = lambda: img
        engine.detector = StateDetector(Ocr())
        engine.adb = FakeAdb()
        engine.game_over_action = "home"

        with self.assertRaises(RunEnded):
            engine.switch_tab("attack")
        self.assertEqual(len(engine.adb.taps), 1)
        self.assertGreater(engine.adb.taps[0][0], img.shape[1] * 0.5)


class AdbRetryTests(unittest.TestCase):
    def test_timeout_is_retried_after_reconnect(self):
        from towerbot_pc.adb import Adb, AdbError

        class RetryAdb(Adb):
            def __init__(self):
                self.attempts = 0
                self.connects = 0

            def shell(self, *args, timeout=None):
                self.attempts += 1
                if self.attempts < 2:
                    raise AdbError("adb 超时: input swipe")
                return "ok"

            def connect(self, serial=None):
                self.connects += 1

        adb = RetryAdb()
        self.assertEqual(adb.shell_retry("input", "swipe"), "ok")
        self.assertEqual(adb.attempts, 2)
        self.assertEqual(adb.connects, 1)


class LevelPopupTests(unittest.TestCase):
    def test_popup_attribute_validation(self):
        from towerbot_pc.engine import Engine
        from towerbot_pc.ocr import Ocr

        img = load_image(ROOT / "tests" / "frames" / "popup_level.png")
        h, w = img.shape[:2]
        layout = build_layout(img, Rect(0, int(h * 0.68), w, 100))
        engine = Engine.__new__(Engine)
        engine.ocr = Ocr()
        # 样本弹窗是「防御/生命值再生」
        self.assertTrue(engine._popup_attribute_matches(img, layout, "defense", 1))
        self.assertFalse(engine._popup_attribute_matches(img, layout, "defense", 0))
        self.assertFalse(engine._popup_attribute_matches(img, layout, "attack", 0))


class MaxedCardTests(unittest.TestCase):
    def test_utility_maxed_frame(self):
        from towerbot_pc.engine import Engine
        from towerbot_pc.ocr import Ocr

        img = load_image(ROOT / "tests" / "frames" / "utility_maxed.png")
        engine = Engine.__new__(Engine)
        engine.ocr = Ocr()
        # 「免费功能升级」卡片的实测位置；价格框显示“上限”
        self.assertTrue(engine.is_maxed(img, Rect(31, 2062, 501, 180)))


class PauseTests(unittest.TestCase):
    def test_stop_while_paused(self):
        import threading
        from towerbot_pc.engine import Cancelled, Engine

        engine = Engine.__new__(Engine)
        engine.pause_event = threading.Event()
        engine.stop_event = threading.Event()
        engine._pause_started = None
        engine.deadline = None
        engine.pause_event.set()
        engine.stop_event.set()
        with self.assertRaises(Cancelled):
            engine._check_pause()


class PriceConfirmTests(unittest.TestCase):
    def test_small_increase_is_not_confirmed(self):
        from towerbot_pc.engine import Engine

        self.assertFalse(Engine._price_confirmed(354370, 357380))
        self.assertFalse(Engine._price_confirmed(1000, 1005))
        self.assertTrue(Engine._price_confirmed(1000, 1100))
        self.assertTrue(Engine._price_confirmed(8, 9))
        self.assertFalse(Engine._price_confirmed(None, 100))


class LoopTests(unittest.TestCase):
    def test_parse_relative_loop(self):
        plan = parse_plan({"strategy": {"steps": [{
            "type": "loop", "name": "攻击交替", "repeat": 3,
            "stop_on_all_targets": True,
            "steps": [
                {"tab": "attack", "slot": 0, "mode": "relative", "amount": 10},
                {"tab": "attack", "slot": 1, "mode": "relative", "amount": 10},
            ],
        }]}})
        self.assertIsInstance(plan[0], Loop)
        loop = plan[0]
        self.assertEqual(loop.repeat, 3)
        self.assertIsInstance(loop.steps[0], Step)
        self.assertEqual(loop.steps[0].mode, MODE_RELATIVE)
        self.assertEqual(loop.steps[0].goal_for(100), 110)

    def test_loop_validation(self):
        with self.assertRaises(ValueError):
            validate({"strategy": {"steps": [{
                "type": "loop", "name": "空", "repeat": 2, "steps": [],
            }]}})
        with self.assertRaises(ValueError):
            validate({"strategy": {"steps": [{
                "type": "loop", "name": "无次数", "repeat": 0,
                "stop_on_all_targets": False,
                "steps": [{"tab": "attack", "slot": 0, "target": 10}],
            }]}})

    def test_runs_validation(self):
        validate({"strategy": {"steps": []}, "runs": {"mode": "count", "count": 3}})
        with self.assertRaises(ValueError):
            validate({"strategy": {"steps": []}, "runs": {"mode": "count", "count": 0}})


class RunModeTests(unittest.TestCase):
    def _engine(self, mode: str, count: int):
        import threading
        from types import SimpleNamespace

        import numpy as np
        from towerbot_pc.engine import Engine

        class FakeAdb:
            def ensure_connected(self):
                return SimpleNamespace(serial="fake")

        cfg = {
            "strategy": {"steps": [{"tab": "attack", "slot": 0, "target": 1}]},
            "runs": {"mode": mode, "count": count},
            "limits": {"max_runtime_min": 0},
            "display": {"expected_frame": [1080, 2400]},
        }
        engine = Engine.__new__(Engine)
        engine.cfg = cfg
        engine.adb = FakeAdb()
        engine.dry_run = False
        engine.limits = cfg["limits"]
        engine.deadline = None
        engine.stop_event = threading.Event()
        engine.pause_event = threading.Event()
        engine._pause_started = None
        engine.current_results = []
        engine.last_detection = None
        engine.recover_count = 0
        engine.end_count = 0

        engine.screenshot = lambda: np.zeros((2400, 1080, 3), dtype=np.uint8)
        engine.recover_to_game = lambda **kwargs: engine.__setattr__(
            "recover_count", engine.recover_count + 1
        )
        engine.execute_plan = lambda plan, max_steps=None: []
        engine.wait_for_run_end = lambda: engine.__setattr__(
            "end_count", engine.end_count + 1
        )
        return engine

    def test_count_runs(self):
        engine = self._engine("count", 2)
        runs = engine.run()
        self.assertEqual([r.index for r in runs], [1, 2])
        self.assertEqual(engine.end_count, 2)
        self.assertEqual(engine.recover_count, 2)  # 初始 + 第 1 局后重开

    def test_single_does_not_wait(self):
        engine = self._engine("single", 1)
        runs = engine.run()
        self.assertEqual(len(runs), 1)
        self.assertEqual(engine.end_count, 0)

    def test_infinite_stops_on_event(self):
        from towerbot_pc.engine import Cancelled

        engine = self._engine("infinite", 1)
        engine.deadline = 123.0  # 模拟误设的最长运行时间，无限模式应清掉

        def fake_wait():
            engine.end_count += 1
            if engine.end_count >= 2:
                engine.stop_event.set()

        engine.wait_for_run_end = fake_wait
        with self.assertRaises(Cancelled):
            engine.run(max_runtime_min=1, runs_count=4)
        self.assertEqual(engine.end_count, 2)
        self.assertIsNone(engine.deadline)


class StrategyTests(unittest.TestCase):
    def test_valid(self):
        validate({"strategy": {"steps": [{"tab": "defense", "slot": 1, "target": 9}]}})
        validate({"strategy": {"steps": [{"tab": "attack", "slot": 12, "target": 9}]}})

    def test_bad_tab(self):
        with self.assertRaises(ValueError):
            validate({"strategy": {"steps": [{"tab": "x", "slot": 1, "target": 9}]}})

    def test_slot_limit(self):
        with self.assertRaises(ValueError):
            validate({"strategy": {"steps": [{"tab": "utility", "slot": 11, "target": 9}]}})

    def test_duplicate(self):
        with self.assertRaises(ValueError):
            validate({"strategy": {"steps": [
                {"tab": "defense", "slot": 1, "target": 9},
                {"tab": "defense", "slot": 1, "target": 10},
            ]}})


if __name__ == "__main__":
    unittest.main()
