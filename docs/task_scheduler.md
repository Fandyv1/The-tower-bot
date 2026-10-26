# Windows 任务计划（无人值守）

## 前提

- BlueStacks 已安装并登录，游戏已更新。
- 已开启 BlueStacks 的 ADB 访问（`bst.enable_adb_access="1"`）。
- `config/tower.json` 里的策略已填写，并先用 `python -m towerbot_pc.cli run --dry-run` 验证。
- 计划任务必须“仅在用户登录时运行”，因为 BlueStacks 需要桌面会话。

## 创建任务

以当前用户身份运行（不需要管理员）：

```powershell
schtasks /Create /TN "TheTowerBot" /TR "\"C:\Tools\TheTowerBot\run_once.bat\"" /SC ONLOGON /RL LIMITED /F
```

- `ONLOGON`：登录后自动跑一轮。
- 如果希望每天固定时间：
  ```powershell
  schtasks /Create /TN "TheTowerBot" /TR "\"C:\Tools\TheTowerBot\run_once.bat\"" /SC DAILY /ST 09:00 /RL LIMITED /F
  ```
- 查看/删除：
  ```powershell
  schtasks /Query /TN "TheTowerBot" /V /FO LIST
  schtasks /Delete /TN "TheTowerBot" /F
  ```

## 行为

- `run_once.bat` 执行 `run --max-runtime 480`，日志追加到 `logs/task.log`。
- 引擎有文件锁 `logs/engine.lock`，不会同时跑两份。
- 退出码：
  - `0` 完成/被用户停止
  - `2` 配置或 ADB 错误
  - `3` 不可恢复（状态未知、连续购买失败等）
  - `4` 达到最长运行时长
  - `5` 已有实例在运行

## 注意

- 任务运行时请保持 BlueStacks 进程存活；窗口可以最小化。
- 如果 BlueStacks 重启后 ADB 端口改变，修改 `config/tower.json` 的 `adb.port`，或重新运行 `doctor`。
- 首次无人值守前，建议先跑一次 10 分钟的 `run --max-runtime 10` 观察日志。
