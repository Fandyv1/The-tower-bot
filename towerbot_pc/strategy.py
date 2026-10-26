# -*- coding: utf-8 -*-
"""策略模型：普通步骤 + 一层循环组。

语义：
- 绝对等级 absolute：达到 target 级。
- 增加等级 relative：每轮以进入该步骤时的等级为基准，增加 amount 级。
- 循环组重复执行子步骤；repeat=0/None 表示靠“全部绝对目标达成”停止。
- “全部达标”只检查绝对步骤，相对步骤不参与（避免每轮刚加完就误判）。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Iterable

from .catalog import TAB_CN, display_name, slot_count

MODE_ABSOLUTE = "absolute"
MODE_RELATIVE = "relative"


@dataclass
class Step:
    tab: str
    slot: int
    mode: str = MODE_ABSOLUTE
    target: int | None = None
    amount: int | None = None
    note: str = ""
    type: str = "step"

    @property
    def name(self) -> str:
        return self.note or display_name(self.tab, self.slot)

    @property
    def tab_cn(self) -> str:
        return TAB_CN.get(self.tab, self.tab)

    def goal_for(self, current: int) -> int:
        if self.mode == MODE_RELATIVE:
            return current + int(self.amount or 0)
        return int(self.target or 0)

    def label(self) -> str:
        if self.mode == MODE_RELATIVE:
            return f"{self.tab_cn}/{self.name} +{self.amount}"
        return f"{self.tab_cn}/{self.name} → {self.target}"


@dataclass
class Loop:
    name: str
    repeat: int | None = None
    stop_on_all_targets: bool = True
    steps: list[Step] = field(default_factory=list)
    type: str = "loop"

    def label(self) -> str:
        count = f"×{self.repeat}" if self.repeat else "直到达标"
        return f"循环[{self.name}] {count}"


def _parse_step(item: dict[str, Any]) -> Step:
    mode = str(item.get("mode", MODE_ABSOLUTE))
    if mode == MODE_RELATIVE:
        if item.get("amount") is None:
            raise ValueError("相对等级步骤缺少 amount")
        return Step(
            tab=str(item["tab"]),
            slot=int(item["slot"]),
            mode=MODE_RELATIVE,
            amount=int(item["amount"]),
            note=str(item.get("note", "")),
        )
    if item.get("target") is None:
        raise ValueError("绝对等级步骤缺少 target")
    return Step(
        tab=str(item["tab"]),
        slot=int(item["slot"]),
        mode=MODE_ABSOLUTE,
        target=int(item["target"]),
        note=str(item.get("note", "")),
    )


def parse_plan(cfg: dict[str, Any]) -> list[Step | Loop]:
    raw = cfg.get("strategy", {}).get("steps", [])
    if not isinstance(raw, list):
        raise ValueError("strategy.steps 必须是数组")
    plan: list[Step | Loop] = []
    for item in raw:
        if not isinstance(item, dict):
            raise ValueError("strategy.steps 的元素必须是对象")
        if item.get("type") == "loop":
            children = item.get("steps", [])
            if not isinstance(children, list) or not children:
                raise ValueError(f"循环 {item.get('name', '')} 没有子步骤")
            if any(child.get("type") == "loop" for child in children):
                raise ValueError("v1 只支持一层循环，不能嵌套循环")
            plan.append(
                Loop(
                    name=str(item.get("name", "循环")),
                    repeat=int(item["repeat"]) if item.get("repeat") else None,
                    stop_on_all_targets=bool(item.get("stop_on_all_targets", True)),
                    steps=[_parse_step(child) for child in children],
                )
            )
        else:
            plan.append(_parse_step(item))
    return plan


def validate_plan(plan: Iterable[Step | Loop]) -> None:
    top_steps = [item for item in plan if isinstance(item, Step)]
    _validate_step_list(top_steps) if top_steps else None
    for item in plan:
        if isinstance(item, Step):
            continue
        _validate_step_list(item.steps)
        if item.repeat is not None and item.repeat < 1:
            raise ValueError(f"循环 {item.name} 的次数必须 >=1 或留空")
        if item.repeat is None and not item.stop_on_all_targets:
            raise ValueError(f"循环 {item.name} 既没有次数也没有停止条件")
        if item.repeat is None and not any(s.mode == MODE_ABSOLUTE for s in item.steps):
            raise ValueError(f"循环 {item.name} 没有绝对目标，必须指定次数")


def _validate_step_list(steps: list[Step]) -> None:
    if not steps:
        raise ValueError("步骤列表不能为空")
    seen = set()
    for step in steps:
        _validate_step(step)
        key = (step.tab, step.slot)
        if key in seen:
            raise ValueError(f"同一列表中重复的属性步骤: {step.tab}#{step.slot}")
        seen.add(key)


def _validate_step(step: Step) -> None:
    if step.tab not in ("attack", "defense", "utility"):
        raise ValueError("tab 必须是 attack/defense/utility")
    limit = slot_count(step.tab)
    if not 0 <= step.slot < limit:
        raise ValueError(f"slot 超出 {step.tab} 范围 0..{limit - 1}")
    if step.mode == MODE_RELATIVE:
        if not step.amount or step.amount < 1:
            raise ValueError("相对等级 amount 必须 >=1")
    elif step.mode == MODE_ABSOLUTE:
        if not step.target or step.target < 1:
            raise ValueError("绝对等级 target 必须 >=1")
    else:
        raise ValueError("mode 必须是 absolute/relative")


def plan_from_config(cfg: dict[str, Any]) -> list[Step | Loop]:
    plan = parse_plan(cfg)
    validate_plan(plan)
    return plan


def flatten_steps(plan: Iterable[Step | Loop]) -> list[Step]:
    out: list[Step] = []
    for item in plan:
        if isinstance(item, Loop):
            out.extend(item.steps)
        else:
            out.append(item)
    return out


def tab_steps(steps: Iterable[Step]) -> dict[str, list[Step]]:
    grouped: dict[str, list[Step]] = {}
    for step in steps:
        grouped.setdefault(step.tab, []).append(step)
    return grouped
