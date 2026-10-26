#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""ADB 访问层：设备发现、截图、点击、前台应用查询。

设计目标：
- 不依赖真实鼠标/窗口前台；
- 所有命令带超时；
- BlueStacks 的 adb 偶尔会断开，关键操作自动重连重试；
- 不在这里做任何游戏语义判断。
"""

from __future__ import annotations

import os
import shutil
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, Optional

from . import __version__

CREATE_NO_WINDOW = 0x08000000 if os.name == "nt" else 0
PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"

DEFAULT_BLUEStacks_ADB = Path(r"C:\Program Files\BlueStacks_nxt\HD-Adb.exe")
DEFAULT_SERIAL = "127.0.0.1:5555"


class AdbError(RuntimeError):
    """ADB 操作失败。"""


@dataclass
class DeviceInfo:
    serial: str
    state: str
    product: str = ""
    model: str = ""
    device: str = ""

    @property
    def online(self) -> bool:
        return self.state == "device"


def find_adb(explicit: Optional[str] = None) -> str:
    """按优先级查找 adb：显式配置 → 环境变量 → PATH → BlueStacks 自带。"""
    candidates = []
    if explicit and explicit != "auto":
        candidates.append(explicit)
    env = os.environ.get("ADB")
    if env:
        candidates.append(env)
    which = shutil.which("adb")
    if which:
        candidates.append(which)
    if DEFAULT_BLUEStacks_ADB.exists():
        candidates.append(str(DEFAULT_BLUEStacks_ADB))
    for item in candidates:
        p = Path(item)
        if p.exists() and p.is_file():
            return str(p)
    raise AdbError(
        "找不到 adb。请在 config/tower.json 的 adb.path 指定 adb 路径，"
        "或把 platform-tools 加入 PATH。"
    )


class Adb:
    """一个设备连接的薄封装。"""

    def __init__(
        self,
        adb_path: Optional[str] = None,
        serial: Optional[str] = None,
        host: str = "127.0.0.1",
        port: int = 5555,
        timeout: float = 15.0,
    ) -> None:
        self.adb_path = find_adb(adb_path)
        self.host = host
        self.port = int(port)
        self.serial = serial
        self.timeout = timeout

    # ------------------------------------------------------------------
    # 基础执行
    # ------------------------------------------------------------------
    def _run(
        self,
        args: Iterable[str],
        *,
        binary: bool = False,
        timeout: Optional[float] = None,
        check: bool = True,
    ) -> bytes | str:
        cmd = [self.adb_path, *[str(a) for a in args]]
        try:
            proc = subprocess.run(
                cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                timeout=timeout or self.timeout,
                creationflags=CREATE_NO_WINDOW,
            )
        except subprocess.TimeoutExpired as exc:
            raise AdbError(f"adb 超时: {' '.join(cmd)}") from exc
        if check and proc.returncode != 0:
            err = proc.stderr.decode("utf-8", "replace").strip()
            out = proc.stdout.decode("utf-8", "replace").strip()
            raise AdbError(f"adb 失败 ({proc.returncode}): {err or out}")
        return proc.stdout if binary else proc.stdout.decode("utf-8", "replace")

    def _device_args(self, serial: Optional[str] = None) -> list[str]:
        s = serial or self.serial
        return ["-s", s] if s else []

    # ------------------------------------------------------------------
    # 设备发现 / 连接
    # ------------------------------------------------------------------
    def start_server(self) -> None:
        self._run(["start-server"], check=False, timeout=20)

    def devices(self) -> list[DeviceInfo]:
        out = self._run(["devices", "-l"], timeout=20)
        result = []
        for line in str(out).splitlines():
            line = line.strip()
            if not line or line.startswith("List of devices") or line.startswith("*"):
                continue
            parts = line.split()
            if len(parts) < 2:
                continue
            serial, state = parts[0], parts[1]
            info = DeviceInfo(serial=serial, state=state)
            for kv in parts[2:]:
                if ":" in kv:
                    k, v = kv.split(":", 1)
                    setattr(info, k, v)
            result.append(info)
        return result

    def connect(self, serial: Optional[str] = None) -> str:
        target = serial or (f"{self.host}:{self.port}")
        out = self._run(["connect", target], check=False, timeout=20)
        return str(out).strip()

    def ensure_connected(self, connect_rounds: int = 3) -> DeviceInfo:
        """确保有一个在线设备；多设备时只接受显式 serial。"""
        self.start_server()
        if self.serial:
            self.connect(self.serial)
        else:
            self.connect(f"{self.host}:{self.port}")

        last: list[DeviceInfo] = []
        for _ in range(connect_rounds):
            last = [d for d in self.devices() if d.online]
            if self.serial:
                for d in last:
                    if d.serial == self.serial:
                        return d
            else:
                preferred = [d for d in last if d.serial == f"{self.host}:{self.port}"]
                pool = preferred or last
                if len(pool) == 1:
                    self.serial = pool[0].serial
                    return pool[0]
                if len(pool) > 1:
                    # BlueStacks 会同时暴露 127.0.0.1:5555 与 emulator-5554；优先前者。
                    for d in pool:
                        if d.serial.startswith("127.0.0.1:"):
                            self.serial = d.serial
                            return d
                    raise AdbError(
                        "检测到多个设备，请在 config 中指定 adb.serial："
                        + ", ".join(d.serial for d in pool)
                    )
            time.sleep(1.0)

        states = ", ".join(f"{d.serial}={d.state}" for d in last) or "无设备"
        raise AdbError(f"没有可用的 ADB 设备（{states}）。请确认 BlueStacks 已启动且 ADB 已开启。")

    # ------------------------------------------------------------------
    # 命令 / 输入
    # ------------------------------------------------------------------
    def shell(self, *args: str, timeout: Optional[float] = None) -> str:
        return str(
            self._run([*self._device_args(), "shell", *args], timeout=timeout)
        )

    def shell_retry(self, *args: str, attempts: int = 3, timeout: Optional[float] = None) -> str:
        last_error: Optional[Exception] = None
        for i in range(attempts):
            try:
                return self.shell(*args, timeout=timeout)
            except AdbError as exc:
                last_error = exc
                text = str(exc).lower()
                retryable = any(
                    marker in text
                    for marker in ("closed", "device", "offline", "timeout", "超时", "timed out")
                )
                if not retryable:
                    raise
                # 超时往往不是命令本身失败，而是 BlueStacks/ADB 链路卡住；
                # 重连后再试，而不是把一次输入超时直接升级成步骤失败。
                time.sleep(0.8 * (i + 1))
                try:
                    self.connect()
                except AdbError:
                    # 重连本身也可能超时；不要让它掩盖原始失败，继续下一次尝试。
                    pass
        raise AdbError(f"shell 重试失败: {last_error}")

    def tap(self, x: int, y: int) -> None:
        self.shell_retry("input", "tap", str(int(x)), str(int(y)))

    def swipe(self, x1: int, y1: int, x2: int, y2: int, duration_ms: int = 300) -> None:
        self.shell_retry(
            "input", "swipe", str(int(x1)), str(int(y1)),
            str(int(x2)), str(int(y2)), str(int(duration_ms)),
        )

    def keyevent(self, key: int | str) -> None:
        self.shell_retry("input", "keyevent", str(key))

    # ------------------------------------------------------------------
    # 截图
    # ------------------------------------------------------------------
    def screencap(self, retries: int = 3) -> bytes:
        last_error: Optional[Exception] = None
        for i in range(retries):
            try:
                data = self._run(
                    [*self._device_args(), "exec-out", "screencap", "-p"],
                    binary=True,
                    timeout=20,
                )
                if not isinstance(data, bytes) or not data.startswith(PNG_SIGNATURE):
                    raise AdbError("screencap 返回的不是有效 PNG")
                return data
            except AdbError as exc:
                last_error = exc
                time.sleep(0.5 * (i + 1))
                self.connect()
        raise AdbError(f"截图失败: {last_error}")

    def save_screencap(self, path: str | Path) -> Path:
        p = Path(path)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(self.screencap())
        return p

    # ------------------------------------------------------------------
    # 设备状态
    # ------------------------------------------------------------------
    def wm_size(self) -> tuple[int, int]:
        """返回当前生效的逻辑尺寸；优先 Override size。"""
        out = self.shell_retry("wm", "size")
        parsed: list[tuple[str, int, int]] = []
        for line in str(out).splitlines():
            if ":" not in line or "x" not in line:
                continue
            label, value = line.split(":", 1)
            w, _, h = value.strip().partition("x")
            if w.isdigit() and h.isdigit():
                parsed.append((label.strip().lower(), int(w), int(h)))
        for label, w, h in parsed:
            if "override" in label:
                return w, h
        if parsed:
            return parsed[0][1], parsed[0][2]
        raise AdbError(f"无法解析 wm size: {out!r}")

    def set_portrait_2400x1080(self) -> None:
        """把 Android 逻辑分辨率设成 2400x1080；游戏竖屏后截图是 1080x2400。"""
        self.shell_retry("wm", "size", "2400x1080")
        self.shell_retry("wm", "density", "240")

    def foreground(self) -> str:
        out = self.shell_retry("dumpsys", "window", "windows")
        for line in str(out).splitlines():
            if "mCurrentFocus" in line:
                return line.strip()
        return ""

    def current_activity(self) -> str:
        out = self.shell_retry("dumpsys", "activity", "activities")
        for line in str(out).splitlines():
            if "mResumedActivity" in line:
                return line.strip()
        return ""

    def launch(self, package: str) -> None:
        self.shell_retry(
            "monkey", "-p", package,
            "-c", "android.intent.category.LAUNCHER", "1",
            timeout=20,
        )

    def force_stop(self, package: str) -> None:
        self.shell_retry("am", "force-stop", package)

    def package_version(self, package: str) -> str:
        out = self.shell_retry("dumpsys", "package", package)
        for line in str(out).splitlines():
            if "versionName=" in line:
                return line.strip().split("versionName=", 1)[1]
        return ""

    def is_screen_on(self) -> bool:
        out = self.shell_retry("dumpsys", "power")
        return "mWakefulness=Awake" in str(out) or "Display Power: state=ON" in str(out)
