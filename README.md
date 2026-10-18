# Codex Watchdog

在 Linux 服务器上守护 Codex CLI 的 Goal，在明确的模型容量故障后自动重试。正常运行时只读监测。使用 Python 标准库，无第三方运行依赖。

## 启动

```sh
cp config.example.json config.json
# 根据服务器填写 socket_path；默认路径需要当前 Codex 实例支持。
./control.sh start
tmux attach -t codex-watchdog
```

要求 Linux、Python 3.10+、tmux，以及已经运行的 Codex App Server。TUI 使用标准库 curses，支持 UTF-8 终端。`control.sh` 默认使用 `python3`；可以用 `CODEX_WATCHDOG_PYTHON` 指定解释器。

配置、恢复记录和日志保存在服务器本机，均被 Git 忽略。`./control.sh stop` 停止守护；`./control.sh status` 查看状态。

## TUI 管理

`./control.sh tui`（或不带参数执行 `./control.sh`）打开管理界面。`start` 会在独立 tmux 会话中启动守护，并打开管理窗口。

| 按键 | 操作 |
| --- | --- |
| ↑ / ↓、j / k | 选择会话 |
| a | 自动发现当前 Codex 服务中的主会话，从列表选择添加 |
| Enter | 主列表查看详情；添加列表确认添加 |
| d、y | 移除选中会话的守护；n 取消 |
| / | 按项目、标题、路径或状态筛选；Enter 完成、Esc 清空 |
| l | 查看最近的守护日志 |
| r | 刷新；添加列表中重新发现会话 |
| Esc | 返回上一级 |
| q | 返回或退出界面；守护继续运行 |

主界面显示项目、运行状态、Goal、守护状态、已重试次数和下次重试倒计时，并标明状态是否过期。回车可查看项目路径、完整会话信息和该会话的守护日志。支持终端缩放和中文会话标题。

添加不需要输入会话 ID：自动列出服务中已加载的主会话，包括正在工作和空闲会话，排除子 agent 及已受管理会话。它不是所有历史聊天的列表。发现过程在后台执行，连接异常可按 r 重试。移除直接选择受管理列表中的项目，离线时也可以操作。

添加和移除只修改本工具的配置，通常 5 秒内生效。重复添加保留当前观察基线；移除后重新添加会建立新基线。添加时已有的失败不会自动恢复。创建新 Codex chat 后需要在列表中添加那个新会话。

## 恢复边界

正常工作只读取状态，不向 Codex pane 发送按键或消息，不修改模型或预算。只有先观察到 active Goal，随后出现新的最终容量故障，经过退避与再次复核后，才恢复同一个 Goal。

退避间隔为 60、120、240、300 秒，附加 0–10% 随机延迟。每个失败轮次最多提交一次恢复；结果不明确时不会盲目重复提交。人工暂停、完成、预算耗尽、非容量错误、读取不一致都会阻止恢复。兼容 Codex 0.162.1 的 `systemError + blocked` 容量失败；不满足时间关联条件的一般阻塞不会恢复。

浏览界面不连接 Codex，只读取本地状态文件。只有显式发现/添加时调用只读接口；移除不需要连接 Codex。关闭界面不会停止守护。守护当前通过 tmux 运行，服务器重启后需要重新 start。

## 配置与部署

默认配置为仓库根目录的 `config.json`；可以通过 `CODEX_WATCHDOG_CONFIG` 指向仓库外的配置。相对路径以配置文件所在目录解析。`state_dir` 保存恢复基线、去重记录和轮转日志，更新代码时应保留。

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
