# Codex Watchdog

在 Linux 服务器上守护 Codex CLI 的 Goal，在明确的模型容量故障后自动重试。正常运行时只读监测。使用 Python 标准库，无第三方运行依赖。

## 启动

```sh
cp config.example.json config.json
# 根据服务器填写 socket_path；默认路径需要当前 Codex 实例支持。
./control.sh start
```

启动后直接在当前终端打开 TUI，同时启动守护服务。要求 Linux、Python 3.10+，以及已经运行的 Codex App Server。TUI 使用标准库 curses，支持 UTF-8 终端。`control.sh` 默认使用 `python3`；可以用 `CODEX_WATCHDOG_PYTHON` 指定解释器。

按两次 Ctrl+C 关闭软件：第一次显示提示，5 秒内再按一次，同时关闭 TUI 并停止守护服务。终端关闭或收到终止信号时也会清理服务。配置、恢复记录和日志保存在服务器本机，均被 Git 忽略。可以从另一终端执行 `./control.sh stop` 请求关闭软件；`./control.sh status` 查看状态。

如果需要通过 tmux 保持远程运行，由用户自行创建会话，再在其中启动：

```sh
tmux new -s my-watchdog
./control.sh start
```

使用 tmux 的 detach 可以保留软件运行。Watchdog 不创建、切换或管理 tmux 会话，tmux 是可选工具。

## TUI 管理

`./control.sh`、`./control.sh start`、`./control.sh tui`、`./run.sh` 和安装后的 `codex-watchdog` 都启动同一个软件：当前终端的 TUI 加守护服务。同一个状态目录只允许一个运行实例，重复启动会报错，不会增加 worker。

| 按键 | 操作 |
| --- | --- |
| ↑ / ↓、j / k | 选择会话 |
| a | 自动发现当前 Codex 服务中的主会话，从列表选择添加 |
| Enter | 主列表查看详情；添加列表确认添加 |
| d、y | 移除选中会话的守护；n 取消 |
| i | 修改检查间隔，输入分钟数；Enter 保存、Esc 取消 |
| / | 按项目、标题、路径或状态筛选；Enter 完成、Esc 清空 |
| l | 查看最近的守护日志 |
| r | 刷新；添加列表中重新发现会话 |
| Esc | 返回上一级 |
| Ctrl+C，Ctrl+C | 第一次提示；5 秒内第二次关闭 TUI 和守护服务 |

主界面显示项目、运行状态、Goal、守护状态、已重试次数和下次重试倒计时，并标明状态是否过期。顶部显示检查间隔和下次检查倒计时。回车可查看项目路径、完整会话信息和该会话的守护日志。支持终端缩放和中文会话标题。

默认每 **5 分钟**检查一次 Codex。按 `i` 可以直接输入分钟数，例如 `5` 或 `0.5`（30 秒）；支持 1 秒至 24 小时，按秒保存。设置自动写入配置，运行中的守护在约 1 秒内重新安排下次检查，不需要重启。增删会话也会及时触发一次检查来建立或更新基线。界面每秒读取本地状态文件更新倒计时，这不会额外检查 Codex。

添加不需要输入会话 ID：自动列出服务中已加载的主会话，包括正在工作和空闲会话，排除子 agent、未产生内容的空白草稿及已受管理会话。正在工作的会话优先显示；项目、最近活动时间和标题帮助区分同一项目的多个会话。它不是所有历史聊天的列表。发现过程在后台执行，连接异常可按 r 重试。移除直接选择受管理列表中的项目，离线时也可以操作。

添加和移除只修改本工具的配置，通常 5 秒内生效。重复添加保留当前观察基线；移除后重新添加会建立新基线。添加时已有的失败不会自动恢复。创建新 Codex chat 后需要在列表中添加那个新会话。

## 恢复边界

正常工作只读取状态，不向 Codex pane 发送按键或消息，不修改模型或预算。只有先观察到 active Goal，随后出现新的最终容量故障，经过退避与再次复核后，才恢复同一个 Goal。

退避间隔为 60、120、240、300 秒，附加 0–10% 随机延迟。实际重试发生在退避结束后的下一次检查，界面显示这个实际计划时间。每个失败轮次最多提交一次恢复；结果不明确时不会盲目重复提交。人工暂停、完成、预算耗尽、非容量错误、读取不一致都会阻止恢复。兼容 Codex 0.162.1 的 `systemError + blocked` 容量失败；不满足时间关联条件的一般阻塞不会恢复。

界面刷新只读取本地状态文件。显式发现/添加时调用只读接口；移除不需要连接 Codex。守护服务随软件启动和退出，异常退出后由前台软件等待 10 秒重启。服务器重启后需要重新 start。

## 配置与部署

默认配置为仓库根目录的 `config.json`；可以通过 `CODEX_WATCHDOG_CONFIG` 指向仓库外的配置。相对路径以配置文件所在目录解析。`check_interval_seconds` 保存检查间隔，未配置时使用 `300`。`state_dir` 保存恢复基线、去重记录和轮转日志，更新代码时应保留。

已有运行目录可以保留配置和 state，再让新仓库引用原配置：

```sh
git clone git@github.com:Leeiieeo/codex-watchdog.git ~/project/codex-watchdog
cd ~/project/codex-watchdog
ln -s ~/scripts/codex-watchdog/config.json config.json
./control.sh start
```

迁移已运行实例时先停止旧 worker，保留状态文件，再启动新版本。升级前先测试，使用 `git pull --ff-only`，验证后重启 watchdog worker；Codex 本身继续运行。详细步骤见 [开发和部署](docs/development.md)。

命令行也保留 `discover`、`add THREAD_ID [LABEL]`、`remove THREAD_ID`、`check`、`logs`，便于脚本调用；日常增删使用 TUI 即可。

## 开发

```sh
PYTHONPATH=src python3 -m unittest discover -s tests -v
sh -n control.sh run.sh
```

恢复规则见 [docs/recovery-design.md](docs/recovery-design.md)。
