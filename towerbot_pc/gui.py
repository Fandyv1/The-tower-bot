# -*- coding: utf-8 -*-
"""策略编辑 GUI（Tkinter）。

支持：
- 普通步骤（绝对等级 / 增加等级）；
- 一层循环组（含多个子步骤，按次数或绝对目标停止）；
- 单局 / 指定局数 / 无限对局；
- dry-run、开始/停止、实时日志、识别自检。
"""

from __future__ import annotations

import argparse
import contextlib
import io
import logging
import os
import queue
import threading
import tkinter as tk
from pathlib import Path
from tkinter import messagebox, ttk
from types import SimpleNamespace

from . import __version__
from .catalog import TAB_CN, display_name, slot_count
from .cli import _load_adb, cmd_doctor
from .config import DEFAULT_CONFIG_PATH, load_config, log_dir, save_config
from .engine import Cancelled, Engine, EngineError, RuntimeLimit, setup_logging
from .strategy import parse_plan, validate_plan

TABS = ["attack", "defense", "utility"]
RUN_MODES = [("单局", "single"), ("指定局数", "count"), ("无限", "infinite")]


class QueueLogHandler(logging.Handler):
    def __init__(self, q: queue.Queue) -> None:
        super().__init__()
        self.q = q

    def emit(self, record: logging.LogRecord) -> None:
        try:
            self.q.put(self.format(record))
        except Exception:
            pass


class StepDialog(tk.Toplevel):
    def __init__(self, parent: tk.Tk, step: dict | None = None) -> None:
        super().__init__(parent)
        self.title("编辑步骤" if step else "添加步骤")
        self.resizable(False, False)
        self.result: dict | None = None
        self.transient(parent)
        self.grab_set()

        step = step or {"tab": "defense", "slot": 0, "mode": "absolute", "target": 100, "note": ""}
        self.tab_var = tk.StringVar(value=step.get("tab", "defense"))
        self.slot_var = tk.IntVar(value=int(step.get("slot", 0)))
        self.mode_var = tk.StringVar(value=step.get("mode", "absolute"))
        value = step.get("amount") if step.get("mode") == "relative" else step.get("target")
        self.value_var = tk.IntVar(value=int(value or 100))
        self.note_var = tk.StringVar(value=step.get("note", ""))

        frame = ttk.Frame(self, padding=14)
        frame.pack(fill="both", expand=True)

        ttk.Label(frame, text="页签").grid(row=0, column=0, sticky="w", pady=5)
        tab_box = ttk.Combobox(frame, textvariable=self.tab_var, state="readonly",
                               values=TABS, width=18)
        tab_box.grid(row=0, column=1, sticky="ew", pady=5)

        ttk.Label(frame, text="槽位（绝对序号）").grid(row=1, column=0, sticky="w", pady=5)
        ttk.Spinbox(frame, from_=0, to=20, textvariable=self.slot_var, width=18).grid(
            row=1, column=1, sticky="ew", pady=5)
        ttk.Label(frame, text="范围随页签自动校验").grid(row=1, column=2, sticky="w", padx=6)

        ttk.Label(frame, text="等级模式").grid(row=2, column=0, sticky="w", pady=5)
        modes = ttk.Frame(frame)
        modes.grid(row=2, column=1, sticky="w", pady=5)
        ttk.Radiobutton(modes, text="绝对等级", value="absolute",
                        variable=self.mode_var, command=self._mode_changed).pack(side="left")
        ttk.Radiobutton(modes, text="增加等级", value="relative",
                        variable=self.mode_var, command=self._mode_changed).pack(side="left", padx=8)

        self.value_label = ttk.Label(frame, text="目标等级")
        self.value_label.grid(row=3, column=0, sticky="w", pady=5)
        ttk.Spinbox(frame, from_=1, to=6000, textvariable=self.value_var, width=18).grid(
            row=3, column=1, sticky="ew", pady=5)

        ttk.Label(frame, text="备注（可选）").grid(row=4, column=0, sticky="w", pady=5)
        ttk.Entry(frame, textvariable=self.note_var, width=24).grid(
            row=4, column=1, columnspan=2, sticky="ew", pady=5)

        self.name_label = ttk.Label(frame, text="", foreground="#555")
        self.name_label.grid(row=5, column=0, columnspan=3, sticky="w", pady=(2, 8))
        self.tab_var.trace_add("write", lambda *_: self._update_name())
        self.slot_var.trace_add("write", lambda *_: self._update_name())

        buttons = ttk.Frame(frame)
        buttons.grid(row=6, column=0, columnspan=3, sticky="e", pady=(8, 0))
        ttk.Button(buttons, text="取消", command=self.destroy).pack(side="left", padx=4)
        ttk.Button(buttons, text="确定", command=self._ok).pack(side="left")
        self.bind("<Return>", lambda _e: self._ok())
        self.bind("<Escape>", lambda _e: self.destroy())
        self._mode_changed()
        self._update_name()
        self.wait_visibility()
        self.focus_set()

    def _mode_changed(self) -> None:
        self.value_label.config(
            text="目标等级" if self.mode_var.get() == "absolute" else "增加等级"
        )

    def _update_name(self) -> None:
        try:
            self.name_label.config(
                text="属性：" + display_name(self.tab_var.get(), int(self.slot_var.get()))
            )
        except (ValueError, tk.TclError):
            self.name_label.config(text="")

    def _ok(self) -> None:
        try:
            slot = int(self.slot_var.get())
            value = int(self.value_var.get())
        except (ValueError, tk.TclError):
            messagebox.showerror("参数错误", "槽位和等级必须是整数", parent=self)
            return
        tab = self.tab_var.get()
        limit = slot_count(tab)
        if not 0 <= slot < limit:
            messagebox.showerror("参数错误", f"槽位需在 0..{limit - 1}", parent=self)
            return
        if not 1 <= value <= 6000:
            messagebox.showerror("参数错误", "等级需在 1..6000", parent=self)
            return
        mode = self.mode_var.get()
        self.result = {
            "type": "step",
            "tab": tab,
            "slot": slot,
            "mode": mode,
            "note": self.note_var.get().strip(),
        }
        if mode == "relative":
            self.result["amount"] = value
        else:
            self.result["target"] = value
        self.destroy()


class LoopDialog(tk.Toplevel):
    def __init__(self, parent: tk.Tk, loop: dict | None = None) -> None:
        super().__init__(parent)
        self.title("编辑循环" if loop else "添加循环")
        self.resizable(False, False)
        self.result: dict | None = None
        self.transient(parent)
        self.grab_set()

        loop = loop or {"name": "循环", "repeat": 3, "stop_on_all_targets": True}
        self.name_var = tk.StringVar(value=loop.get("name", "循环"))
        self.repeat_var = tk.IntVar(value=int(loop.get("repeat") or 0))
        self.stop_var = tk.BooleanVar(value=bool(loop.get("stop_on_all_targets", True)))

        frame = ttk.Frame(self, padding=14)
        frame.pack(fill="both", expand=True)
        ttk.Label(frame, text="循环名称").grid(row=0, column=0, sticky="w", pady=5)
        ttk.Entry(frame, textvariable=self.name_var, width=24).grid(
            row=0, column=1, sticky="ew", pady=5)
        ttk.Label(frame, text="循环次数（0=不按次数）").grid(row=1, column=0, sticky="w", pady=5)
        ttk.Spinbox(frame, from_=0, to=1000, textvariable=self.repeat_var, width=10).grid(
            row=1, column=1, sticky="w", pady=5)
        ttk.Checkbutton(
            frame, text="所有绝对目标达成后提前结束", variable=self.stop_var,
        ).grid(row=2, column=0, columnspan=2, sticky="w", pady=5)
        ttk.Label(
            frame, text="相对 +N 步骤不参与“全部达标”判断。", foreground="#555",
        ).grid(row=3, column=0, columnspan=2, sticky="w", pady=(0, 8))

        buttons = ttk.Frame(frame)
        buttons.grid(row=4, column=0, columnspan=2, sticky="e", pady=(8, 0))
        ttk.Button(buttons, text="取消", command=self.destroy).pack(side="left", padx=4)
        ttk.Button(buttons, text="确定", command=self._ok).pack(side="left")
        self.bind("<Return>", lambda _e: self._ok())
        self.bind("<Escape>", lambda _e: self.destroy())
        self.wait_visibility()
        self.focus_set()

    def _ok(self) -> None:
        name = self.name_var.get().strip()
        if not name:
            messagebox.showerror("参数错误", "循环名称不能为空", parent=self)
            return
        try:
            repeat = int(self.repeat_var.get())
        except (ValueError, tk.TclError):
            messagebox.showerror("参数错误", "循环次数必须是整数", parent=self)
            return
        if repeat < 0:
            messagebox.showerror("参数错误", "循环次数不能为负", parent=self)
            return
        if repeat == 0 and not self.stop_var.get():
            messagebox.showerror("参数错误", "次数为 0 时必须勾选“全部达标”停止条件", parent=self)
            return
        self.result = {
            "type": "loop",
            "name": name,
            "repeat": repeat,
            "stop_on_all_targets": bool(self.stop_var.get()),
            "steps": [],
        }
        self.destroy()


class App(tk.Tk):
    def __init__(self, config_path: str | Path = DEFAULT_CONFIG_PATH) -> None:
        super().__init__()
        self.config_path = Path(config_path)
        self.cfg = load_config(self.config_path)
        self.plan: list[dict] = list(self.cfg.get("strategy", {}).get("steps", []))
        self.engine: Engine | None = None
        self.worker: threading.Thread | None = None
        self.log_queue: queue.Queue = queue.Queue()
        self.item_map: dict[str, tuple[dict, list, int]] = {}
        self._build_logging()
        self._build_ui()
        self._refresh_table()
        self._poll_logs()

    # ------------------------------------------------------------------
    def _build_logging(self) -> None:
        setup_logging()
        handler = QueueLogHandler(self.log_queue)
        handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s", "%H:%M:%S"))
        logging.getLogger("towerbot").addHandler(handler)

    def _build_ui(self) -> None:
        self.title(f"The Tower PC Bot v{__version__}")
        self.geometry("1080x760")
        self.minsize(980, 680)
        try:
            self.option_add("*Font", ("Microsoft YaHei UI", 10))
        except tk.TclError:
            pass

        toolbar = ttk.Frame(self, padding=(10, 8))
        toolbar.pack(fill="x")
        ttk.Label(toolbar, text="配置：").pack(side="left")
        ttk.Label(toolbar, text=str(self.config_path), foreground="#555").pack(side="left")
        ttk.Button(toolbar, text="保存策略", command=self._save).pack(side="right", padx=4)
        ttk.Button(toolbar, text="识别自检", command=self._doctor).pack(side="right", padx=4)
        ttk.Button(toolbar, text="打开标注图", command=self._open_overlay).pack(side="right", padx=4)

        body = ttk.Frame(self, padding=(10, 0))
        body.pack(fill="both", expand=True)

        strategy_box = ttk.LabelFrame(body, text="升级策略（循环组可双击编辑）", padding=8)
        strategy_box.pack(fill="both", expand=True)

        columns = ("kind", "tab", "slot", "name", "target", "note")
        self.tree = ttk.Treeview(strategy_box, columns=columns, show="headings", height=10)
        headings = {
            "kind": "类型", "tab": "页签", "slot": "槽位",
            "name": "属性/循环", "target": "目标", "note": "备注",
        }
        widths = {"kind": 80, "tab": 100, "slot": 60, "name": 220, "target": 120, "note": 260}
        for col in columns:
            self.tree.heading(col, text=headings[col])
            self.tree.column(col, width=widths[col], anchor="center" if col != "note" else "w")
        self.tree.pack(side="left", fill="both", expand=True)
        scroll = ttk.Scrollbar(strategy_box, orient="vertical", command=self.tree.yview)
        scroll.pack(side="right", fill="y")
        self.tree.configure(yscrollcommand=scroll.set)
        self.tree.bind("<Double-1>", lambda _e: self._edit())

        buttons = ttk.Frame(body, padding=(0, 8))
        buttons.pack(fill="x")
        for text, command in (
            ("添加步骤", self._add_step),
            ("添加循环", self._add_loop),
            ("添加子步骤", self._add_child),
            ("编辑", self._edit),
            ("删除", self._delete),
            ("上移", lambda: self._move(-1)),
            ("下移", lambda: self._move(1)),
        ):
            ttk.Button(buttons, text=text, command=command).pack(side="left", padx=3)

        run_box = ttk.LabelFrame(body, text="运行", padding=8)
        run_box.pack(fill="x", pady=(4, 6))
        runs = self.cfg.get("runs", {})
        self.run_mode = tk.StringVar(value=str(runs.get("mode", "single")))
        self.run_count = tk.StringVar(value=str(runs.get("count", 1)))
        self.dry_var = tk.BooleanVar(value=True)
        self.runtime_var = tk.StringVar(
            value=str(self.cfg.get("limits", {}).get("max_runtime_min", 480))
        )

        ttk.Label(run_box, text="对局模式").pack(side="left")
        for text, value in RUN_MODES:
            ttk.Radiobutton(run_box, text=text, value=value, variable=self.run_mode,
                            command=self._mode_changed).pack(side="left", padx=3)
        self.count_spin = ttk.Spinbox(run_box, from_=1, to=999, textvariable=self.run_count, width=5)
        self.count_spin.pack(side="left", padx=(2, 10))

        ttk.Checkbutton(run_box, text="dry-run", variable=self.dry_var).pack(side="left", padx=(4, 10))
        self.runtime_label = ttk.Label(run_box, text="最长运行(分钟)")
        self.runtime_label.pack(side="left")
        self.runtime_entry = ttk.Entry(run_box, textvariable=self.runtime_var, width=7)
        self.runtime_entry.pack(side="left")
        self._mode_changed()

        self.start_btn = ttk.Button(run_box, text="开始", command=self._start)
        self.start_btn.pack(side="right", padx=3)
        self.stop_btn = ttk.Button(run_box, text="停止", command=self._stop, state="disabled")
        self.stop_btn.pack(side="right", padx=3)
        self.pause_btn = ttk.Button(
            run_box, text="暂停", command=self._toggle_pause, state="disabled",
        )
        self.pause_btn.pack(side="right", padx=3)
        self.status_var = tk.StringVar(value="就绪")
        ttk.Label(run_box, textvariable=self.status_var, foreground="#0a6").pack(side="right", padx=10)

        log_box = ttk.LabelFrame(body, text="日志", padding=6)
        log_box.pack(fill="both", expand=True)
        self.log_text = tk.Text(log_box, height=10, wrap="word", state="disabled")
        self.log_text.pack(side="left", fill="both", expand=True)
        log_scroll = ttk.Scrollbar(log_box, orient="vertical", command=self.log_text.yview)
        log_scroll.pack(side="right", fill="y")
        self.log_text.configure(yscrollcommand=log_scroll.set)
        self.protocol("WM_DELETE_WINDOW", self._on_close)

    # ------------------------------------------------------------------
    def _refresh_table(self) -> None:
        for item in self.tree.get_children():
            self.tree.delete(item)
        self.item_map.clear()

        for i, item in enumerate(self.plan):
            if item.get("type") == "loop":
                repeat = int(item.get("repeat") or 0)
                target_text = f"×{repeat}" if repeat else "直到达标"
                parent = self.tree.insert(
                    "", "end", open=True,
                    values=("循环", "-", "-", item.get("name", "循环"), target_text, ""),
                )
                self.item_map[parent] = (item, self.plan, i)
                for j, child in enumerate(item.get("steps", [])):
                    child_id = self.tree.insert(
                        parent, "end",
                        values=self._step_values(("  └ 步骤"), child),
                    )
                    self.item_map[child_id] = (child, item["steps"], j)
            else:
                item_id = self.tree.insert("", "end", values=self._step_values("步骤", item))
                self.item_map[item_id] = (item, self.plan, i)

    @staticmethod
    def _step_values(kind: str, step: dict) -> tuple:
        mode = step.get("mode", "absolute")
        target = (
            f"增加 +{step.get('amount', 0)}"
            if mode == "relative"
            else f"绝对 {step.get('target', 0)}"
        )
        return (
            kind,
            TAB_CN.get(step.get("tab"), step.get("tab")),
            step.get("slot"),
            display_name(step.get("tab"), int(step.get("slot", 0))),
            target,
            step.get("note", ""),
        )

    def _selected(self):
        selection = self.tree.selection()
        if not selection:
            return None
        iid = selection[0]
        return iid, *self.item_map.get(iid, (None, None, None))

    def _find_iid(self, target: dict) -> str | None:
        for iid, (item, _container, _idx) in self.item_map.items():
            if item is target:
                return iid
        return None

    def _select(self, item: dict) -> None:
        iid = self._find_iid(item)
        if iid:
            self.tree.selection_set(iid)
            self.tree.focus(iid)

    def _loop_context(self):
        """返回选中项所属的循环 dict 或 None。"""
        selected = self._selected()
        if not selected:
            return None
        iid, item, _container, _idx = selected
        if item.get("type") == "loop":
            return item
        parent = self.tree.parent(iid)
        if parent:
            parent_item = self.item_map.get(parent, (None, None, None))[0]
            if parent_item and parent_item.get("type") == "loop":
                return parent_item
        return None

    # ------------------------------------------------------------------
    def _add_step(self) -> None:
        loop = self._loop_context()
        dialog = StepDialog(self)
        self.wait_window(dialog)
        if not dialog.result:
            return
        if loop is not None:
            loop.setdefault("steps", []).append(dialog.result)
        else:
            self.plan.append(dialog.result)
        self._refresh_table()
        self._select(dialog.result)

    def _add_loop(self) -> None:
        dialog = LoopDialog(self)
        self.wait_window(dialog)
        if dialog.result:
            self.plan.append(dialog.result)
            self._refresh_table()
            self._select(dialog.result)

    def _add_child(self) -> None:
        selected = self._selected()
        if not selected or selected[1].get("type") != "loop":
            messagebox.showinfo("提示", "先选中一个循环组", parent=self)
            return
        loop = selected[1]
        dialog = StepDialog(self)
        self.wait_window(dialog)
        if dialog.result:
            loop.setdefault("steps", []).append(dialog.result)
            self._refresh_table()
            self._select(dialog.result)

    def _edit(self) -> None:
        selected = self._selected()
        if not selected:
            messagebox.showinfo("提示", "先选择一行", parent=self)
            return
        _iid, item, container, index = selected
        if item.get("type") == "loop":
            dialog = LoopDialog(self, item)
            self.wait_window(dialog)
            if dialog.result:
                dialog.result["steps"] = item.get("steps", [])
                container[index] = dialog.result
        else:
            dialog = StepDialog(self, item)
            self.wait_window(dialog)
            if dialog.result:
                container[index] = dialog.result
        self._refresh_table()
        self._select(container[index])

    def _delete(self) -> None:
        selected = self._selected()
        if not selected:
            return
        _iid, item, container, index = selected
        del container[index]
        self._refresh_table()

    def _move(self, delta: int) -> None:
        selected = self._selected()
        if not selected:
            return
        _iid, item, container, index = selected
        new = index + delta
        if not 0 <= new < len(container):
            return
        container[index], container[new] = container[new], container[index]
        self._refresh_table()
        self._select(item)

    # ------------------------------------------------------------------
    def _save(self) -> bool:
        try:
            validate_plan(parse_plan({"strategy": {"steps": self.plan}}))
        except ValueError as exc:
            messagebox.showerror("策略无效", str(exc), parent=self)
            return False
        self.cfg.setdefault("strategy", {})["steps"] = self.plan
        mode = self.run_mode.get()
        try:
            count = int(self.run_count.get())
            if count < 1:
                raise ValueError
        except ValueError:
            messagebox.showerror("参数错误", "对局次数必须是正整数", parent=self)
            return False
        self.cfg["runs"] = {"mode": mode, "count": count}
        try:
            save_config(self.cfg, self.config_path)
        except OSError as exc:
            messagebox.showerror("保存失败", str(exc), parent=self)
            return False
        self._log(f"已保存 {len(self.plan)} 个顶层项: {self.config_path}")
        return True

    def _log(self, text: str) -> None:
        self.log_text.configure(state="normal")
        self.log_text.insert("end", text.rstrip() + "\n")
        self.log_text.see("end")
        self.log_text.configure(state="disabled")

    def _doctor(self) -> None:
        self._log("开始识别自检…")
        args = SimpleNamespace(config=str(self.config_path))

        def work() -> None:
            buf = io.StringIO()
            try:
                with contextlib.redirect_stdout(buf):
                    code = cmd_doctor(args)
                self.log_queue.put(f"自检退出码={code}")
                if code == 0:
                    self.log_queue.put("标注图: logs/doctor_layout.png")
            except Exception as exc:
                self.log_queue.put(f"自检失败: {exc}")
            finally:
                for line in buf.getvalue().splitlines():
                    self.log_queue.put(line)

        threading.Thread(target=work, daemon=True).start()

    def _open_overlay(self) -> None:
        path = log_dir() / "doctor_layout.png"
        if not path.exists():
            messagebox.showinfo("提示", "还没有标注图，先点“识别自检”", parent=self)
            return
        try:
            os.startfile(path)  # type: ignore[attr-defined]
        except OSError as exc:
            messagebox.showerror("打开失败", str(exc), parent=self)

    # ------------------------------------------------------------------
    def _mode_changed(self) -> None:
        mode = self.run_mode.get()
        self.count_spin.configure(state="normal" if mode == "count" else "disabled")
        runtime_state = "normal" if mode == "single" else "disabled"
        self.runtime_entry.configure(state=runtime_state)
        self.runtime_label.configure(state=runtime_state)

    def _start(self) -> None:
        if self.worker and self.worker.is_alive():
            return
        if not self.plan:
            messagebox.showinfo("提示", "策略为空，先添加步骤或循环", parent=self)
            return
        if not self._save():
            return
        mode = self.run_mode.get()
        if mode == "single":
            try:
                runtime = float(self.runtime_var.get())
                if runtime <= 0:
                    raise ValueError
            except ValueError:
                messagebox.showerror("参数错误", "最长运行时间必须是正数", parent=self)
                return
        else:
            runtime = 0  # 指定局数/无限：禁用最长运行时间
        try:
            count = int(self.run_count.get()) if mode == "count" else 1
            if mode == "count" and count < 1:
                raise ValueError
        except ValueError:
            messagebox.showerror("参数错误", "对局次数必须是正整数", parent=self)
            return

        self.start_btn.configure(state="disabled")
        self.stop_btn.configure(state="normal")
        self.pause_btn.configure(state="normal", text="暂停")
        self.status_var.set("运行中…")
        cfg = load_config(self.config_path)
        adb = _load_adb(cfg)
        engine = Engine(cfg, adb, dry_run=self.dry_var.get())
        self.engine = engine

        def work() -> None:
            try:
                runs = engine.run(
                    max_runtime_min=runtime,
                    runs_mode=mode,
                    runs_count=count,
                )
                for run in runs:
                    self.log_queue.put(f"--- 第 {run.index} 局: {run.message} ---")
                    for result in run.results:
                        self.log_queue.put(
                            f"{result.step.label()}: level={result.level} goal={result.goal} "
                            f"purchases={result.purchases} {result.message}"
                        )
                self.log_queue.put("__DONE__")
            except Cancelled:
                self.log_queue.put("__STOPPED__")
            except RuntimeLimit as exc:
                self.log_queue.put(f"__LIMIT__:{exc}")
            except EngineError as exc:
                self.log_queue.put(f"__ERROR__:{exc}")
            except Exception as exc:
                self.log_queue.put(f"__ERROR__:未预期异常 {exc}")
            finally:
                self.engine = None

        self.worker = threading.Thread(target=work, daemon=True)
        self.worker.start()

    def _toggle_pause(self) -> None:
        if not self.engine:
            return
        if self.engine.is_paused:
            self.engine.resume()
            self.pause_btn.configure(text="暂停")
            self.status_var.set("运行中…")
        else:
            self.engine.pause()
            self.pause_btn.configure(text="继续")
            self.status_var.set("已暂停")

    def _stop(self) -> None:
        if self.engine:
            self._log("请求停止…")
            self.engine.stop()
            self.status_var.set("停止中…")

    def _on_close(self) -> None:
        if self.worker and self.worker.is_alive():
            if not messagebox.askyesno("退出", "任务仍在运行，停止并退出？", parent=self):
                return
            if self.engine:
                self.engine.stop()
        self.destroy()

    # ------------------------------------------------------------------
    def _handle_marker(self, text: str) -> bool:
        if text == "__DONE__":
            self.status_var.set("完成")
        elif text == "__STOPPED__":
            self.status_var.set("已停止")
        elif text.startswith("__LIMIT__:"):
            self.status_var.set("达到时长上限")
            self._log(text.split(":", 1)[1])
        elif text.startswith("__ERROR__:"):
            self.status_var.set("失败")
            self._log(text.split(":", 1)[1])
        else:
            return False
        self.start_btn.configure(state="normal")
        self.stop_btn.configure(state="disabled")
        self.pause_btn.configure(state="disabled", text="暂停")
        return True

    def _poll_logs(self) -> None:
        try:
            while True:
                text = self.log_queue.get_nowait()
                if not self._handle_marker(text):
                    self._log(text)
        except queue.Empty:
            pass
        self.after(200, self._poll_logs)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="towerbot_pc.gui")
    parser.add_argument("--config", default=str(DEFAULT_CONFIG_PATH))
    args = parser.parse_args(argv)
    App(args.config).mainloop()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
