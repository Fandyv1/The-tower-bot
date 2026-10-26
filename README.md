# The Tower PC Bot

Windows + BlueStacks 5 + ADB 的《The Tower - Idle Tower Defense》自动升级脚本。

**当前状态：核心 + 无人值守恢复 + 循环策略 + 多局/无限运行 + GUI 已可用**。
能读取真实等级、按价格变化确认购买、用弹窗校验目标等级；支持绝对/相对等级、
一层循环组、单局/指定局数/无限对局；能从 App Center、主页、继续上局、奖励弹窗、
加载页和游戏结束页恢复到局内；提供 Tkinter 策略编辑器。

---

## 一、能做什么

- 不占用真实鼠标：通过 `adb shell input` 注入点击，BlueStacks 最小化也能操作。
- 不依赖游戏内存/无障碍节点：ADB 截图 + OpenCV 几何定位 + RapidOCR 数字识别。
- 按有序策略执行：`防御/生命值再生 → 95`、`攻击/伤害 → 30`……
- 每次购买后核对价格变化；步骤结束打开等级弹窗核对真实等级。
- 支持 `dry-run`：只识别和打印计划，不点击购买。
- 自动恢复：游戏不在前台/加载中/主页/继续上局/云存档/奖励弹窗/等级弹窗/游戏结束；单局 Game Over 点「主页」，多局/无限点「重试」。
- ADB 断连、输入超时自动重连重试；文件锁避免重复运行；最长运行时间保护。
- 失败时停止并保存现场截图，不盲点。

## 二、环境要求

1. Windows 10/11。
2. BlueStacks 5，安装《The Tower》并更新到 `29.1.2` 或更新版本。
3. **开启 BlueStacks 的 ADB shell 访问**（关键，默认关闭）。

   GUI：BlueStacks 设置 → 高级 → 打开「Android Debug Bridge」。

   或完全退出 BlueStacks 后，编辑
   `C:\ProgramData\BlueStacks_nxt\bluestacks.conf`：

   ```ini
   bst.enable_adb_access="1"
   ```

   改完重启 BlueStacks。验证：

   ```powershell
   & "C:\Program Files\BlueStacks_nxt\HD-Adb.exe" connect 127.0.0.1:5555
   & "C:\Program Files\BlueStacks_nxt\HD-Adb.exe" shell echo hi
   ```

   应输出 `hi`。如果 `input`/`wm` 报 `error: closed`，说明 ADB 访问仍未开启。

4. 显示分辨率：让游戏跑成竖屏 `1080×2400` 截图。

   ```powershell
   & "C:\Program Files\BlueStacks_nxt\HD-Adb.exe" -s 127.0.0.1:5555 shell wm size 2400x1080
   & "C:\Program Files\BlueStacks_nxt\HD-Adb.exe" -s 127.0.0.1:5555 shell wm density 240
   ```

   游戏强制竖屏后，`screencap` 输出为 `1080x2400`。BlueStacks 重启后可能需要重新设置。

## 三、安装与自检

```powershell
cd "<项目目录>"
python -m pip install -r requirements.txt
python -m towerbot_pc.cli doctor
```

`doctor` 会输出：

- ADB 路径、设备、游戏版本、前台 Activity
- 实际截图尺寸
- 标题条、6 张卡片、购买点/等级入口点
- 标注图：`logs/doctor_layout.png`

如果 `doctor` 失败，先解决 ADB/分辨率/游戏前台，不要直接跑购买。

## 四、配置策略

首次使用先复制示例配置（`config/tower.json` 被 `.gitignore` 排除，不会提交个人策略）：

```powershell
Copy-Item config\tower.example.json config\tower.json
```

然后编辑 `config/tower.json`：

```json
{
  "strategy": {
    "steps": [
      {"tab": "defense", "slot": 1, "target": 95, "note": "生命值再生"},
      {"tab": "attack",  "slot": 0, "target": 30, "note": "伤害"}
    ]
  }
}
```

- `tab`：`attack` / `defense` / `utility`
- `slot`：该页签的**绝对槽位**，从 0 开始，按游戏内屏幕顺序
- `mode`：`absolute`（绝对等级，默认）或 `relative`（增加等级）
- `target`：绝对等级；`amount`：相对增加等级

循环组示例（每轮交替 +10，循环 3 轮）：

```json
{
  "strategy": {
    "steps": [
      {
        "type": "loop",
        "name": "攻击交替",
        "repeat": 3,
        "stop_on_all_targets": true,
        "steps": [
          {"type": "step", "tab": "attack", "slot": 0, "mode": "relative", "amount": 10, "note": "伤害"},
          {"type": "step", "tab": "attack", "slot": 1, "mode": "relative", "amount": 10, "note": "攻击速度"}
        ]
      }
    ]
  }
}
```

循环语义：

- 每轮按顺序执行全部子步骤；相对 +N 每轮都以“进入该步骤时的等级”重新计算。
- `repeat`：最多循环轮数；`0`/留空表示不按次数。
- `stop_on_all_targets`：所有**绝对目标**子步骤达成后提前结束；相对步骤不参与该判断。
- 子步骤也可以选绝对等级，例如“生命值到 500，再交替刷攻击”。
- v1 只支持一层循环，循环组里不能再放循环组。

| 页签 | 槽位与属性 |
| --- | --- |
| attack | 0 伤害 · 1 攻击速度 · 2 暴击率 · 3 暴击倍率 · 4 射程 · 5 伤害/米 · 6 连射几率 · 7 连射目标数 · 8 速射几率 · 9 速射持续时间 · 10 弹射几率 · 11 弹射目标数 · 12 弹射射程 |
| defense | 0 生命值 · 1 生命值再生 · 2 防御率 · 3 绝对防御 · 4 荆棘伤害 · 5 吸血 · 6 击退几率 · 7 击退力度 · 8 球体速度 · 9 球体 · 10 冲击波大小 · 11 冲击波频率 · 12 地雷几率 · 13 地雷伤害 · 14 地雷半径 · 15 死里逃生 |
| utility | 0 现金奖励 · 1 现金/波次 · 2 金币/击杀奖励 · 3 金币/波次 · 4 免费攻击升级 · 5 免费防御升级 · 6 免费功能升级 · 7 利息/波次 · 8 回复量 · 9 回复上限 · 10 包几率 |

槽位 0–3 直接用固定几何；更深的槽位会自动回顶，用 OCR 找到属性名后
再慢速滚动到可点击位置，不依赖固定页码。功能页卡片位置不同，也走同一套
OCR 定位逻辑。实现细节见 [docs/SCROLL.md](docs/SCROLL.md)。

## 五、运行

### GUI（推荐）

```powershell
run_gui.bat
```

![策略编辑 GUI](docs/gui.png)

- 策略页：添加步骤 / 添加循环 / 添加子步骤，树状显示，支持编辑、删除、上移、下移。
- 步骤编辑：绝对等级或增加等级。
- 循环编辑：名称、次数、是否“绝对目标全部达成后停止”。
- 运行区：单局 / 指定局数 / 无限；dry-run、开始 / 暂停 / 停止。
- 选择“指定局数”或“无限”时，最长运行时间自动禁用（避免误设上限）。
- 识别自检：生成 `logs/doctor_layout.png`，可一键打开。
- 日志区：实时显示每局、每轮、每一步的日志。

### 命令行

```powershell
# 只识别，不购买
python -m towerbot_pc.cli run --dry-run

# 实际执行；到 60 分钟自动安全退出
python -m towerbot_pc.cli run --max-runtime 60

# 共运行 3 局（当前局算第 1 局）；第 3 局自然结束后停止
python -m towerbot_pc.cli run --runs 3

# 无限对局，直到按停止键
python -m towerbot_pc.cli run --infinite

# 只做状态恢复（调试用）
python -m towerbot_pc.cli recover
```

日志：`logs/engine.log`。失败截图：`logs/fail_*.png`。

### 多局语义

- `单局`：策略执行完即停止，不等待死亡；如果策略执行中遇到 Game Over，点「主页」后停止，不自动重开。
- `指定局数 N`：当前局算第 1 局；每局策略完成后等待自然结束，再自动重开并从头执行策略；第 N 局自然结束后停止。
- `无限`：同上，但一直重开，直到点击停止；忽略 `runs.count` 和最长运行时间，只受停止键控制。
- 多局/无限模式遇到 Game Over 时点「重试」，自动开始下一局。
- 如果对局在策略执行中提前结束，也会计为一局，重开后从头执行策略，不会接着上一步继续。
- `dry-run` 只执行一轮策略，不等待死亡、不重开。
- 运行中可随时点「暂停」；暂停期间不会点击，点「继续」恢复；停止键在暂停状态下仍有效。

### 速度与等待

`config/tower.json` 的 `delays`：

```json
{
  "action_interval": 1.0,
  "after_tap": 0.35,
  "after_tab": 0.35,
  "after_popup_open": 0.40,
  "after_popup_close": 0.30,
  "idle_poll": 1.0,
  "insufficient_gold": 5.0
}
```

- 每次购买循环至少间隔 `action_interval` 秒；OCR 本身较慢时不会额外叠加等待。
- 金币不足统一等待 5 秒，不再 3 秒。

### 无人值守

```powershell
run_once.bat
```

配合 Windows 任务计划使用，详见 [docs/task_scheduler.md](docs/task_scheduler.md)。

## 六、恢复能力

| 场景 | 处理 |
| --- | --- |
| 游戏不在前台 / App Center | 自动启动游戏并等待加载 |
| Loading | 等待后重试 |
| 主页 | 点「接续战斗」 |
| 继续先前回合 | 点「继续」 |
| 云存档警告 | 点「是」加载 |
| 金币奖励弹窗 | 点右上角 X |
| 等级弹窗 | 返回键，确认关闭 |
| 游戏结束 | 点「重试」重新开局 |
| ADB 断连 | 重连后重试当前步骤 |
| 未知界面 | 两次仍无法识别 → 保存截图并停止 |

游戏结束页使用 `tests/frames/game_over.jpg` 做过离线识别/按钮选择验证；
主页、奖励弹窗、App Center 已在真机实测。

## 七、批量倍率说明

游戏每个页签有独立的批量购买倍率（实测攻击 `x10`、防御 `x5`）。
脚本会读取倍率，并把它计入等级估算。目标语义是「至少达到」，
因此实际等级可能超出目标最多 `倍率-1` 级。

## 八、已知限制 / 下一步

- [ ] 滚动依赖 OCR 能读到属性名；名字被遮挡/读错时会停止，不会盲点。
- [ ] 滚动搜索最多 14 个慢速步进；极深位置可能需要重试。
- [ ] 只支持一层循环，循环里不能再嵌套循环。
- [ ] 相对 +N 每轮重新计算；“全部达标”只检查绝对步骤。
- [ ] 多局/无限模式的“自然结束→重开”只用单元测试模拟过，尚未跑完真实多局全流程。
- [ ] 云存档弹窗的「是」是固定策略；如果你的存档冲突规则不同，请先手动处理。
- [ ] 误触内购保护：只点已识别区域；未知弹窗会停止，但无法 100% 防误触。
- [ ] 价格 OCR 仍可能误读；已改为优先取带 K/M 的完整数字，并在价格未确认时用等级弹窗兜底。读不到价格仍会停止。
- [ ] 进度持久化/断点续跑尚未实现；每轮由策略重新读等级。

## 九、安全提示

- 自动化可能违反游戏服务条款，存在账号风险；脚本不做反检测/绕过。
- 测试时曾误触「禁用广告 HK$78.00」支付页；已确认必须识别弹窗并避免盲点。
  正式无人值守前应先完成弹窗识别。
- 所有区域坐标来自 `1080×2400` 实测；换分辨率需重新校准。
