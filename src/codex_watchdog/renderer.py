"""Responsive dashboard and grouped detail pages for the terminal UI."""
from datetime import datetime
import time

from .events import EVENT_NAMES, read_events
from .patterns import PATTERNS, CAPACITY_LABEL, pattern_mode
from .presentation import Canvas, HEALTH, RUNTIME, GOAL, phase_text, severity, readable_wrap
from .view import activity_text, cells, interval_text, log_lines, retry_text, wrap_text

PAGE_TITLES = {'main':'会话总览', 'add':'添加会话', 'detail':'会话详情', 'events':'事件时间线',
               'patterns':'监控规则与提醒设置', 'doctor':'运行诊断', 'help':'键盘帮助', 'logs':'守护日志'}
MODE_NAMES = {'recover':'自动恢复', 'observe':'仅记录', 'off':'已禁用'}


def health_role(value):
    if value in ('running','healthy'):
        return 'success'
    if value in ('error','blocked','stale'):
        return 'danger'
    if value in ('degraded','restarting'):
        return 'warning'
    return 'muted'


def age_text(seconds):
    if seconds is None:
        return '尚无记录'
    seconds = int(seconds)
    return f'{seconds} 秒前' if seconds < 60 else f'{seconds//60} 分钟前'


class Renderer:
    def __init__(self, app, screen):
        self.app = app
        self.canvas = Canvas(screen, getattr(app, 'palette', None))
        self.h, self.w = screen.getmaxyx()
        self.data = app.data or {}
        self.config = self.data.get('config', {})
        self.now = time.time()
        self.compact = self.h < 24
        self.body_top = 7 if self.compact else 9

    def draw(self):
        c, app = self.canvas, self.app
        c.screen.erase()
        c.text(0, 2, 'CODEX WATCHDOG', role='heading', bold=True)
        c.text(0, 19, '/ '+PAGE_TITLES[app.mode], self.w-31, 'muted')
        if self.h < 18 or self.w < 70:
            c.paragraph(3, 2, '请将终端调整到至少 70 列、18 行。', max(1, self.w-4))
            c.paragraph(6, 2, '5 秒内再按一次 Ctrl+C 关闭软件' if time.monotonic() < app.exit_confirmation_until
                        else '按两次 Ctrl+C 关闭软件', max(1, self.w-4), role='warning')
            c.screen.refresh()
            return
        c.text(0, self.w-10, datetime.now().strftime('%H:%M:%S'), 8, 'muted')
        self.header()
        if app.mode in ('main', 'add'):
            self.sessions()
        else:
            self.page()
        self.footer()
        self.dialog()
        c.screen.refresh()

    def header(self):
        c, d = self.canvas, self.data
        service = d.get('service_health', d.get('health', 'unknown'))
        monitor = d.get('monitor_health', 'unknown')
        rows = d.get('rows', [])
        count = sum(bool(row.get('error') or row.get('attention') or row.get('phase') in ('suspended','resume_outcome_unknown','observed_failure')) for row in rows)
        if self.compact:
            c.text(2,2,'● 服务 '+HEALTH.get(service,service),22,health_role(service),bold=True)
            c.text(2,25,'● 监控 '+HEALTH.get(monitor,monitor)+f" {d.get('successful_targets',0)}/{len(rows)}",self.w-27,health_role(monitor),bold=True)
            c.text(3,2,'最近成功 '+age_text(d.get('last_success_age')),27,'muted')
            c.text(3,31,'需关注 '+str(count)+' · 下次 '+retry_text(d.get('next_check_at'),self.now),self.w-33,'warning' if count else 'muted')
            c.text(5,2,CAPACITY_LABEL+' · '+MODE_NAMES[pattern_mode(self.config,PATTERNS[0])],42,'accent')
            c.text(5,46,'间隔 '+interval_text(d.get('check_interval_seconds',300))+' · g 规则',self.w-48,'muted')
            return
        gap = 2
        widths = [(self.w-4-2*gap)//3]*3
        widths[-1] = self.w-4-2*gap-sum(widths[:2])
        cards = [('守护服务', '● '+HEALTH.get(service, service), '最近尝试 '+age_text(d.get('age')),health_role(service)),
                 ('监控连接', HEALTH.get(monitor, monitor)+f"  {d.get('successful_targets', 0)}/{len(rows)}",
                  '最近成功 '+age_text(d.get('last_success_age')),health_role(monitor)),
                 ('需要关注', f'{count} 个会话' if count else '暂无异常',
                  '下次检查 '+retry_text(d.get('next_check_at'), self.now),'warning' if count else 'success')]
        x = 2
        for (title, value, note, role), width in zip(cards, widths):
            c.box(2, x, 5, width, title)
            c.text(3, x+2, value, width-4, role, bold=True)
            if title == '监控连接' and rows:
                c.progress(4,x+2,d.get('successful_targets',0)/len(rows),width-4,role)
            c.text(5, x+2, note, width-4, 'muted')
            x += width+gap
        mode = MODE_NAMES[pattern_mode(self.config, PATTERNS[0])]
        c.text(7, 2, CAPACITY_LABEL+' · '+mode, 42, 'success' if mode=='自动恢复' else 'muted')
        if self.w < 120:
            c.text(7,46,'间隔 '+interval_text(d.get('check_interval_seconds',300))+' · g 规则',self.w-48,'muted')
        else:
            c.text(7, 46, '间隔 '+interval_text(d.get('check_interval_seconds', 300)), 20, 'muted')
            c.text(7, 68, 'g 规则与提醒', 20, 'muted')
            theme = getattr(self.app, 'palette', None)
            c.text(7,self.w-22,'c '+{'dark':'深色','light':'浅色','mono':'单色'}.get(theme.name if theme else 'mono','单色')+'主题',20,'muted')

    def sessions(self):
        app, c = self.app, self.canvas
        app.rows = app.choose_rows()
        split = self.w >= 120 and self.h >= 24
        sidebar = min(44, self.w//3) if split else 0
        left_width = self.w-4-sidebar-(2 if split else 0)
        title = '当前服务中的主会话' if app.mode == 'add' else '受管理的 Codex 会话'
        top = self.body_top
        end = self.h-5 if split or self.compact else self.h-7
        c.box(top, 2, end-top+1, left_width, title)
        query = '/ '+app.query+('▏' if app.editing_filter else '') if app.query or app.editing_filter else '/ 搜索项目、标题或状态'
        c.text(top+1, 4, query, left_width-16, 'accent' if app.editing_filter else 'muted')
        c.text(top+1, 2+left_width-11, f'{len(app.rows)} 个会话', 8, 'muted')
        inner = left_width-5
        if inner >= 86:
            fields = [('项目 / 会话', 30), ('运行', 12), ('Goal', 12), ('守护状态', inner-64), ('重试', 10)]
        else:
            fields = [('项目 / 会话', inner-30), ('运行', 12), ('守护状态', 18)]
        if app.mode == 'add':
            fields = [('项目 / 会话', inner-29), ('运行', 12), ('最近活动', 17)]
        x = 4
        for title, width in fields:
            c.text(top+3, x, title, width, 'muted')
            x += width
        roomy = self.h >= 26
        step = 2 if roomy else 1
        available = max(1, (end-(top+4)-1)//step+1)
        start = max(0, app.index-available+1)
        for offset, row in enumerate(app.rows[start:start+available]):
            number = start+offset
            y = top+4+offset*step
            selected = number == app.index
            c.text(y, 3, '', left_width-2, fill=True, selected=selected)
            label = ('› ' if selected else '  ')+row['label']
            role = severity(row)
            if app.mode == 'add':
                values = [label, RUNTIME.get(row.get('runtime'), row.get('runtime', '-')),
                          activity_text(row.get('updated_at'))]
            elif len(fields) == 5:
                values = [label, RUNTIME.get(row.get('runtime'), row.get('runtime', '-')),
                          GOAL.get(row.get('goal_status'), row.get('goal_status', '-')), phase_text(row),
                          retry_text(row.get('next_retry_at'), self.now)]
            else:
                values = [label, RUNTIME.get(row.get('runtime'), row.get('runtime', '-')), phase_text(row)]
            x = 4
            for n, ((_, width), value) in enumerate(zip(fields, values)):
                c.text(y, x, value, width-1, role if n else 'text', bold=selected, selected=selected)
                x += width
            if not selected:
                c.text(y,4,{'success':'●','warning':'◷','danger':'!','muted':'Ⅱ','accent':'·'}[role],1,role)
            if roomy and y+1 < end:
                subtitle = row.get('title') or row.get('reason') or row['cwd']
                c.text(y+1, 6, subtitle, inner-2, 'muted')
        if not app.rows:
            text = ('正在发现会话…' if app.busy == 'discover' else
                    '没有匹配会话，调整搜索条件' if app.query else
                    '没有可添加会话，按 r 重新发现' if app.mode == 'add' else
                    '还没有受管理会话，按 a 添加')
            c.paragraph(top+4, 5, text, inner-2, min(2,end-(top+4)), 'muted')
        row = app.selected()
        if split:
            x = 4+left_width
            c.box(top, x, end-top+1, sidebar, '选中会话')
            if row:
                self.sidebar(row, x+2, top+2, sidebar-4, end-1)
            else:
                c.paragraph(top+2, x+2, '选择会话后，在这里查看状态与恢复判定。', sidebar-4, 3, 'muted')
        elif row and not self.compact:
            c.text(self.h-6, 3, row.get('title') or row['label'], self.w-6, bold=True)
            c.text(self.h-5, 3, row.get('reason') or row['cwd'], self.w-6, 'muted')

    def sidebar(self, row, x, y, width, bottom):
        c = self.canvas
        items = [(row['label'], 'heading', True), (row.get('title') or row['cwd'], 'muted', False),
                 ('', 'text', False), (phase_text(row), severity(row), True),
                 (RUNTIME.get(row.get('runtime'), row.get('runtime','-'))+' · Goal '+GOAL.get(row.get('goal_status'),row.get('goal_status','-')), 'muted', False),
                 ('重试 '+retry_text(row.get('next_retry_at'),self.now), 'accent', False),
                 ('', 'text', False), ('恢复判定', 'muted', False),
                 (row.get('reason') or '等待首次检查', 'text', False),
                 ('', 'text', False), ('最近失败', 'muted', False),
                 ((row.get('last_failure') or {}).get('message') or '暂无失败记录', 'muted', False),
                 ('', 'text', False), ('Enter 查看详情', 'accent', False)]
        for text, role, bold in items:
            lines = readable_wrap(text, width)
            for line in lines[:2]:
                if y > bottom:
                    return
                c.text(y, x, line, width, role, bold)
                y += 1

    def content(self):
        app, config = self.app, self.config
        content = []
        def section(title):
            content.extend([('', 'text'), (title, 'heading')])
        def field(label, value, role='text'):
            content.append((label+'  '+str(value), role))
        row = next((r for r in self.data.get('rows', []) if r['thread_id'] == app.detail), None)
        if app.mode == 'detail' and row:
            field(row['label'], row.get('title') or '', 'heading')
            field('守护状态', phase_text(row), severity(row))
            field('恢复判定', row.get('reason') or '等待首次检查')
            section('会话信息')
            for label, value in [('项目目录',row['cwd']), ('Thread',row['thread_id']),
                                 ('运行 / Goal', RUNTIME.get(row.get('runtime'), row.get('runtime','-'))+' / '+GOAL.get(row.get('goal_status'),row.get('goal_status','-'))),
                                 ('自动守护','启用' if row.get('enabled',True) else '暂停'),
                                 ('最近成功',activity_text(row.get('last_success_at'))),
                                 ('下次重试',retry_text(row.get('next_retry_at'),self.now))]:
                field(label,value,'muted')
            section('失败与恢复')
            failure = row.get('last_failure') or {}
            field('最近失败', failure.get('message') or '暂无失败记录', severity(row))
            field('匹配规则', CAPACITY_LABEL if failure.get('pattern') == 'capacity' else failure.get('pattern_name') or '-')
            field('错误码', failure.get('code') or '-', 'muted')
            if row.get('error'): field('读取异常', row['error'], 'danger')
            if row.get('attention'): field('提醒','恢复未确认或持续失败，请检查 Codex 状态','warning')
            section('累计统计')
            stats = row.get('lifetime_stats') or {}
            field('失败 / 恢复提交 / 恢复完成', f"{stats.get('failures',0)} / {stats.get('attempts',0)} / {stats.get('recoveries',0)}")
            field('观察停顿', interval_text(int(stats.get('downtime_seconds',0))), 'muted')
            section('最近事件 · e 查看时间线')
        if app.mode in ('events','detail'):
            events = read_events(config.get('state_dir',''), app.detail)
            for event in reversed(events):
                role = 'danger' if event.get('kind') in ('observation_failed','resume_unknown','service_blocked') else 'success' if event.get('kind') in ('recovery_completed','goal_completed') else 'accent'
                line = activity_text(event.get('at'))+'  '+EVENT_NAMES.get(event.get('kind'),event.get('kind',''))
                if app.detail is None and event.get('thread_id'):
                    label = next((r['label'] for r in self.data.get('rows',[]) if r['thread_id']==event['thread_id']),event['thread_id'])
                    line += ' / '+label
                content += [(line,role), ('    '+str(event.get('reason') or event.get('message') or ''),'muted')]
            if not events: content.append(('暂无事件，首次观察后会自动记录。','muted'))
        elif app.mode == 'logs':
            for line in reversed(log_lines(config,service=app.log_service)):
                content.append((line, 'danger' if 'ERROR' in line else 'warning' if 'WARNING' in line else 'muted'))
        elif app.mode == 'patterns':
            for index, pattern in enumerate(PATTERNS):
                content += [(('› ' if index==app.index else '  ')+pattern.name+'  '+MODE_NAMES[pattern_mode(config,pattern)],'selection' if index==app.index else 'heading'),
                            ('    '+', '.join(pattern.codes),'muted'),
                            ('    '+(pattern.messages[0] if pattern.messages else '按明确错误码识别'),'muted')]
            section('提醒设置')
            settings = config.get('notifications',{})
            field('n 通知','开启' if settings.get('enabled') else '关闭')
            field('b 终端响铃','开启' if settings.get('terminal_bell') else '关闭')
            field('t/T 停顿提醒时间',interval_text(config.get('stall_after_seconds',0)) if config.get('stall_after_seconds') else '关闭')
            field('Webhook 环境变量',settings.get('webhook_url_env','CODEX_WATCHDOG_WEBHOOK_URL'),'muted')
            content += [('仅容量故障支持自动恢复；停顿只提醒。','muted'),
                        ('选中 selected model is at capacity 后按 m：仅记录 → 自动恢复。','accent'),
                        ('首次启用需按 y 确认；已有失败会作为基线。','muted'),
                        ('全局观察模式：'+('开启' if config.get('observe_only') else '关闭'),'warning' if config.get('observe_only') else 'muted')]
        elif app.mode == 'doctor':
            for row in (app.diagnostic or {}).get('checks',[]):
                role = {'ok':'success','warning':'warning','error':'danger'}[row['level']]
                content += [(f"{ {'ok':'✓','warning':'!','error':'×'}[row['level']]}  {row['name']}",role),
                            ('   '+row['message'],'muted'), ('','text')]
            if not app.diagnostic: content.append(('正在执行只读诊断…','accent'))
        elif app.mode == 'help':
            for title, lines in [('会话管理',['↑↓ / j k 选择会话','a 添加 · d 移除 · p/P 暂停/启用','Enter 详情 · / 搜索']),
                                 ('观察与设置',['e 时间线 · l 日志（s 切换服务日志）','g/G 规则与提醒 · D 只读诊断','i 检查间隔 · r 刷新']),
                                 ('导航与退出',['↑↓ / PgUp / PgDn 滚动详情','Esc 返回或取消','5 秒内按两次 Ctrl+C 关闭整个软件']),
                                 ('终端显示',['c/C 切换深色、浅色和单色主题','规则页 t/T 设置停顿提醒时间','字体和字号由终端设置','建议等宽字体、14–16 pt，窗口至少 120 × 30','无彩色终端会使用文字、粗体和反白提示'])]:
                section(title)
                content.extend((line,'muted') for line in lines)
        return content

    def page(self):
        app, c = self.app, self.canvas
        title = '服务日志' if app.mode == 'logs' and app.log_service else PAGE_TITLES[app.mode]
        top = self.body_top
        bottom = self.h-5
        c.box(top,2,bottom-top+1,self.w-4,title)
        content = []
        for text, role in self.content():
            content.extend((line,role) for line in readable_wrap(text,self.w-10))
        available = max(1,bottom-top-2)
        if app.mode == 'patterns':
            focus = (app.index,self.h,self.w)
            if getattr(app,'pattern_render_focus',None) != focus:
                selected = next((index for index,(_,role) in enumerate(content) if role=='selection'),0)
                if selected < app.scroll or selected >= app.scroll+available:
                    app.scroll = selected
                app.pattern_render_focus = focus
        app.scroll = min(app.scroll,max(0,len(content)-available))
        for offset,(line,role) in enumerate(content[app.scroll:app.scroll+available]):
            c.text(top+2+offset,5,line,self.w-10,role,bold=role in ('heading','selection'),selected=role=='selection')
        if len(content)>available:
            c.text(top+1,self.w-23,f'{app.scroll+1}–{min(len(content),app.scroll+available)} / {len(content)}',18,'muted')

    def footer(self):
        app, c = self.app, self.canvas
        error = app.view_error or self.data.get('error')
        c.line(self.h-3,2,self.w-4)
        if app.busy:
            c.text(self.h-4,3,'◌ 正在处理 '+app.busy+'…',self.w-6,'accent')
        elif app.message_until>self.now:
            c.text(self.h-4,3,app.message,self.w-6,'accent')
        elif error:
            c.text(self.h-4,3,'! '+error,self.w-6,'danger')
        keys = ([('↑↓','选择'),('Enter','添加'),('/','搜索'),('r','发现'),('Esc','返回')] if app.mode=='add' else
                [('↑↓','选择'),('m','模式'),('n','通知'),('b','响铃'),('t','提醒'),('Esc','返回')] if app.mode=='patterns' else
                [('↑↓','滚动'),('s','服务日志'),('Esc','返回')] if app.mode=='logs' else
                [('↑↓','滚动'),('e','时间线'),('Esc','返回')] if app.mode=='detail' else
                [('↑↓','滚动'),('Esc','返回')] if app.mode in ('events','doctor','help') else
                [('a','添加'),('p','暂停'),('Enter','详情'),('e','历史'),('g','规则'),('?','帮助')])
        c.keys(self.h-2,3,keys,self.w-6)
        c.text(self.h-1,3,'Ctrl+C × 2 关闭软件',self.w-6,'muted')

    def dialog(self):
        app, c = self.app, self.canvas
        interrupt = time.monotonic()<app.exit_confirmation_until
        if not (app.interval_input is not None or app.confirm or app.recovery_confirmation or interrupt):
            return
        width = min(68,self.w-8)
        height = 8
        x,y = (self.w-width)//2,max(1,(self.h-height)//2)
        for row in range(y,y+height): c.text(row,x,'',width,role='dialog',fill=True)
        if interrupt:
            title, message = '关闭软件', '5 秒内再按一次 Ctrl+C，关闭界面和守护服务'
            value, helptext, role = '', '继续使用时，退出提示会自动取消。', 'warning'
        elif app.recovery_confirmation:
            title, message = '启用自动恢复', CAPACITY_LABEL
            value = '将关闭全局观察模式，仅恢复新出现的容量故障。'
            helptext, role = 'y 确认启用    n / Esc 取消；保留 Goal、模型和预算。', 'warning'
        elif app.confirm:
            title, message = '移除守护', app.confirm['label']
            value,helptext,role = app.confirm['cwd'],'y 确认移除    n / Esc 取消','danger'
        else:
            stall = app.interval_kind=='stall'
            title,message = ('停顿提醒' if stall else '检查间隔'),'输入分钟数'
            value = app.interval_input+'▏'
            helptext = app.interval_error or ('0 关闭提醒 · 只提醒，不恢复' if stall else '例如 5 或 0.5    Enter 保存 · Esc 取消')
            role = 'danger' if app.interval_error else 'accent'
        c.box(y,x,height,width,title,role)
        c.text(y+2,x+3,message,width-6,role,bold=True)
        c.text(y+3,x+3,value,width-6,bold=True)
        c.paragraph(y+5,x+3,helptext,width-6,2,'muted')
