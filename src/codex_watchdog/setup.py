"""Configuration locations and a first-run terminal setup form."""
import curses
import os
from pathlib import Path
import signal
import time

from .storage import atomic_json
from .view import fit


def default_config():
    override = os.environ.get('CODEX_WATCHDOG_CONFIG')
    if override:
        return override
    root = Path(__file__).resolve().parents[2]
    if (root/'pyproject.toml').is_file() and (root/'control.sh').is_file():
        return str(root/'config.json')
    return str(Path(os.environ.get('XDG_CONFIG_HOME', '~/.config')).expanduser()
               /'codex-watchdog/config.json')


class Setup:
    def __init__(self, path):
        self.path = Path(path)
        self.values = ['~/.codex', '~/.codex/app-server-control/app-server-control.sock',
                       str(self.path.parent/'state')]
        self.index = 0
        self.authorized = False
        self.quit = False
        self.confirm_until = 0
        self.error = ''

    def interrupt(self):
        now = time.monotonic()
        if now < self.confirm_until:
            self.quit = True
        else:
            self.confirm_until = now+5

    def run(self, screen):
        screen.keypad(True)
        screen.timeout(150)
        while not self.quit:
            screen.erase()
            height, width = screen.getmaxyx()
            lines = ['CODEX WATCHDOG / 首次配置', '配置文件：'+str(self.path), '',
                     'Codex 目录：'+self.values[0], 'Socket 路径：'+self.values[1],
                     '状态目录：'+self.values[2],
                     '自动恢复容量故障：'+('已授权' if self.authorized else '未授权，仅观察'), '',
                     '↑↓/Tab 选择，路径可直接编辑，空格切换自动恢复授权',
                     'Enter 保存并继续；未授权时只记录，不自动恢复',
                     '5 秒内再按 Ctrl+C 取消' if time.monotonic() < self.confirm_until else
                     '按两次 Ctrl+C 取消配置', self.error]
            for y, line in enumerate(lines):
                if y < height-1:
                    attr = curses.A_REVERSE if y == self.index+3 else 0
                    try:
                        screen.addstr(y, 0, fit(line, max(0, width-1)), attr)
                    except curses.error:
                        pass
            screen.refresh()
            try:
                key = screen.get_wch()
            except curses.error:
                continue
            if key == '\x03':
                self.interrupt()
            elif key in (curses.KEY_DOWN, '\t'):
                self.index = (self.index+1)%4
            elif key == curses.KEY_UP:
                self.index = (self.index-1)%4
            elif key == ' ' and self.index == 3:
                self.authorized = not self.authorized
            elif key in ('\n', '\r', curses.KEY_ENTER):
                if not all(value.strip() for value in self.values):
                    self.error = '路径不能为空'
                    continue
                self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
                if self.path.exists():
                    raise ValueError('配置文件已存在，未覆盖')
                atomic_json(self.path, {
                    'schema_version': 1, 'user_authorized_capacity_resume': self.authorized,
                    'observe_only': not self.authorized, 'codex_home': self.values[0].strip(),
                    'socket_path': self.values[1].strip(), 'state_dir': self.values[2].strip(),
                    'check_interval_seconds': 300, 'targets': []})
                return True
            elif self.index < 3:
                if key in (curses.KEY_BACKSPACE, '\x7f', '\b'):
                    self.values[self.index] = self.values[self.index][:-1]
                elif key == '\x15':
                    self.values[self.index] = ''
                elif isinstance(key, str) and key.isprintable() and len(self.values[self.index]) < 4096:
                    self.values[self.index] += key
        return False


def initialize(path):
    if Path(path).exists():
        raise ValueError('配置文件已存在，未覆盖')
    import locale
    locale.setlocale(locale.LC_ALL, '')
    app = Setup(path)
    handlers = {}
    try:
        handlers[signal.SIGINT] = signal.signal(signal.SIGINT, lambda *_: app.interrupt())
        for sig in (signal.SIGHUP, signal.SIGTERM):
            handlers[sig] = signal.signal(sig, lambda *_: setattr(app, 'quit', True))
        return curses.wrapper(app.run)
    finally:
        for sig, handler in handlers.items():
            signal.signal(sig, handler)
