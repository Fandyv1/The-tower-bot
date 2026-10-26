# -*- coding: utf-8 -*-
"""属性目录：页签 + 绝对槽位（屏幕顺序，含滚动后的项）→ 显示名。

v29.1.2 实测顺序；槽位是绝对序号，滚动由引擎负责。
"""

from __future__ import annotations

ATTACK = "attack"
DEFENSE = "defense"
UTILITY = "utility"

ATTRIBUTES_BY_TAB = {
    ATTACK: [
        "Damage", "Attack Speed", "Critical Chance", "Critical Factor",
        "Range", "Damage Per Meter", "Multishot Chance", "Multishot Targets",
        "Rapid Fire Chance", "Rapid Fire Duration", "Bounce Shot Chance",
        "Bounce Shot Targets", "Bounce Shot Range",
    ],
    DEFENSE: [
        "Health", "Health Regen", "Defense %", "Defense Absolute",
        "Thorn Damage", "Lifesteal", "Knockback Chance", "Knockback Force",
        "Orb Speed", "Orbs", "Shockwave Size", "Shockwave Frequency",
        "Land Mine Chance", "Land Mine Damage", "Land Mine Radius",
        "Death Defy",
    ],
    UTILITY: [
        "Cash Bonus", "Cash / Wave", "Coins / Kill Bonus", "Coins/Wave",
        "Free Attack Upgrade", "Free Defense Upgrade", "Free Utility Upgrade",
        "Interest / Wave", "Recovery Amount", "Max Recovery",
        "Package Chance",
    ],
}

# 屏幕上的中文名（OCR 定位与 GUI 显示都用它）
DISPLAY_CN = {
    (ATTACK, 0): "伤害",
    (ATTACK, 1): "攻击速度",
    (ATTACK, 2): "暴击率",
    (ATTACK, 3): "暴击倍率",
    (ATTACK, 4): "射程",
    (ATTACK, 5): "伤害/米",
    (ATTACK, 6): "连射几率",
    (ATTACK, 7): "连射目标数",
    (ATTACK, 8): "速射几率",
    (ATTACK, 9): "速射持续时间",
    (ATTACK, 10): "弹射几率",
    (ATTACK, 11): "弹射目标数",
    (ATTACK, 12): "弹射射程",
    (DEFENSE, 0): "生命值",
    (DEFENSE, 1): "生命值再生",
    (DEFENSE, 2): "防御率",
    (DEFENSE, 3): "绝对防御",
    (DEFENSE, 4): "荆棘伤害",
    (DEFENSE, 5): "吸血",
    (DEFENSE, 6): "击退几率",
    (DEFENSE, 7): "击退力度",
    (DEFENSE, 8): "球体速度",
    (DEFENSE, 9): "球体",
    (DEFENSE, 10): "冲击波大小",
    (DEFENSE, 11): "冲击波频率",
    (DEFENSE, 12): "地雷几率",
    (DEFENSE, 13): "地雷伤害",
    (DEFENSE, 14): "地雷半径",
    (DEFENSE, 15): "死里逃生",
    (UTILITY, 0): "现金奖励",
    (UTILITY, 1): "现金/波次",
    (UTILITY, 2): "金币/击杀奖励",
    (UTILITY, 3): "金币/波次",
    (UTILITY, 4): "免费攻击升级",
    (UTILITY, 5): "免费防御升级",
    (UTILITY, 6): "免费功能升级",
    (UTILITY, 7): "利息/波次",
    (UTILITY, 8): "回复量",
    (UTILITY, 9): "回复上限",
    (UTILITY, 10): "包几率",
}

TAB_CN = {"attack": "攻击升级", "defense": "防御升级", "utility": "功能升级"}


def display_name(tab: str, slot: int) -> str:
    if (tab, slot) in DISPLAY_CN:
        return DISPLAY_CN[(tab, slot)]
    names = ATTRIBUTES_BY_TAB.get(tab, [])
    return names[slot] if 0 <= slot < len(names) else f"{tab}#{slot}"


def slot_count(tab: str) -> int:
    return len(ATTRIBUTES_BY_TAB.get(tab, []))
