"""Keyboard-driven manager. Reads status; only explicit add/remove edits config."""
import argparse
import curses
import locale
import os
from pathlib import Path
import queue
import threading
import time

from .registry import add_session, discover_sessions, remove_session
from .view import activity_text, dashboard, filter_rows, fit, log_lines, retry_text

PHASE = {'baseline':'建立基线', 'running':'正常工作', 'healthy':'正常', 'watching':'监测中',
         'paused':'已暂停', 'backoff':'等待重试', 'goal_stopped':'目标已停止',
         'unavailable':'暂不可恢复', 'suspended':'读取异常', 'resume_accepted':'已请求恢复',
         'resume_outcome_unknown':'恢复结果待确认', 'awaiting_new_turn':'等待新轮次',
         'cancelled':'恢复已取消', 'pending_observation':'等待首次检查',
         'cancelled_before_resume':'复核后取消'}


class App:
    def __init__(self, config_path):
        self.config_path = config_path
        self.mode = 'main'
        self.query = ''
        self.editing_filter = False
        self.index = 0
        self.focus = None
        self.candidates = []
        self.discovery_done = False
        self.confirm = None
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

    def task(self, kind, function, *args):
        if self.busy:
            self.toast('正在处理，请稍候')
            return
        self.busy = kind
        def run():
            try: self.results.put((kind, function(*args), None))
            except Exception as exc: self.results.put((kind, None, f'{type(exc).__name__}: {exc}'))
        threading.Thread(target=run, daemon=True).start()

    def toast(self, text):
        self.message, self.message_until = text, time.time()+8

    def refresh(self):
        try:
            self.data = dashboard(self.config_path)
            self.view_error = None
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

    def key(self, key):
        if key == curses.KEY_RESIZE: return
        if self.confirm:
            if key in ('y','Y'):
                row, self.confirm = self.confirm, None
                self.task('remove', remove_session, self.config_path, row['thread_id'], row.get('registration_id'))
            elif key in ('n','N','q','\x1b','\n','\r'): self.confirm = None
            return
        if self.editing_filter:
            if key in ('\n','\r',curses.KEY_ENTER): self.editing_filter = False
            elif key == '\x1b': self.editing_filter, self.query = False, ''
            elif key in (curses.KEY_BACKSPACE,'\x7f','\b'): self.query = self.query[:-1]
            elif isinstance(key,str) and key.isprintable() and len(self.query) < 100: self.query += key
            self.index, self.focus = 0, None
            return
        if key in ('q','\x1b'):
            if self.mode != 'main':
                self.mode, self.query, self.detail = 'main', '', None
            else: self.quit = True
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
        elif key in ('\n','\r',curses.KEY_ENTER):
            row = self.selected()
            if row and self.mode == 'add':
                self.task('add', add_session, self.config_path, row['thread_id'])
            elif row and self.mode == 'main':
                self.detail, self.mode = row['thread_id'], 'detail'
        elif key == 'l' and self.mode == 'main': self.mode = 'logs'

    def text(self, screen, y, x, text, attr=0, width=None):
        height, columns = screen.getmaxyx()
        if 0 <= y < height and 0 <= x < columns:
            try: screen.addstr(y,x,fit(text,min(width or columns-x-1,columns-x-1)),attr)
            except curses.error: pass

    def draw(self, screen):
        screen.erase()
        h,w = screen.getmaxyx()
        self.text(screen,0,1,'CODEX WATCHDOG',curses.A_BOLD | curses.color_pair(1))
        if h < 18 or w < 70:
            self.text(screen,3,1,'请将终端调整到至少 70 列、18 行；q 退出界面')
            screen.refresh()
            return
        health = self.data['health'] if self.data else 'unknown'
        age = self.data['age'] if self.data else None
        self.text(screen,1,1,f"守护: {health}    最近检查: {int(age) if age is not None else '-'} 秒前    管理 {len(self.data['rows']) if self.data else 0} 个会话")
        error = self.view_error or (self.data or {}).get('error')
        if error: self.text(screen,2,1,'状态异常: '+error,curses.color_pair(3))
        if self.mode in ('detail','logs'):
            self.draw_details(screen)
        else:
            self.rows = self.choose_rows()
            title = '添加守护 / 当前服务中的主会话' if self.mode == 'add' else '受管理的 Codex 会话'
            self.text(screen,3,1,title,curses.A_BOLD)
            self.text(screen,4,1,'筛选: '+self.query+('▏' if self.editing_filter else '')+'    (/ 搜索)')
            wide = w >= 105
            header = (fit('项目 / 会话',24)+fit('运行',13)+fit('Goal',12)+fit('守护状态',22)+fit('重试',7)+'下次重试'
                      if wide else fit('项目 / 会话',23)+fit('运行',13)+fit('Goal',11)+'守护状态')
            if self.mode == 'add': header = fit('项目',24)+fit('运行',13)+fit('最近活动',14)+'会话标题'
            self.text(screen,6,1,header,curses.A_BOLD)
            count = max(1,h-12)
            start = max(0,self.index-count+1)
            for n,row in enumerate(self.rows[start:start+count],start):
                if self.mode == 'add':
                    line = (fit(row['label'],24)+fit(row.get('runtime','-'),13)
                            +fit(activity_text(row.get('updated_at')),14)+row.get('title',''))
                else:
                    line = fit(row['label'],24 if wide else 23)+fit(row.get('runtime','-'),13)+fit(row.get('goal_status','-'),12 if wide else 11)
                    phase = PHASE.get(row.get('phase'),row.get('phase','-'))
                    line += fit(phase,22)+fit(row.get('attempts',0),7)+retry_text(row.get('next_retry_at'),time.time()) if wide else phase
                self.text(screen,7+n-start,1,line,curses.A_REVERSE if n==self.index else 0)
            row = self.selected()
            if row:
                self.text(screen,h-5,1,row.get('title') or row['cwd'])
                self.text(screen,h-4,1,row['cwd'])
            if not self.rows:
                empty = ('正在发现会话…' if self.busy=='discover' else
                         '没有可添加的主会话；会话可能已全部受管理。按 r 刷新' if self.mode=='add' and self.discovery_done else
                         '未找到会话。按 r 重试' if self.mode=='add' else '暂无匹配会话；按 a 自动发现并添加')
                self.text(screen,8,1,empty)
        if self.confirm:
            self.text(screen,h-4,1,'移除守护: '+self.confirm['label']+'  '+self.confirm['cwd'],curses.A_REVERSE)
            self.text(screen,h-3,1,'y 确认移除 / n 取消；移除只修改守护列表',curses.A_BOLD)
        elif self.busy:
            self.text(screen,h-3,1,'正在处理 '+self.busy+' …（界面继续响应）',curses.color_pair(2))
        elif self.message_until > time.time(): self.text(screen,h-3,1,self.message,curses.color_pair(2))
        helptext = ('↑↓/jk 选择  Enter 添加  / 搜索  r 重新发现  Esc 返回' if self.mode=='add' else
                    'Esc 返回  q 返回' if self.mode in ('detail','logs') else
                    '↑↓/jk 选择  a 添加  d 移除  Enter 详情  l 日志  / 搜索  r 刷新  q 退出')
        self.text(screen,h-2,1,helptext,curses.A_BOLD)
        self.text(screen,h-1,1,'界面退出后，守护进程继续运行。')
        screen.refresh()

    def draw_details(self, screen):
        h,w = screen.getmaxyx()
        row = next((r for r in (self.data or {}).get('rows',[]) if r['thread_id']==self.detail),None)
        y = 3
        if self.mode == 'detail' and row:
            fields = [('会话',row['label']),('标题',row.get('title','-')),('目录',row['cwd']),
                      ('Thread',row['thread_id']),('运行 / Goal',f"{row.get('runtime')} / {row.get('goal_status')}"),
                      ('守护',PHASE.get(row.get('phase'),row.get('phase','-'))),
                      ('重试',f"{row.get('attempts',0)} 次；下次 {retry_text(row.get('next_retry_at'),time.time())}"),
                      ('异常',row.get('error') or ('模型容量不足' if row.get('capacity_error') else '-'))]
            for label,value in fields:
                self.text(screen,y,1,label+': '+str(value))
                y += 1
        self.text(screen,y+1,1,'最近守护日志',curses.A_BOLD)
        lines = log_lines(self.data['config'],self.detail if self.mode=='detail' else None) if self.data else []
        available = max(0,h-y-7)
        for n,line in enumerate(lines[-available:] if available else []):
            self.text(screen,y+2+n,1,line)

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
            self.poll()
            if time.monotonic()-self.last_refresh >= 1: self.refresh()
            self.draw(screen)
            try: self.key(screen.get_wch())
            except curses.error: pass


def default_config():
    return os.environ.get('CODEX_WATCHDOG_CONFIG', str(Path.cwd()/'config.json'))


def main():
    parser = argparse.ArgumentParser(description='Codex Watchdog terminal manager')
    parser.add_argument('--config',default=default_config())
    args = parser.parse_args()
    locale.setlocale(locale.LC_ALL,'')
    try: curses.wrapper(App(str(Path(args.config).expanduser().resolve())).run)
    except KeyboardInterrupt: pass


if __name__ == '__main__': main()
