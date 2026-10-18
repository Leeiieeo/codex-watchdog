# Codex Watchdog

在 Linux 服务器上守护 Codex CLI 的 Goal，在明确的模型容量故障后自动重试。正常运行时只读监测。使用 Python 标准库，无第三方运行依赖。

## 启动

```sh
./control.sh start
```

配置不存在时会打开首次配置表单，填写 Codex 目录、Unix socket 路径和状态目录。默认只观察；勾选容量故障自动恢复授权后才会恢复 Goal。也可复制 `config.example.json` 手动配置。`./control.sh init` 单独运行配置表单，已有配置不会被覆盖；`./control.sh doctor` 执行只读诊断。

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
| p | 暂停或启用选中会话的自动守护；继续读取状态并保留历史 |
| i | 修改检查间隔，输入分钟数；Enter 保存、Esc 取消 |
| / | 按项目、标题、路径或状态筛选；Enter 完成、Esc 清空 |
| e | 查看事件时间线；详情页内仅显示该会话 |
| l、s | 查看日志；日志页按 s 切换守护日志和服务启动日志 |
| P、m | 查看失败规则；规则页按 m 切换选中规则的处理模式 |
| P 后 n / b / t | 开关通知 / 终端响铃 / 设置停顿提醒阈值 |
| D | 执行只读运行诊断 |
| ? | 查看完整键盘帮助 |
| ↑ / ↓、PgUp / PgDn | 详情、事件、日志和诊断页滚动 |
| r | 刷新；添加列表中重新发现会话 |
| Esc | 返回上一级 |
| Ctrl+C，Ctrl+C | 第一次提示；5 秒内第二次关闭 TUI 和守护服务 |

顶部区分守护服务是否运行、会话检查成功数量、最近尝试与最近成功检查时间，并显示检查间隔和下次检查。容量 pattern 及处理模式始终可见。回车查看最近失败、匹配规则、具体恢复判定原因、累计失败/恢复统计和观察到的停顿时间。详情和历史页面支持滚动，兼容中文标题和最小 70 列、18 行终端。

事件时间线记录失败、安排恢复、提交、接受、观察到新轮次、恢复后轮次完成，以及 Goal 完成、守护开关和读取异常。累计统计独立于退避次数，正常轮次完成不会清零。停顿时长以轮询观察为依据，可能包含检查延迟，不代表精确的模型停机时间。

默认每 **5 分钟**检查一次 Codex。按 `i` 可以直接输入分钟数，例如 `5` 或 `0.5`（30 秒）；支持 1 秒至 24 小时，按秒保存。设置自动写入配置，运行中的守护在约 1 秒内重新安排下次检查，不需要重启。增删会话也会及时触发一次检查来建立或更新基线。界面每秒读取本地状态文件更新倒计时，这不会额外检查 Codex。

添加不需要输入会话 ID：自动列出服务中已加载的主会话，包括正在工作和空闲会话，排除子 agent、未产生内容的空白草稿及已受管理会话。正在工作的会话优先显示；项目、最近活动时间和标题帮助区分同一项目的多个会话。它不是所有历史聊天的列表。发现过程在后台执行，连接异常可按 r 重试。移除直接选择受管理列表中的项目，离线时也可以操作。

添加和移除只修改本工具的配置，通常 5 秒内生效。重复添加保留当前观察基线；移除后重新添加会建立新基线。添加时已有的失败不会自动恢复。创建新 Codex chat 后需要在列表中添加那个新会话。

暂停守护保留会话、去重记录和历史，并继续读取状态。重新启用守护时把当前轮次作为基线，不补恢复暂停期间的失败。恢复提交前会核对开关版本，避免暂停与重试发生竞争。

## 规则与提醒

| 失败分类 | 默认处理 |
| --- | --- |
| 模型容量不足：`serverOverloaded` 或完整容量提示 | 已授权时自动恢复 |
| 模型请求连接失败、响应流断开 | 仅记录 |
| 配额、速率或计费限制 | 仅记录 |
| 认证失败 | 仅记录 |
| 服务内部错误 | 仅记录 |
| 未匹配的最终失败 | 保留错误证据供检查 |

`P` 查看规则和明确错误码，`m` 切换处理模式；容量规则支持自动恢复、仅记录、禁用，其余规则支持仅记录和禁用。全局 `observe_only` 模式会阻止所有恢复。新增连接分类不改变 Goal 恢复边界：watchdog 自身连不上 Codex 时，只尝试重连并显示监控中断。

通知默认关闭。规则页面按 `n` 开启后，持续无法监控、长时间未恢复、需人工处理和 Goal 完成会生成通知事件；`b` 可启用终端响铃。每会话/主题有持久冷却时间，普通检查不通知。可在启动前设置 `CODEX_WATCHDOG_WEBHOOK_URL`，开启通知后异步发送通用 JSON Webhook；地址只从环境变量读取，不写入配置或日志。发送失败记录错误类型，最多等待 5 秒，不阻塞检查。

Webhook JSON 包含 `event`、`thread_id`、`at` 和简短 `message`，不会发送完整会话内容。它使用通用格式；接入有专用消息格式的平台时，可在接收端转换。

停顿提醒默认关闭，在规则页按 `t` 输入分钟数，`0` 关闭。轮次、token 用量或会话活动时间长期不变化时只提醒，不恢复正在运行的轮次。长工具调用也可能触发提示，应结合 Codex 实际状态判断。

## 恢复边界

正常工作只读取状态，不向 Codex pane 发送按键或消息，不修改模型或预算。只有先观察到 active Goal，随后出现新的最终容量故障，经过退避与再次复核后，才恢复同一个 Goal。

退避间隔为 60、120、240、300 秒，附加 0–10% 随机延迟。退避到期会提前唤醒复核，不必等待正常 5 分钟检查；倒计时显示独立的恢复计划。读取异常时约 30 秒后重连。恢复接受后最多每 10 秒检查新轮次，超过确认时限则提示并恢复正常检查频率。每个失败轮次最多提交一次恢复；结果不明确时不会盲目重复提交。人工暂停、完成、预算耗尽、非容量错误、读取不一致都会阻止恢复。兼容 Codex 0.162.1 的 `systemError + blocked` 容量失败；不满足时间关联条件的一般阻塞不会恢复。

界面刷新只读取本地文件。显式发现、添加和诊断时调用只读接口；移除和暂停不需要连接 Codex。守护服务随软件启动和退出，异常退出后依次等待 10、20 秒重启；连续三次快速退出则保留 TUI 并停止自动重启。服务日志追加并轮转，启动错误不会在重启时被覆盖。服务器重启后需要重新 start。

## 配置与部署

源码运行默认使用仓库根目录的 `config.json`；安装为 Python 包后默认使用 `$XDG_CONFIG_HOME/codex-watchdog/config.json`（未设置时为 `~/.config/codex-watchdog/config.json`）。可以通过 `--config` 或 `CODEX_WATCHDOG_CONFIG` 指向仓库外的配置。相对路径以配置文件所在目录解析。

`check_interval_seconds` 默认 `300`；`recovery_confirmation_seconds` 默认 `300`；`attention_after_seconds` 默认 `1800`；`stall_after_seconds` 默认 `0`。`state_dir` 保存带版本的恢复基线、去重记录、事件时间线、累计统计和轮转日志，更新代码时应保留。旧版无版本状态自动迁移并保留已尝试轮次；损坏或未知版本会阻止服务启动并保留原文件，通过 doctor 排查或恢复已验证的备份，不能删除去重记录来绕过错误。

已有运行目录可以保留配置和 state，再让新仓库引用原配置：

```sh
git clone git@github.com:Leeiieeo/codex-watchdog.git ~/project/codex-watchdog
cd ~/project/codex-watchdog
ln -s ~/scripts/codex-watchdog/config.json config.json
./control.sh start
```

迁移已运行实例时先停止旧 worker，保留状态文件，再启动新版本。升级前先测试，使用 `git pull --ff-only`，验证后重启 watchdog worker；Codex 本身继续运行。详细步骤见 [开发和部署](docs/development.md)。

命令行支持 `discover`、`add THREAD_ID [LABEL]`、`remove THREAD_ID`、`pause THREAD_ID`、`resume THREAD_ID`、`check`、`logs`、`events [THREAD_ID]`、`doctor` 和 `init`；其中 pause/resume 仅控制 watchdog 守护开关。日常操作使用 TUI 即可。

## 开发

```sh
PYTHONPATH=src python3 -m unittest discover -s tests -v
sh -n control.sh run.sh
```

恢复规则见 [docs/recovery-design.md](docs/recovery-design.md)。
