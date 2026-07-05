# MAA progress signal notes

Captured on 2026-07-05 with `.venv/Scripts/python scripts/capture_maa_output.py`.

## Signal source

- `maa-cli` stdout was not useful for progress parsing in this run: `logs/maa_stdout_capture.txt` stayed at 0 bytes.
- The run produced structured TaskChain events in MaaCore `asst.log`.
- Signal source for implementation: tail `C:\Users\Blonde127\AppData\Roaming\loong\maa\data\debug\asst.log`.
- `MAA_LOG=debug` is not required for TaskChainStart/TaskChainCompleted in this environment.
- 本次抓取运行进入 `Fight` 后超过一小时仍在继续，原因是账号较久未登录、理智大量溢出；MAA 正常代理刷了很多轮以清理溢出理智，并非卡死。抓到足够进度信号后，手动停止了抓取脚本启动的 `maa.exe` 进程。fixture 因此保留前面已完成链路和 `Fight` start。

## Event examples

Start:

```text
[2026-07-05 15:03:39.871][INF][Px1044][Tx49609] Assistant::append_callback | TaskChainStart {"taskchain":"Recruit","taskid":2,"uuid":"222203e1afbe086d"}
```

Completed:

```text
[2026-07-05 15:04:30.020][INF][Px1044][Tx49609] Assistant::append_callback | TaskChainCompleted {"taskchain":"Recruit","taskid":2,"uuid":"222203e1afbe086d"}
```

No TaskChainError was observed in this capture. Parser tests use a synthetic `TaskChainError` line with the same callback shape.
