# -*- coding: utf-8 -*-
"""配置模型：设备、策略、延时、限制。

配置文件是唯一事实源；代码里不写死策略。
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any, Dict, List

from .strategy import plan_from_config

ROOT = Path(__file__).resolve().parent.parent
CONFIG_DIR = ROOT / "config"
DEFAULT_CONFIG_PATH = CONFIG_DIR / "tower.json"
CALIBRATION_PATH = CONFIG_DIR / "calibration.json"
LOG_DIR = ROOT / "logs"

DEFAULT_CONFIG: Dict[str, Any] = {
    "adb": {
        "path": "auto",
        "serial": "auto",
        "host": "127.0.0.1",
        "port": 5555,
        "package": "com.TechTreeGames.TheTower",
        "activity": "com.unity3d.player.UnityPlayerActivity",
    },
    "display": {
        "wm_size": "2400x1080",
        "density": 240,
        "expected_frame": [1080, 2400],
    },
    "strategy": {
        "name": "默认策略",
        "repeat_on_restart": True,
        "allow_overshoot": True,
        "steps": [],
    },
    "runs": {
        "mode": "single",
        "count": 1,
    },
    "limits": {
        "max_runtime_min": 480,
        "max_consecutive_failures": 8,
        "max_purchases_per_step": 500,
    },
    "delays": {
        "action_interval": 1.0,
        "after_tap": 0.35,
        "after_tab": 0.35,
        "after_popup_open": 0.40,
        "after_popup_close": 0.30,
        "idle_poll": 1.0,
        "insufficient_gold": 5.0,
    },
}


def _deep_merge(base: Dict[str, Any], extra: Dict[str, Any]) -> Dict[str, Any]:
    out = dict(base)
    for key, value in extra.items():
        if isinstance(value, dict) and isinstance(out.get(key), dict):
            out[key] = _deep_merge(out[key], value)
        else:
            out[key] = value
    return out


def load_config(path: str | Path | None = None) -> Dict[str, Any]:
    p = Path(path) if path else DEFAULT_CONFIG_PATH
    cfg = json.loads(json.dumps(DEFAULT_CONFIG))
    if p.exists():
        try:
            data = json.loads(p.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            raise ValueError(f"配置文件读取失败: {p}: {exc}") from exc
        if not isinstance(data, dict):
            raise ValueError(f"配置文件必须是 JSON 对象: {p}")
        cfg = _deep_merge(cfg, data)
    validate(cfg)
    return cfg


def save_config(cfg: Dict[str, Any], path: str | Path | None = None) -> Path:
    p = Path(path) if path else DEFAULT_CONFIG_PATH
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(p.suffix + ".tmp")
    tmp.write_text(json.dumps(cfg, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(tmp, p)
    return p


def validate(cfg: Dict[str, Any]) -> None:
    plan_from_config(cfg)
    runs = cfg.get("runs", {})
    mode = runs.get("mode", "single")
    if mode not in ("single", "count", "infinite"):
        raise ValueError("runs.mode 必须是 single/count/infinite")
    count = runs.get("count", 1)
    if not isinstance(count, int) or count < 1:
        raise ValueError("runs.count 必须是 >=1 的整数")


def ensure_default_config() -> Path:
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    if not DEFAULT_CONFIG_PATH.exists():
        save_config(DEFAULT_CONFIG, DEFAULT_CONFIG_PATH)
    return DEFAULT_CONFIG_PATH


def log_dir() -> Path:
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    return LOG_DIR
