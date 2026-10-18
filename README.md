# Codex Watchdog

在 Linux 服务器上守护 Codex CLI 的 Goal，在明确的模型容量故障后自动重试。正常运行时只读监测。使用 Python 标准库，无第三方运行依赖。

## 启动

```sh
cp config.example.json config.json
# 根据服务器填写 socket_path；默认路径需要当前 Codex 实例支持。
./control.sh start
tmux attach -t codex-watchdog
```

配置、恢复记录和日志保存在本机，均被 Git 忽略。`./control.sh stop` 停止守护；`./control.sh status` 查看状态。

## 开发

```sh
PYTHONPATH=src python3 -m unittest discover -s tests -v
```

恢复规则见 [docs/recovery-design.md](docs/recovery-design.md)。
