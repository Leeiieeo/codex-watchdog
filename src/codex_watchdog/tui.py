"""Keyboard manager: browse status and explicitly edit enrollment or interval."""
import curses
import queue
import threading
import time

from .registry import (add_session, discover_sessions, remove_session,
                       set_check_interval, minutes_to_seconds, set_session_enabled,
                       set_pattern_mode, set_options)
from .view import dashboard, filter_rows, interval_text
from .patterns import PATTERNS, pattern_mode, safe_text
from .events import read_events



class App:
    def __init__(self, config_path, service=None):
        self.config_path = config_path
        self.service = service
        self.exit_confirmation_until = 0
        self.mode = 'main'
        self.query = ''
        self.editing_filter = False
        self.index = 0
        self.focus = None
        self.candidates = []
        self.discovery_done = False
        self.confirm = None
        self.interval_input = None
        self.interval_error = None
        self.interval_kind = 'interval'
        self.detail = None
        self.busy = None
        self.results = queue.Queue()
        self.message = ''
        self.message_until = 0
        self.data = None
        self.rows = []
        self.last_refresh = 0
        self.view_error = None
        self.quit = False
        self.scroll = 0
        self.log_service = False
        self.diagnostic = None
        self.last_notification_id = None

    def task(self, kind, function, *args):
        if self.busy:
            self.toast('正在处理，请稍候')
            return
        self.busy = kind
        def run():
            try: self.results.put((kind, function(*args), None))
            except Exception as exc: self.results.put((kind, None, safe_text(f'{type(exc).__name__}: {exc}')))
        threading.Thread(target=run, daemon=True).start()

    def toast(self, text):
        self.message, self.message_until = text, time.time()+8

    def refresh(self):
        try:
            self.data = dashboard(self.config_path)
            self.view_error = None
            notifications = [e for e in read_events(self.data['config']['state_dir'], limit=20)
                             if e.get('kind') == 'notification']
            if notifications:
                event = notifications[-1]
                if self.last_notification_id is not None and event['id'] != self.last_notification_id:
                    self.toast(event.get('message', '有新的通知'))
                    if event.get('terminal_bell'):
                        try:
                            curses.beep()
                        except curses.error:
                            pass
                self.last_notification_id = event['id']
            elif self.last_notification_id is None:
                self.last_notification_id = ''
        except Exception as exc:
            self.view_error = str(exc)
        self.last_refresh = time.monotonic()

    def poll(self):
        try: kind, value, error = self.results.get_nowait()
        except queue.Empty: return
        self.busy = None
        if error:
            self.toast(error)
        elif kind == 'discover':
            self.candidates = value['sessions']
            self.discovery_done = True
            self.index = 0
            self.focus = None
            if value['warnings']:
                self.toast(f"发现 {len(self.candidates)} 个主会话；{len(value['warnings'])} 个会话读取失败，按 r 重试")
        elif kind == 'add':
            self.mode, self.query, self.focus = 'main', '', value['thread_id']
            self.toast('已加入守护；历史错误作为基线，正常工作继续')
        elif kind == 'remove':
            self.toast('已移除守护，下一轮检查生效')
            self.focus = None
        elif kind == 'interval':
            self.toast('检查间隔已保存为 '+interval_text(value)+'，无需重启')
        elif kind == 'toggle':
            self.toast('已启用守护；暂停期间失败作为基线' if value.get('enabled', True) else '已暂停守护；继续读取状态')
        elif kind == 'pattern':
            self.toast('规则已更新；切换后已有失败作为基线')
        elif kind == 'options':
            self.toast('设置已保存')
        elif kind == 'doctor':
            self.diagnostic = value
            self.mode = 'doctor'
            self.scroll = 0
        self.refresh()

    def choose_rows(self):
        if not self.data: return []
        if self.mode == 'add':
            managed = {r['thread_id'] for r in self.data['rows']}
            rows = [r for r in self.candidates if r['thread_id'] not in managed]
        else:
            rows = self.data['rows']
        rows = filter_rows(rows, self.query)
        ids = [r['thread_id'] for r in rows]
        if self.focus in ids: self.index = ids.index(self.focus)
        self.index = max(0, min(self.index, len(rows)-1))
        if rows: self.focus = rows[self.index]['thread_id']
        return rows

    def selected(self):
        return self.rows[self.index] if self.rows else None

    def interrupt(self):
        now = time.monotonic()
        if 0 < self.exit_confirmation_until and now < self.exit_confirmation_until:
            self.quit = True
        else:
            self.exit_confirmation_until = now+5

    def key(self, key):
        if key == '\x03':
            self.interrupt()
            return
        if key == curses.KEY_RESIZE: return
        if self.interval_input is not None:
            if key == '\x1b':
                self.interval_input = None
            elif key in ('\n','\r',curses.KEY_ENTER):
                if not self.interval_input:
                    self.interval_input = None
                    return
                try:
                    seconds = (0 if self.interval_kind == 'stall' and self.interval_input == '0'
                               else minutes_to_seconds(self.interval_input))
                except ValueError as exc:
                    self.interval_error = str(exc)
                    return
                self.interval_input = None
                if self.interval_kind == 'stall':
                    self.task('options', lambda: set_options(self.config_path, stall_after_seconds=seconds))
                else:
                    self.task('interval',set_check_interval,self.config_path,seconds)
            elif key in (curses.KEY_BACKSPACE,'\x7f','\b'):
                self.interval_input = self.interval_input[:-1]
                self.interval_error = None
            elif key == '\x15':
                self.interval_input = ''
            elif isinstance(key,str) and key in '0123456789.' and len(self.interval_input)<20:
                self.interval_input += key
                self.interval_error = None
            return
        if self.confirm:
            if key in ('y','Y'):
                row, self.confirm = self.confirm, None
                self.task('remove', remove_session, self.config_path, row['thread_id'], row.get('registration_id'))
            elif key in ('n','N','\x1b','\n','\r'): self.confirm = None
            return
        if self.editing_filter:
            if key in ('\n','\r',curses.KEY_ENTER): self.editing_filter = False
            elif key == '\x1b': self.editing_filter, self.query = False, ''
            elif key in (curses.KEY_BACKSPACE,'\x7f','\b'): self.query = self.query[:-1]
            elif isinstance(key,str) and key.isprintable() and len(self.query) < 100: self.query += key
            self.index, self.focus = 0, None
            return
        if self.mode == 'patterns':
            if key in (curses.KEY_NPAGE, curses.KEY_PPAGE):
                self.scroll = max(0, self.scroll+(8 if key == curses.KEY_NPAGE else -8))
                return
            if key in ('m', 'n', 'b', 't') and not self.busy:
                config = self.data['config']
                if key == 'm':
                    pattern = PATTERNS[self.index]
                    if config.get('observe_only'):
                        modes = ['observe', 'off']
                    else:
                        modes = ['recover', 'observe', 'off'] if pattern.recoverable else ['observe', 'off']
                    mode = pattern_mode(config, pattern)
                    self.task('pattern', set_pattern_mode, self.config_path,
                              pattern.id, modes[(modes.index(mode)+1)%len(modes)])
                elif key == 't':
                    self.interval_input, self.interval_error, self.interval_kind = '', None, 'stall'
                else:
                    setting = 'enabled' if key == 'n' else 'terminal_bell'
                    value = not config.get('notifications', {}).get(setting, False)
                    self.task('options', lambda: set_options(self.config_path,
                        **{'notifications' if key == 'n' else 'terminal_bell': value}))
                return
            if key in (curses.KEY_DOWN, 'j', curses.KEY_UP, 'k'):
                self.index = max(0, min(len(PATTERNS)-1, self.index+(1 if key in (curses.KEY_DOWN, 'j') else -1)))
                self.scroll = self.index*3
                return
        elif self.mode in ('detail', 'logs', 'events', 'doctor', 'help'):
            if key in (curses.KEY_DOWN, 'j', curses.KEY_UP, 'k', curses.KEY_NPAGE, curses.KEY_PPAGE):
                step = 1 if key in (curses.KEY_DOWN,'j') else -1 if key in (curses.KEY_UP,'k') else 8 if key == curses.KEY_NPAGE else -8
                self.scroll = max(0, self.scroll+step)
                return
            if key == 's' and self.mode == 'logs':
                self.log_service = not self.log_service
                self.scroll = 0
                return
            if key == 'e' and self.mode == 'detail':
                self.mode, self.scroll = 'events', 0
                return
        if key == '\x1b':
            if self.mode != 'main':
                self.mode, self.query, self.detail = 'main', '', None
                self.scroll, self.index, self.focus = 0, 0, None
        elif key in (curses.KEY_DOWN,'j',curses.KEY_UP,'k',curses.KEY_NPAGE,curses.KEY_PPAGE):
            step = (1 if key in (curses.KEY_DOWN,'j') else -1 if key in (curses.KEY_UP,'k')
                    else 8 if key == curses.KEY_NPAGE else -8)
            self.index = max(0,min(len(self.rows)-1,self.index+step))
            self.focus = self.rows[self.index]['thread_id'] if self.rows else None
        elif key == '/': self.editing_filter = self.mode in ('main','add')
        elif key == 'a' and self.mode == 'main':
            self.mode, self.query, self.index, self.focus = 'add','',0,None
            self.candidates, self.discovery_done = [], False
            self.task('discover', discover_sessions, self.config_path)
        elif key == 'r':
            self.refresh()
            if self.mode == 'add': self.task('discover', discover_sessions, self.config_path)
        elif key == 'd' and self.mode == 'main' and self.selected() and not self.busy:
            self.confirm = dict(self.selected())
        elif key == 'i' and self.mode == 'main' and not self.busy:
            self.interval_input, self.interval_error, self.interval_kind = '',None,'interval'
        elif key == 'p' and self.mode == 'main' and self.selected() and not self.busy:
            row = self.selected()
            self.task('toggle', set_session_enabled, self.config_path, row['thread_id'],
                      not row.get('enabled', True), row.get('registration_id'))
        elif key in ('\n','\r',curses.KEY_ENTER) and self.mode in ('main','add'):
            row = self.selected()
            if row and self.mode == 'add':
                self.task('add', add_session, self.config_path, row['thread_id'])
            elif row and self.mode == 'main':
                self.detail, self.mode, self.scroll = row['thread_id'], 'detail', 0
        elif key in ('l', 'e', 'P', '?') and self.mode == 'main':
            self.mode = {'l':'logs','e':'events','P':'patterns','?':'help'}[key]
            self.detail, self.scroll, self.index = None, 0, 0
        elif key == 'D' and not self.busy:
            from .diagnostics import doctor
            self.mode, self.diagnostic, self.scroll = 'doctor', None, 0
            self.task('doctor', doctor, self.config_path)

    def draw(self, screen):
        from .renderer import Renderer
        Renderer(self, screen).draw()

    def run(self, screen):
        try: curses.curs_set(0)
        except curses.error: pass
        if curses.has_colors():
            curses.start_color()
            background = 0
            try:
                curses.use_default_colors()
                background = -1
            except curses.error: pass
            for n,color in ((1,curses.COLOR_CYAN),(2,curses.COLOR_YELLOW),(3,curses.COLOR_RED)):
                try: curses.init_pair(n,color,background)
                except curses.error: pass
        screen.keypad(True)
        screen.timeout(150)
        self.refresh()
        while not self.quit:
            if self.service:
                if self.service.stop_requested:
                    self.quit = True
                    break
                message = self.service.poll()
                if message:
                    self.toast(message)
            self.poll()
            if time.monotonic()-self.last_refresh >= 1: self.refresh()
            self.draw(screen)
            try: self.key(screen.get_wch())
            except curses.error: pass


def main():
    from .control import main as control_main
    control_main()


if __name__ == '__main__': main()
