# MAA-Remote

在飞书里对机器人说一句话（如"跑一下日常"），本地服务就自动拉起 MuMu 模拟器、用 maa-cli 跑明日方舟日常，跑完把结果润色成自然语言回到飞书。

- 设计背景与决策：[CONTEXT.md](CONTEXT.md)
- 详细设计（模块接口/配置/错误矩阵）：[SPEC.md](SPEC.md)
- 实现计划（照着逐任务执行）：[docs/plans/2026-07-04-maa-remote.md](docs/plans/2026-07-04-maa-remote.md)
- 抄作业打活动关（设计草案，首批 headless Spike 已完成）：[docs/superpowers/specs/2026-07-07-copilot-auto-battle-design.md](docs/superpowers/specs/2026-07-07-copilot-auto-battle-design.md)，证据见 [spikes/SPIKE_REPORT.md](spikes/SPIKE_REPORT.md)

## 前置依赖

| 依赖 | 说明 |
|---|---|
| Python 3.11+（本机 3.14） | 运行本服务 |
| Node + `@larksuite/cli`（lark-cli ≥1.0.63，全局） | 飞书收发消息，已 `auth login`（bot 可用） |
| maa-cli v0.7.5+ | 跑游戏；本机复用 MAA GUI 的 MaaCore（配置 `core_dir`） |
| MuMu 12 模拟器 | 平时可以关着，服务会自动拉起 |
| DeepSeek API key | 设为环境变量 `DEEPSEEK_API_KEY`（系统级，别写进任何文件） |

## 安装

```bash
cd MAA-remote
python -m venv .venv
.venv\Scripts\pip install -r requirements.txt
```

## 配置

```bash
copy config.example.toml config.toml
# 按本机实际路径/端口修改 config.toml（见 SPEC.md §0 的探测方法）
```

注意：`config.toml` 是机器专属、已在 `.gitignore`，不会被提交；含空格的 exe 路径要加双引号（模板里有示例）。

## 运行

```bash
# 前台运行（调试用，日志同时打到控制台和 logs/maa_remote.log）
.venv\Scripts\python -m maa_remote
```

或者直接双击 `start.bat`。

启动后在飞书 DM 机器人发"跑日常"即可。执行中再发消息会收到"正在跑中"；碎石/动囤药的指令会先让你回"确认"。

## 开机自启（可选）

用 Windows 任务计划程序注册（管理员 PowerShell）：

```powershell
schtasks /Create /TN "MAA-Remote" /TR "E:\code\MAA-remote\start.bat" /SC ONLOGON /RL LIMITED
```

删除：`schtasks /Delete /TN "MAA-Remote" /F`

## 日志

- 运行日志：`logs/maa_remote.log`（滚动，UTF-8）。出问题先看这里。
- maa 本身的日志：跟随 maa-cli（`%APPDATA%/loong/maa/debug`）。

## 意图识别回归（改 prompt/schema 后必跑）

```bash
.venv\Scripts\python -m maa_remote.eval_router
```

## 单元测试

```bash
.venv\Scripts\python -m pytest -v
```
