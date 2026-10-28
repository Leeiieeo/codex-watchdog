# 开发和部署

本地项目：`/Users/bytedance/project/codex-watchdog`。GitHub：私有仓库 `Leeiieeo/codex-watchdog`。服务器源码：`~/project/codex-watchdog`。

代码、测试、CI 和说明进入 Git；`config.json`、`state/`、日志和密钥留在服务器，不提交到仓库。服务器拉取使用 SSH 认证。若使用部署密钥，应设置为只读，私钥仅留在服务器。

## 开发流程

1. 本地修改并运行 `PYTHONPATH=src python3 -m unittest discover -s tests -v`。
   提交使用 Conventional Commits，例如 `feat(watchdog): explain recovery decisions`、`fix(schedule): honor retry deadlines`。标题描述具体行为；正文记录影响和必要验证，不改写已发布历史。
2. 检查 `git diff --check`、`sh -n control.sh run.sh`、`git status`，确认没有配置和状态文件被暂存。
3. 提交并推送到 GitHub；GitHub Actions 在 Python 3.10 和 3.13 上运行隔离测试。
4. 服务器在 `~/project/codex-watchdog` 中执行 `git pull --ff-only`，再次运行测试。
5. 验证后在 TUI 中按两次 Ctrl+C，或从另一终端执行 `codex-watchdog stop`，等待软件和 worker 退出，再执行 `npm install -g .` 更新安装，最后运行 `codex-watchdog`。保留配置和状态，避免把已有故障当成新故障。
6. 启动命令直接在当前终端打开 TUI；核对 `codex-watchdog status`、日志和目标列表。需要保持 SSH 断开后的运行时，自行创建 tmux 会话并在其中启动，再使用 tmux detach。

测试使用假的 Unix WebSocket 服务，不调用真实模型。现场验证优先查看状态；不要为了测试打断正常工作的研究会话。界面增删的现场测试应使用隔离配置或既有会话的幂等添加。

## 安装与开发入口

npm 入口使用 `npm install -g .` 安装，`npm start` 在 checkout 内启动。Node.js 启动器只负责定位兼容 Python、选择用户配置、转发终端信号和等待退出，Goal 观察与恢复仍由 Python 核心执行。npm 包白名单仅包含 `bin/`、Python 源码、说明和示例配置，排除本机配置、状态和测试。

本地目录全局安装通常链接到 checkout；保留目录，更新后重新 `npm install -g .`。独立安装可用 `npm pack` 后安装 tgz。npm 包保持 `private: true`，当前不发布 registry；要公开发布必须先核对包名、访问范围和发布内容。npm 安装不运行生命周期脚本、不下载解释器；要求已有 Python 3.10+，可用 `CODEX_WATCHDOG_PYTHON` 指定。

Python 安装仍支持 `uv tool install --python 3.12 .` / `pipx install .`；选择此路径不需要 Node.js。Python 开发入口可用 `./control.sh` 或 editable 安装。避免同时安装两个相同的 `codex-watchdog` 命令；切换安装方式后用 `command -v codex-watchdog` 检查当前入口。

CI 使用 Node.js 22 运行 npm 打包、解释器检查和临时目录全局安装测试，再运行 Python 测试（包括 npm 信号与生命周期）。Python 包入口也独立安装验证。npm 默认配置与 Python 包默认配置一致，已有部署应使用 `--config` 或 `CODEX_WATCHDOG_CONFIG` 沿用原路径，保留恢复去重记录。

## 模块

- `watchdog.py`：观察、恢复策略、持久去重和复核。
- `patterns.py`：恢复策略和界面展示共用的失败错误码与提示文本。
- `decisions.py`：稳定的恢复判定原因与显示文本。
- `events.py`：有界事件时间线与累计指标。
- `notifications.py`：默认关闭的冷却通知和异步 Webhook。
- `diagnostics.py`：只读连接、接口、数据库与状态诊断。
- `setup.py`：首次配置表单和源码/安装模式的默认配置位置。
- `rpc.py`：Unix WebSocket JSON-RPC 客户端。
- `registry.py`：自动发现主会话、锁保护的配置增删。
- `schedule.py`：默认 5 分钟的检查调度、间隔热更新和配置变更唤醒。
- `view_models.py`：不可变的会话、Goal、失败和统计显示模型，以及 JSON/发现结果到模型的字段校验。
- `view.py`：只读状态加载、进程与过期检测、显示模型组装、日志和终端文本格式。
- `tui.py`：键盘管理界面，后台执行发现和配置操作。
- `ui_errors.py`：界面异常的脱敏、限频和轮转日志。
- `renderer.py`：响应式状态卡片、会话列表、侧栏、分组详情与弹窗。
- `presentation.py`：终端绘制、文本宽度、换行和状态标签。
- `theme.py`：状态语义配色、深浅主题及无彩色降级。
- `control.py`：命令行、当前终端的 TUI 启动和退出信号处理。
- `service.py`：前台软件拥有的 worker 生命周期、单实例锁、异常重启和退出清理。
- `storage.py`：JSON 原子持久化。

配置修改具有文件锁；更新后守护进程会自行重读。恢复前还会检查目标是否仍在列表且注册版本未变化。TUI 移除同时核对注册版本，避免旧选择误移除一个已经被重新添加的会话。

升级到 0.3.0 时，旧版恢复状态迁移为 schema_version=1，保留 pending 与 attempted。回退至 0.2.x 前应在软件停止后转换状态封装为旧格式；不能直接将新版封装交给旧 worker，更不能删除状态。迁移测试必须覆盖仍在退避的故障与已提交但响应丢失的轮次。

界面改动验证 70×18、80×24、130×30 和 180×45，确认中文宽度、长英文错误换行、选中规则滚动、居中弹窗和窗口缩放。配色覆盖 256 色、8 色和 NO_COLOR；终端集成测试同时验证主题切换和退出清理。字体和字号属于用户终端设置，不写入其终端配置。

## 显示数据与异常边界

worker 的持久化格式与恢复策略保持独立。`view.py` 读取已有 JSON，通过 `view_models.py` 校验后生成 `DashboardView` / `SessionView`；TUI 和 renderer 使用这些模型的属性，不直接合并配置、观察结果或原始字典。配置中的 thread ID、cwd、注册版本和守护开关是身份依据，观察结果不能覆盖它们。`-` 等占位符只在展示时产生。

`Observation` 区分 pending、current、failed、stale；`GoalPresence` 区分 unknown、absent、present。发现列表还没有读取 Goal，因此是 unknown；成功观察返回空 Goal 才是 absent。读取失败或过期不能转换为“无 Goal”或正常工作。未知的字符串枚举保留原文，错误类型、非有限时间戳和无效统计在边界被拒绝并降级为会话错误行。旧状态文件无需迁移；`status` 命令保留会话字段，并增加 `observation` 和 `goal_presence`。

绘制异常在 `App.draw` 边界记录到 `ui.log`，进入独立于 renderer 的简化界面。简化界面只接收重试、返回、日志和退出操作，重试时清除不可见的确认弹窗。服务轮询继续运行；退出、父进程消失和无法使用终端时仍执行已有清理。这个边界不能用于吞掉恢复状态损坏、配置授权失败或 worker 执行异常。

回归测试既覆盖原始状态文件到模型再到主界面/详情的链路，也在伪终端中连接假的 Unix WebSocket 服务，完成发现、添加、无 Goal 的首次观察、暂停、启用、移除和退出。另有真实 worker 的界面故障注入测试，验证简化界面、重试与退出清理。测试必须使用隔离配置和临时数据库，不连接用户的真实会话。

## 回退

记录当前提交后再升级。如果需要回退，停止 worker、切换到已验证的提交，再 start。保留 `state/` 和原配置，不删除恢复记录。恢复状态机或 Codex 接口发生变化时，应先在隔离测试中验证兼容性。
