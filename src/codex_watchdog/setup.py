"""Configuration locations and a first-run terminal setup form."""
import curses
import os
from pathlib import Path
import signal
import time

from .storage import atomic_json
from .view import cells
from .presentation import Canvas
from .theme import Palette


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
        self.palette = Palette()

    def interrupt(self):
        now = time.monotonic()
        if now < self.confirm_until:
            self.quit = True
        else:
            self.confirm_until = now+5

    def draw(self, screen):
        c = Canvas(screen, self.palette)
        screen.erase()
        h,w = screen.getmaxyx()
        c.text(0,2,'CODEX WATCHDOG',role='heading',bold=True)
        c.text(0,19,'/ 首次配置',role='muted')
        if h < 18 or w < 70:
            c.paragraph(3,2,'请将终端调整到至少 70 列、18 行。',max(1,w-4),role='warning')
            c.paragraph(6,2,'5 秒内再按 Ctrl+C 取消' if time.monotonic()<self.confirm_until else
                        '按两次 Ctrl+C 取消配置',max(1,w-4),role='warning')
            screen.refresh()
            return
        panel_width = min(88,w-6)
        x = (w-panel_width)//2
        roomy = h >= 26
        y = max(2,(h-(23 if roomy else 15))//2)
        c.text(y,x,'连接你的 Codex',panel_width,role='heading',bold=True)
        c.text(y+1,x,'设置本机路径，选择容量故障的处理方式。',panel_width,'muted')
        top = y+3
        height = 15 if roomy else 9
        c.box(top,x,height,panel_width,'运行配置')
        labels = ('Codex 目录','Unix socket','状态目录')
        for n,label in enumerate(labels):
            row = top+1+n*(3 if roomy else 2)
            selected = self.index==n
            value = self.values[n]
            if roomy:
                c.text(row,x+3,label,panel_width-6,'accent' if selected else 'muted',bold=selected)
                row += 1
                available = panel_width-6
            else:
                c.text(row,x+3,label,13,'accent' if selected else 'muted',bold=selected)
                available = panel_width-20
            value_x = x+3 if roomy else x+17
            if selected:
                while cells(value)+1>available and value:
                    value=value[1:]
                value += '▏'
            c.text(row,value_x,value,available,'text',fill=True,selected=selected)
        row = top+(11 if roomy else 7)
        selected = self.index==3
        c.text(row,x+3,('☑' if self.authorized else '☐')+' 容量故障自动恢复',panel_width-6,
               'success' if self.authorized else 'muted',bold=True,selected=selected)
        if roomy:
            c.text(row+1,x+5,'已授权恢复原 Goal' if self.authorized else '默认仅观察，可在此按空格授权',panel_width-8,'muted')
        path_row = top+height+1
        if path_row<h-3:
            c.text(path_row,x,'保存至 '+str(self.path),panel_width,'muted')
        if self.error:
            c.text(h-4,3,'! '+self.error,w-6,'danger')
        c.line(h-3,2,w-4)
        c.keys(h-2,3,[('Tab / ↑↓','选择'),('空格','授权开关'),('Enter','保存'),('Ctrl+U','清空')],w-6)
        c.text(h-1,3,'5 秒内再按 Ctrl+C 取消' if time.monotonic()<self.confirm_until else
               'Ctrl+C × 2 取消配置',w-6,'warning' if time.monotonic()<self.confirm_until else 'muted')
        screen.refresh()

    def run(self, screen):
        self.palette.initialize(screen)
        try:
            curses.curs_set(0)
        except curses.error:
            pass
        screen.keypad(True)
        screen.timeout(150)
        while not self.quit:
            self.draw(screen)
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
