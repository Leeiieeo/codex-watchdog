"""Terminal presentation primitives, kept independent of watchdog behavior."""
import curses

from .view import cells, fit, wrap_text


def readable_wrap(value, width):
    """Prefer English word boundaries while allowing Chinese and long paths."""
    raw = ''.join(c if c.isprintable() else ' ' for c in str(value))
    lines = []
    while cells(raw) > max(1,width):
        prefix = wrap_text(raw,width)[0]
        end = len(prefix)
        whitespace = prefix.rfind(' ')
        if whitespace >= len(prefix)//2:
            end = whitespace
        lines.append(raw[:end].rstrip())
        raw = raw[end:].lstrip()
    return lines+[raw]

PHASE = {'baseline':'建立基线', 'running':'正常工作', 'healthy':'正常', 'watching':'监测中',
         'paused':'已暂停', 'backoff':'等待重试', 'goal_stopped':'目标已停止',
         'unavailable':'暂不可恢复', 'suspended':'读取异常', 'resume_accepted':'已请求恢复',
         'resume_outcome_unknown':'恢复结果待确认', 'awaiting_new_turn':'等待新轮次',
         'cancelled':'恢复已取消', 'pending_observation':'等待首次检查',
         'guard_paused':'守护已暂停', 'observed_failure':'失败已记录',
         'cancelled_before_resume':'复核后取消'}
HEALTH = {'running':'运行中', 'healthy':'正常', 'warming':'建立连接', 'empty':'暂无会话',
          'stopped':'已停止', 'stale':'检查过期', 'error':'异常', 'degraded':'部分异常',
          'starting':'启动中', 'restarting':'重启中', 'blocked':'需处理', 'unknown':'待确认'}
RUNTIME = {'active':'运行中', 'idle':'空闲', 'systemError':'失败', 'notLoaded':'未加载'}
GOAL = {'active':'执行中', 'paused':'已暂停', 'blocked':'已阻塞', 'complete':'已完成',
        'budget_limited':'预算耗尽', 'usage_limited':'用量限制'}


def phase_text(row):
    if not row.get('enabled', True):
        return '守护已暂停'
    result = PHASE.get(row.get('phase'), row.get('phase', '等待首次检查'))
    return result+' · 需关注' if row.get('attention') else result


def severity(row):
    if row.get('error') or row.get('attention') or row.get('phase') in ('suspended', 'resume_outcome_unknown'):
        return 'danger'
    if not row.get('enabled', True) or row.get('phase') == 'guard_paused':
        return 'muted'
    if row.get('phase') in ('backoff', 'observed_failure', 'awaiting_new_turn', 'unavailable', 'paused'):
        return 'warning'
    if row.get('phase') in ('running', 'healthy'):
        return 'success'
    return 'accent'


class Canvas:
    def __init__(self, screen, palette=None):
        self.screen = screen
        self.height, self.width = screen.getmaxyx()
        self.palette = palette

    def attr(self, role='text', bold=False):
        value = self.palette.attr(role) if self.palette else (curses.A_DIM if role in ('muted', 'border') else 0)
        return value | (curses.A_BOLD if bold else 0)

    def text(self, y, x, value, width=None, role='text', bold=False, fill=False, selected=False):
        if not 0 <= y < self.height or not 0 <= x < self.width-1:
            return
        width = min(self.width-x-1, width if width is not None else self.width-x-1)
        value = str(value)
        if fill:
            value = fit(value, width)
        else:
            value = fit(value, min(width, cells(value))).rstrip()
        attr = self.attr('selection' if selected else role, bold)
        if selected and self.palette is None:
            attr |= curses.A_REVERSE
        try:
            self.screen.addstr(y, x, value, attr)
        except curses.error:
            pass

    def line(self, y, x, width, role='border'):
        self.text(y, x, '─'*max(0, width), width, role)

    def box(self, y, x, height, width, title=None, role='border'):
        if height < 3 or width < 4:
            return
        self.text(y, x, '╭'+'─'*(width-2)+'╮', width, role)
        self.text(y+height-1, x, '╰'+'─'*(width-2)+'╯', width, role)
        for row in range(y+1, y+height-1):
            self.text(row, x, '│', 1, role)
            self.text(row, x+width-1, '│', 1, role)
        if title:
            self.text(y, x+2, ' '+title+' ', width-5, 'heading', bold=True)

    def progress(self, y, x, ratio, width, role='accent'):
        if width < 3:
            return
        filled = max(0,min(width,int(width*ratio)))
        self.text(y,x,'━'*filled,filled,role)
        self.text(y,x+filled,'─'*(width-filled),width-filled,'border')

    def paragraph(self, y, x, value, width, max_lines=2, role='text'):
        lines = readable_wrap(value, width)
        for n, line in enumerate(lines[:max_lines]):
            self.text(y+n, x, line, width, role)
        return min(len(lines), max_lines)

    def keys(self, y, x, items, width):
        for key, description in items:
            needed = cells(key)+cells(description)+4
            if needed > width:
                break
            self.text(y, x, key, cells(key), 'accent', bold=True)
            x += cells(key)+1
            self.text(y, x, description, cells(description), 'muted')
            x += cells(description)+3
            width -= needed
