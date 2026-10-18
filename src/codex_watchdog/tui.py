"""Keyboard manager: browse status and explicitly edit enrollment or interval."""
import curses
import queue
import threading
import time

from .registry import (add_session, discover_sessions, remove_session,
                       set_check_interval, minutes_to_seconds, set_session_enabled,
                       set_pattern_mode, set_options)
from .view import activity_text, dashboard, filter_rows, fit, interval_text, log_lines, retry_text, wrap_text
from .patterns import CAPACITY_ERROR_CODE, CAPACITY_MESSAGE, PATTERNS, pattern_mode, safe_text
from .events import read_events, EVENT_NAMES

PHASE = {'baseline':'建立基线', 'running':'正常工作', 'healthy':'正常', 'watching':'监测中',
         'paused':'已暂停', 'backoff':'等待重试', 'goal_stopped':'目标已停止',
         'unavailable':'暂不可恢复', 'suspended':'读取异常', 'resume_accepted':'已请求恢复',
         'resume_outcome_unknown':'恢复结果待确认', 'awaiting_new_turn':'等待新轮次',
         'cancelled':'恢复已取消', 'pending_observation':'等待首次检查',
         'guard_paused':'守护已暂停', 'observed_failure':'失败已记录',
         'cancelled_before_resume':'复核后取消'}


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
            self.text(screen,3,1,'请将终端调整到至少 70 列、18 行；按两次 Ctrl+C 关闭软件')
            if time.monotonic() < self.exit_confirmation_until:
                self.text(screen,5,1,'5 秒内再按一次 Ctrl+C，关闭界面和守护服务',curses.A_BOLD)
            screen.refresh()
            return
        health = self.data.get('service_health', self.data['health']) if self.data else 'unknown'
        age = self.data['age'] if self.data else None
        self.text(screen,1,1,f"服务: {health}    最近尝试: {int(age) if age is not None else '-'} 秒前    管理 {len(self.data['rows']) if self.data else 0} 个会话")
        if self.data:
            success_age = self.data.get('last_success_age')
            self.text(screen,2,1,f"监控: {self.data.get('monitor_health','unknown')}    成功 {self.data.get('successful_targets',0)}/{len(self.data['rows'])}"
                      +f"    最近成功: {int(success_age) if success_age is not None else '-'} 秒前")
        error = self.view_error or (self.data or {}).get('error')
        if error: self.text(screen,3,1,'状态异常: '+error,curses.color_pair(3))
        elif self.data:
            self.text(screen,3,1,'检查间隔: '+interval_text(self.data['check_interval_seconds'])
                      +'    下次检查: '+retry_text(self.data['next_check_at'],time.time())+'    i 修改间隔')
        mode = pattern_mode(self.data['config'] if self.data else {}, PATTERNS[0])
        mode_name = {'recover':'自动恢复','observe':'仅记录','off':'已禁用'}[mode]
        self.text(screen,4,1,'失败 pattern: 模型容量不足 ['+mode_name+'] / '+CAPACITY_ERROR_CODE+'    P 查看规则',curses.A_BOLD)
        self.text(screen,5,1,'  '+CAPACITY_MESSAGE)
        if self.mode in ('detail','logs','events','patterns','doctor','help'):
            self.draw_details(screen)
        else:
            self.rows = self.choose_rows()
            title = '添加守护 / 当前服务中的主会话' if self.mode == 'add' else '受管理的 Codex 会话'
            self.text(screen,6,1,title,curses.A_BOLD)
            self.text(screen,7,1,'筛选: '+self.query+('▏' if self.editing_filter else '')+'    (/ 搜索)')
            wide = w >= 105
            header = (fit('项目 / 会话',24)+fit('运行',13)+fit('Goal',12)+fit('守护状态',22)+fit('重试',7)+'下次重试'
                      if wide else fit('项目 / 会话',23)+fit('运行',13)+fit('Goal',11)+'守护状态')
            if self.mode == 'add': header = fit('项目',24)+fit('运行',13)+fit('最近活动',14)+'会话标题'
            self.text(screen,8,1,header,curses.A_BOLD)
            count = max(1,h-14)
            start = max(0,self.index-count+1)
            for n,row in enumerate(self.rows[start:start+count],start):
                if self.mode == 'add':
                    line = (fit(row['label'],24)+fit(row.get('runtime','-'),13)
                            +fit(activity_text(row.get('updated_at')),14)+row.get('title',''))
                else:
                    line = fit(row['label'],24 if wide else 23)+fit(row.get('runtime','-'),13)+fit(row.get('goal_status','-'),12 if wide else 11)
                    phase = PHASE.get(row.get('phase'),row.get('phase','-'))
                    if not row.get('enabled', True): phase = '守护已暂停'
                    elif row.get('attention'): phase += ' / 需关注'
                    line += fit(phase,22)+fit(row.get('attempts',0),7)+retry_text(row.get('next_retry_at'),time.time()) if wide else phase
                self.text(screen,9+n-start,1,line,curses.A_REVERSE if n==self.index else 0)
            row = self.selected()
            if row:
                self.text(screen,h-5,1,row.get('title') or row['cwd'])
                self.text(screen,h-4,1,row['cwd'])
            if not self.rows:
                empty = ('正在发现会话…' if self.busy=='discover' else
                         '没有可添加的主会话；会话可能已全部受管理。按 r 刷新' if self.mode=='add' and self.discovery_done else
                         '未找到会话。按 r 重试' if self.mode=='add' else '暂无匹配会话；按 a 自动发现并添加')
                self.text(screen,10,1,empty)
        if self.interval_input is not None:
            stall = self.interval_kind == 'stall'
            current = interval_text(self.data['config'].get('stall_after_seconds',0) if stall else self.data['check_interval_seconds']) if self.data else '-'
            self.text(screen,h-4,1,('设置停顿提醒' if stall else '设置检查间隔')+'（分钟），当前 '+current+': '+self.interval_input+'▏',curses.A_REVERSE)
            self.text(screen,h-3,1,self.interval_error or ('0 关闭提醒；只提醒，不恢复正在运行的轮次' if stall else '例如 5 = 5分钟，0.5 = 30秒；范围 1秒～24小时'))
        elif self.confirm:
            self.text(screen,h-4,1,'移除守护: '+self.confirm['label']+'  '+self.confirm['cwd'],curses.A_REVERSE)
            self.text(screen,h-3,1,'y 确认移除 / n 取消；移除只修改守护列表',curses.A_BOLD)
        elif self.busy:
            self.text(screen,h-3,1,'正在处理 '+self.busy+' …（界面继续响应）',curses.color_pair(2))
        elif self.message_until > time.time(): self.text(screen,h-3,1,self.message,curses.color_pair(2))
        helptext = ('Enter 保存  Esc 取消  留空回车保留当前设置' if self.interval_input is not None else
                    '↑↓/jk 选择  Enter 添加  / 搜索  r 重新发现  Esc 返回' if self.mode=='add' else
                    '↑↓ 选择  PgDn 滚动  m 模式  n 通知  b 响铃  t 提醒  Esc 返回' if self.mode == 'patterns' else
                    '↑↓/PgUp/PgDn 滚动  s 切换服务日志  Esc 返回' if self.mode == 'logs' else
                    '↑↓/PgUp/PgDn 滚动  e 历史  Esc 返回' if self.mode == 'detail' else
                    '↑↓/PgUp/PgDn 滚动  Esc 返回' if self.mode in ('events','doctor','help') else
                    'a 添加  d 移除  p 暂停  i 间隔  Enter 详情  P 规则  ? 帮助')
        self.text(screen,h-2,1,helptext,curses.A_BOLD)
        self.text(screen,h-1,1,
                  '5 秒内再按一次 Ctrl+C，关闭界面和守护服务' if time.monotonic() < self.exit_confirmation_until
                  else '按两次 Ctrl+C 关闭软件，同时停止界面和守护服务。')
        screen.refresh()

    def draw_details(self, screen):
        h, w = screen.getmaxyx()
        config = self.data['config'] if self.data else {}
        row = next((r for r in (self.data or {}).get('rows', [])
                    if r['thread_id'] == self.detail), None)
        content = []
        title = {'detail': '会话详情', 'events': '事件时间线', 'logs': '服务日志' if self.log_service else '守护日志',
                 'patterns': '监控规则与提醒设置', 'doctor': '运行诊断', 'help': '键盘帮助'}[self.mode]
        if self.mode == 'detail' and row:
            failure, stats = row.get('last_failure') or {}, row.get('lifetime_stats') or {}
            fields = [('会话', row['label']), ('标题', row.get('title', '-')), ('目录', row['cwd']),
                      ('Thread', row['thread_id']), ('运行 / Goal', f"{row.get('runtime')} / {row.get('goal_status')}"),
                      ('自动守护', '启用' if row.get('enabled', True) else '暂停'),
                      ('阶段', PHASE.get(row.get('phase'), row.get('phase', '-'))),
                      ('恢复判定', row.get('reason', '等待首次检查')),
                      ('最近成功检查', activity_text(row.get('last_success_at'))),
                      ('当前退避重试', f"{row.get('attempts', 0)} 次；下次 {retry_text(row.get('next_retry_at'), time.time())}"),
                      ('最近失败', failure.get('message') or '-'),
                      ('匹配规则', failure.get('pattern_name') or '-'),
                      ('错误码', failure.get('code') or '-'),
                      ('累计统计', f"失败 {stats.get('failures', 0)} / 提交恢复 {stats.get('attempts', 0)} / 恢复完成 {stats.get('recoveries', 0)}"),
                      ('累计观察停顿', interval_text(int(stats.get('downtime_seconds', 0)))),
                      ('提醒', '恢复后未观察到新轮次' if row.get('recovery_unconfirmed') else
                       '长时间无可观测的新活动' if row.get('stalled') else '持续失败需关注' if row.get('attention') else '-'),
                      ('读取异常', row.get('error') or '-')]
            content.extend(label+': '+str(value) for label, value in fields)
            content += ['', '最近事件（e 查看完整时间线）']
        if self.mode in ('events', 'detail'):
            events = read_events(config.get('state_dir', ''), self.detail)
            for event in reversed(events):
                line = activity_text(event.get('at'))+'  '+EVENT_NAMES.get(event.get('kind'), event.get('kind', ''))
                if self.detail is None and event.get('thread_id'):
                    label = next((r['label'] for r in self.data['rows'] if r['thread_id'] == event['thread_id']), event['thread_id'])
                    line += ' / '+label
                line += '  '+str(event.get('reason') or event.get('message') or '')
                content.append(line)
            if not events:
                content.append('暂无事件')
        elif self.mode == 'logs':
            content.extend(reversed(log_lines(config, service=self.log_service)))
        elif self.mode == 'patterns':
            for n, pattern in enumerate(PATTERNS):
                mode = pattern_mode(config, pattern)
                content += [('▶ ' if n == self.index else '  ')+pattern.name+' ['+{'recover':'自动恢复', 'observe':'仅记录', 'off':'已禁用'}[mode]+']',
                            '    错误码：'+', '.join(pattern.codes),
                            '    '+('完整提示：'+pattern.messages[0] if pattern.messages else '按明确错误码识别')]
            settings = config.get('notifications', {})
            content += ['', '通知：'+('开启' if settings.get('enabled') else '关闭')+'（n 切换）',
                        '终端响铃：'+('开启' if settings.get('terminal_bell') else '关闭')+'（b 切换）',
                        'Webhook 从环境变量读取：'+settings.get('webhook_url_env', 'CODEX_WATCHDOG_WEBHOOK_URL'),
                        '停顿提醒阈值：'+(interval_text(config.get('stall_after_seconds', 0)) if config.get('stall_after_seconds') else '关闭')+'（t 修改）',
                        '停顿只提示，不修改正在运行的 Goal。',
                        '全局观察模式：'+('开启' if config.get('observe_only') else '关闭'),
                        '切换规则模式后已有失败作为基线；仅容量规则支持自动恢复。']
        elif self.mode == 'doctor':
            if self.diagnostic:
                content.extend(f"[{r['level']}] {r['name']}: {r['message']}" for r in self.diagnostic['checks'])
            else:
                content.append('正在执行只读诊断…')
        elif self.mode == 'help':
            content += ['↑↓ / j k：选择会话或滚动页面', 'a：发现并添加会话', 'd：移除（y 确认，n 取消）',
                        'p：暂停或启用选中会话的自动守护', 'i：修改正常检查间隔',
                        'Enter：会话详情', 'e：事件时间线', 'l：日志（s 切换服务日志）',
                        'P：规则与通知设置', 'D：只读诊断', '/：筛选会话',
                        'Esc：返回或取消', 'Ctrl+C 两次：5 秒内第二次关闭整个软件',
                        '暂停守护仍读取状态；重新启用后，暂停期间失败作为基线。',
                        '只有容量故障支持自动恢复；连接错误等仅记录。']
        wrapped = [line for item in content for line in wrap_text(item, w-3)]
        available = max(1, h-11)
        self.scroll = min(self.scroll, max(0, len(wrapped)-available))
        self.text(screen, 6, 1, title+f' ({self.scroll+1}/{max(1, len(wrapped))})', curses.A_BOLD)
        for offset, line in enumerate(wrapped[self.scroll:self.scroll+available]):
            self.text(screen, 7+offset, 1, line)

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
