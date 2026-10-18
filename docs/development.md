# 开发和部署

本地项目：`/Users/bytedance/project/codex-watchdog`。GitHub：私有仓库 `Leeiieeo/codex-watchdog`。服务器源码：`~/project/codex-watchdog`。

代码、测试、CI 和说明进入 Git；`config.json`、`state/`、日志和密钥留在服务器，不提交到仓库。服务器拉取使用 SSH 认证。若使用部署密钥，应设置为只读，私钥仅留在服务器。

## 开发流程

1. 本地修改并运行 `PYTHONPATH=src python3 -m unittest discover -s tests -v`。
2. 检查 `git diff --check`、`sh -n control.sh run.sh`、`git status`，确认没有配置和状态文件被暂存。
3. 提交并推送到 GitHub；GitHub Actions 在 Python 3.10 和 3.13 上运行隔离测试。
4. 服务器在 `~/project/codex-watchdog` 中执行 `git pull --ff-only`，再次运行测试。
5. 验证后在 TUI 中按两次 Ctrl+C，或从另一终端执行 `./control.sh stop`，等待软件和 worker 退出，再执行 `./control.sh start`。保留配置和状态，避免把已有故障当成新故障。
6. 启动命令直接在当前终端打开 TUI；核对 `./control.sh status`、日志和目标列表。需要保持 SSH 断开后的运行时，自行创建 tmux 会话并在其中启动，再使用 tmux detach。

测试使用假的 Unix WebSocket 服务，不调用真实模型。现场验证优先查看状态；不要为了测试打断正常工作的研究会话。界面增删的现场测试应使用隔离配置或既有会话的幂等添加。

## 模块

- `watchdog.py`：观察、恢复策略、持久去重和复核。
- `rpc.py`：Unix WebSocket JSON-RPC 客户端。
- `registry.py`：自动发现主会话、锁保护的配置增删。
- `schedule.py`：默认 5 分钟的检查调度、间隔热更新和配置变更唤醒。
- `view.py`：只读状态、过期检测、日志和终端文本格式。
- `tui.py`：键盘管理界面，后台执行发现和配置操作。
- `control.py`：命令行、当前终端的 TUI 启动和退出信号处理。
- `service.py`：前台软件拥有的 worker 生命周期、单实例锁、异常重启和退出清理。
- `storage.py`：JSON 原子持久化。

配置修改具有文件锁；更新后守护进程会自行重读。恢复前还会检查目标是否仍在列表且注册版本未变化。TUI 移除同时核对注册版本，避免旧选择误移除一个已经被重新添加的会话。

## 回退

记录当前提交后再升级。如果需要回退，停止 worker、切换到已验证的提交，再 start。保留 `state/` 和原配置，不删除恢复记录。恢复状态机或 Codex 接口发生变化时，应先在隔离测试中验证兼容性。
