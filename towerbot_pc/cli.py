# -*- coding: utf-8 -*-
"""命令行入口：doctor / capture / detect / ocr / tap。"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

from . import __version__
from .adb import Adb, AdbError
from .config import DEFAULT_CONFIG_PATH, ensure_default_config, load_config, log_dir
from .ocr import Ocr, parse_multiplier, parse_quantity, read_level
from .vision import (
    Layout,
    Rect,
    build_layout,
    card_points,
    decode_png,
    draw_layout,
    find_title_band,
    load_image,
)

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(errors="replace")


def _load_adb(cfg: dict) -> Adb:
    adb_cfg = cfg["adb"]
    return Adb(
        adb_path=adb_cfg.get("path"),
        serial=None if adb_cfg.get("serial") in (None, "", "auto") else adb_cfg["serial"],
        host=adb_cfg.get("host", "127.0.0.1"),
        port=int(adb_cfg.get("port", 5555)),
    )


def _print_layout(layout: Layout) -> None:
    print(f"frame={layout.frame_w}x{layout.frame_h}")
    print(f"title_band={layout.title_band.as_list()}")
    for i, card in enumerate(layout.cards):
        buy, info = card_points(card)
        print(f"card[{i}]={card.as_list()} buy={buy} info={info}")


def cmd_doctor(args: argparse.Namespace) -> int:
    cfg = load_config(args.config)
    adb = _load_adb(cfg)
    print(f"The Tower PC bot {__version__}")
    print(f"adb={adb.adb_path}")
    device = adb.ensure_connected()
    print(f"device={device.serial} model={device.model}")
    print(f"wm_size={adb.wm_size()}")
    print(f"foreground={adb.foreground()}")
    pkg = cfg["adb"]["package"]
    print(f"package={pkg} version={adb.package_version(pkg)}")

    expected = cfg["display"].get("expected_frame", [1080, 2400])
    data = adb.screencap()
    out_dir = log_dir()
    png = out_dir / "doctor.png"
    png.write_bytes(data)
    img = decode_png(data)
    print(f"screenshot={png} size={img.shape[1]}x{img.shape[0]}")
    if [img.shape[1], img.shape[0]] != expected:
        print(f"截图尺寸不符，尝试设置 {cfg['display'].get('wm_size', '2400x1080')} …")
        adb.set_portrait_2400x1080()
        import time

        time.sleep(2.0)
        data = adb.screencap()
        png.write_bytes(data)
        img = decode_png(data)
        print(f"screenshot={png} size={img.shape[1]}x{img.shape[0]}")
        if [img.shape[1], img.shape[0]] != expected:
            print(f"ERROR: 无法得到期望分辨率 {expected}")
            return 2

    try:
        layout = build_layout(img)
    except ValueError as exc:
        print(f"LAYOUT FAILED: {exc}")
        return 2
    _print_layout(layout)
    overlay = draw_layout(img, layout)
    import cv2

    overlay_path = out_dir / "doctor_layout.png"
    cv2.imwrite(str(overlay_path), overlay)
    print(f"overlay={overlay_path}")
    return 0


def cmd_capture(args: argparse.Namespace) -> int:
    cfg = load_config(args.config)
    adb = _load_adb(cfg)
    adb.ensure_connected()
    out = Path(args.out) if args.out else log_dir() / "capture.png"
    adb.save_screencap(out)
    print(out)
    return 0


def cmd_detect(args: argparse.Namespace) -> int:
    cfg = load_config(args.config)
    if args.image:
        img = load_image(args.image)
    else:
        adb = _load_adb(cfg)
        adb.ensure_connected()
        img = decode_png(adb.screencap())
    band = find_title_band(img)
    layout = build_layout(img, band)
    _print_layout(layout)
    import cv2

    out = Path(args.out) if args.out else log_dir() / "detect_layout.png"
    out.parent.mkdir(parents=True, exist_ok=True)
    cv2.imwrite(str(out), draw_layout(img, layout))
    print(f"overlay={out}")
    return 0


def cmd_ocr(args: argparse.Namespace) -> int:
    img = load_image(args.image)
    rect = None
    if args.rect:
        x, y, w, h = [int(v) for v in args.rect.split(",")]
        rect = Rect(x, y, w, h)
    ocr = Ocr()
    lines = ocr.recognize(img, rect, scale=args.scale)
    for line in lines:
        print(f"{line.confidence:.3f}\t{line.text}\t{line.center}")
    if args.number:
        for line in lines:
            q = parse_quantity(line.text)
            m = parse_multiplier(line.text)
            if q is not None or m is not None:
                print(f"parsed: text={line.text!r} quantity={q} multiplier={m}")
    return 0


def cmd_level(args: argparse.Namespace) -> int:
    cfg = load_config(args.config)
    adb = _load_adb(cfg)
    adb.ensure_connected()
    img = decode_png(adb.screencap())
    layout = build_layout(img)
    ocr = Ocr()
    if args.slot is None:
        # 打开第 0 格等级弹窗测试
        slot = 0
    else:
        slot = args.slot
    info = card_points(layout.cards[slot])[1]
    adb.tap(*info)
    import time

    time.sleep(0.6)
    img2 = decode_png(adb.screencap())
    level = read_level(ocr, img2, layout.level_popup, layout.level_number)
    print(f"slot={slot} info_point={info} level={level}")
    adb.keyevent(4)
    return 0


def cmd_tap(args: argparse.Namespace) -> int:
    cfg = load_config(args.config)
    adb = _load_adb(cfg)
    adb.ensure_connected()
    adb.tap(args.x, args.y)
    print(f"tapped {args.x},{args.y}")
    return 0


class AlreadyRunning(RuntimeError):
    pass


def _acquire_lock() -> Path:
    path = log_dir() / "engine.lock"
    if path.exists():
        try:
            pid = int(path.read_text(encoding="ascii").strip())
            os.kill(pid, 0)
            raise AlreadyRunning(f"已有引擎在运行（PID {pid}）")
        except (ValueError, OSError):
            path.unlink(missing_ok=True)
    fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
    try:
        os.write(fd, str(os.getpid()).encode("ascii"))
    finally:
        os.close(fd)
    return path


def _release_lock(path: Path) -> None:
    try:
        path.unlink(missing_ok=True)
    except OSError:
        pass


def cmd_run(args: argparse.Namespace) -> int:
    from .engine import Cancelled, Engine, EngineError, RuntimeLimit, setup_logging

    cfg = load_config(args.config)
    adb = _load_adb(cfg)
    setup_logging()
    print(f"log={log_dir() / 'engine.log'}")
    try:
        lock = _acquire_lock()
    except AlreadyRunning as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 5
    if getattr(args, "infinite", False) and getattr(args, "runs", None):
        print("ERROR: --runs 与 --infinite 不能同时使用", file=sys.stderr)
        return 2
    runs_mode = "infinite" if getattr(args, "infinite", False) else (
        "count" if getattr(args, "runs", None) else None
    )
    runs_count = getattr(args, "runs", None)
    try:
        try:
            results = Engine(cfg, adb, dry_run=args.dry_run).run(
                max_steps=args.max_steps,
                max_runtime_min=args.max_runtime,
                runs_mode=runs_mode,
                runs_count=runs_count,
            )
        except RuntimeLimit as exc:
            print(f"RUNTIME LIMIT: {exc}", file=sys.stderr)
            return 4
        except Cancelled:
            print("STOPPED: 已手动停止", file=sys.stderr)
            return 0
        except EngineError as exc:
            print(f"ENGINE ERROR: {exc}", file=sys.stderr)
            return 3
    finally:
        _release_lock(lock)
    for run in results:
        print(f"--- 第 {run.index} 局: {run.message} ---")
        for result in run.results:
            print(f"{result.step.label()}: level={result.level} goal={result.goal} "
                  f"purchases={result.purchases} completed={result.completed} {result.message}")
    return 0


def cmd_recover(args: argparse.Namespace) -> int:
    from .engine import Engine, EngineError, setup_logging

    cfg = load_config(args.config)
    adb = _load_adb(cfg)
    adb.ensure_connected()
    setup_logging()
    try:
        detection = Engine(cfg, adb).recover_to_game()
    except EngineError as exc:
        print(f"RECOVER FAILED: {exc}", file=sys.stderr)
        return 3
    print(f"state={detection.state.value} reason={detection.reason}")
    print(detection.text[:500])
    return 0


def cmd_state(args: argparse.Namespace) -> int:
    cfg = load_config(args.config)
    adb = _load_adb(cfg)
    adb.ensure_connected()
    print(f"foreground={adb.foreground()}")
    print(f"activity={adb.current_activity()}")
    print(f"wm_size={adb.wm_size()}")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="towerbot_pc", description="The Tower PC automation")
    parser.add_argument("--version", action="version", version=__version__)
    parser.add_argument("--config", default=str(DEFAULT_CONFIG_PATH))
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("doctor", help="检查 ADB、截图、分辨率与面板定位")
    p.set_defaults(func=cmd_doctor)

    p = sub.add_parser("capture", help="抓一张截图")
    p.add_argument("--out")
    p.set_defaults(func=cmd_capture)

    p = sub.add_parser("detect", help="定位面板并输出标注图")
    p.add_argument("--image")
    p.add_argument("--out")
    p.set_defaults(func=cmd_detect)

    p = sub.add_parser("ocr", help="对截图或区域做 OCR")
    p.add_argument("--image", required=True)
    p.add_argument("--rect", help="x,y,w,h")
    p.add_argument("--scale", type=float, default=2.0)
    p.add_argument("--number", action="store_true")
    p.set_defaults(func=cmd_ocr)

    p = sub.add_parser("level", help="打开某格等级弹窗并 OCR")
    p.add_argument("--slot", type=int)
    p.set_defaults(func=cmd_level)

    p = sub.add_parser("tap", help="点击屏幕坐标")
    p.add_argument("x", type=int)
    p.add_argument("y", type=int)
    p.set_defaults(func=cmd_tap)

    p = sub.add_parser("run", help="按配置策略执行购买")
    p.add_argument("--dry-run", action="store_true", help="只识别与打印，不购买")
    p.add_argument("--max-steps", type=int)
    p.add_argument("--max-runtime", type=float, help="最长运行分钟数")
    p.add_argument("--runs", type=int, help="总共运行 N 局（当前局算第 1 局）")
    p.add_argument("--infinite", action="store_true", help="无限重复对局")
    p.set_defaults(func=cmd_run)

    p = sub.add_parser("recover", help="只执行状态恢复，不购买")
    p.set_defaults(func=cmd_recover)

    p = sub.add_parser("state", help="打印前台与分辨率")
    p.set_defaults(func=cmd_state)

    return parser


def main(argv: list[str] | None = None) -> int:
    ensure_default_config()
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return int(args.func(args))
    except (AdbError, ValueError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
