import curses
import tempfile
import unittest
import os
from unittest.mock import Mock, patch

from codex_watchdog.renderer import Renderer
from codex_watchdog.tui import App
from codex_watchdog.view import cells
from codex_watchdog.theme import Palette, resolve_theme
from codex_watchdog.setup import Setup
from codex_watchdog.presentation import readable_wrap


class Buffer:
    """A cell-aware terminal buffer: catch boundaries and inspect real output."""
    def __init__(self, height, width):
        self.height, self.width = height, width
        self.erase()

    def getmaxyx(self): return self.height, self.width
    def refresh(self): pass
    def erase(self):
        self.grid = [[' ']*self.width for _ in range(self.height)]
        self.attributes = [[0]*self.width for _ in range(self.height)]

    def addstr(self, y, x, text, attr=0):
        if not 0 <= y < self.height or x < 0 or x+cells(text) >= self.width:
            raise AssertionError(f'out of bounds at {y},{x}: {text!r}')
        for char in text:
            size = cells(char)
            if size == 0:
                continue
            self.grid[y][x] = char
            self.attributes[y][x] = attr
            if size == 2:
                self.grid[y][x+1] = ''
                self.attributes[y][x+1] = attr
            x += size

    @property
    def text(self):
        return '\n'.join(''.join(row) for row in self.grid)


class PresentationTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.app = App('unused')
        self.rows = [
            {'thread_id':'one','label':'研究项目','title':'第一视角视频理解',
             'cwd':'/projects/research','runtime':'active','goal_status':'active',
             'phase':'running','reason':'Codex 正在执行，无需恢复'},
            {'thread_id':'two','label':'open-vtuber','title':'Framework implementation',
             'cwd':'/projects/open-vtuber','runtime':'systemError','goal_status':'blocked',
             'phase':'backoff','reason':'已匹配容量故障，等待退避到期后复核'},
            {'thread_id':'three','label':'论文管理','title':'同步论文','cwd':'/projects/papers',
             'runtime':'idle','goal_status':'paused','phase':'guard_paused','enabled':False}]
        self.app.data = {'health':'running','service_health':'running','monitor_health':'healthy',
                         'successful_targets':3,'rows':self.rows,'age':10,'last_success_age':10,
                         'next_check_at':None,'check_interval_seconds':300,
                         'config':{'state_dir':self.tmp.name}}

    def render(self, height=30, width=130):
        screen = Buffer(height, width)
        Renderer(self.app, screen).draw()
        return screen

    def test_wide_layout_has_overview_and_selected_recovery_reason(self):
        self.app.index = 1
        screen = self.render()
        for value in ('守护服务','监控连接','需要关注','选中会话',
                      '已匹配容量故障，等待退避到期后复核','Goal 已阻塞','重试'):
            self.assertIn(value,screen.text)
        self.assertEqual(self.app.selected()['thread_id'],'two')

    def test_compact_layout_keeps_selection_visible_when_scrolling(self):
        self.app.index = 2
        screen = self.render(18,70)
        self.assertIn('› 论文管理',screen.text)
        self.assertIn('守护已暂停',screen.text)
        self.assertNotIn('选中会话',screen.text)
        self.assertIn('Ctrl+C × 2',screen.text)

    def test_every_page_survives_resize_and_long_unicode_content(self):
        self.rows[0]['reason'] = '原因很长 '*100
        self.app.detail = 'one'
        self.app.diagnostic = {'checks':[{'name':'连接','level':'error','message':'长错误 '*100}]}
        for height,width in ((18,70),(24,80),(30,130),(45,180)):
            for mode in ('main','add','detail','events','logs','patterns','doctor','help'):
                with self.subTest(height=height,width=width,mode=mode):
                    self.app.mode = mode
                    self.app.index = 0
                    self.app.scroll = 0
                    screen = self.render(height,width)
                    self.assertIn('容量不足',screen.text)
                    self.app.key('j')
                    self.render(height,width)

    def test_dialog_overlays_do_not_change_selection_and_fit_small_terminal(self):
        self.app.index = 1
        self.app.confirm = dict(self.rows[1])
        screen = self.render(18,70)
        self.assertIn('移除守护',screen.text)
        self.assertIn('y 确认移除',screen.text)
        self.assertEqual(self.app.selected()['thread_id'],'two')
        self.app.confirm = None
        self.app.interval_input = '0.5'
        screen = self.render(18,70)
        self.assertIn('检查间隔',screen.text)
        self.assertIn('0.5▏',screen.text)

    def test_state_labels_and_monochrome_selection_are_accessible(self):
        screen = self.render()
        self.assertIn('正常工作',screen.text)
        self.assertIn('等待重试',screen.text)
        self.assertIn('守护已暂停',screen.text)
        self.assertTrue(any(attr & curses.A_REVERSE for row in screen.attributes for attr in row))

    def test_selected_rule_remains_visible_after_wrapping_and_resize(self):
        self.app.mode='patterns'
        for height,width in ((18,70),(30,130),(18,70)):
            for index,name in enumerate(('模型容量不足','模型请求连接失败','配额或速率限制','认证失败','服务内部错误')):
                self.app.index=index
                self.assertIn('› '+name,self.render(height,width).text)

    def test_english_failure_messages_wrap_at_word_boundaries(self):
        text='Selected model is at capacity. Please try a different model.'
        lines=readable_wrap(text,39)
        self.assertEqual(' '.join(lines),text)
        self.assertTrue(all(cells(line)<=39 for line in lines))
        self.assertNotEqual(lines[0][-1],'t')

    def test_attention_card_handles_real_error_messages(self):
        self.rows[0].update(error='daemon disconnected',phase='suspended')
        self.assertIn('1 个会话',self.render().text)


class ThemeTests(unittest.TestCase):
    def test_theme_can_be_selected_without_changing_terminal_settings(self):
        with patch.dict(os.environ,{'CODEX_WATCHDOG_THEME':'light'},clear=True):
            self.assertEqual(resolve_theme(),'light')
        with patch.dict(os.environ,{'COLORFGBG':'0;15'},clear=True):
            self.assertEqual(resolve_theme(),'light')
        with patch.dict(os.environ,{'NO_COLOR':'','CODEX_WATCHDOG_THEME':'dark'},clear=True):
            self.assertEqual(resolve_theme(),'mono')

    def test_extended_and_basic_terminals_initialize_semantic_colors(self):
        for colors in (8,256):
            with self.subTest(colors=colors), patch.dict(os.environ,{},clear=True), patch('codex_watchdog.theme.curses.has_colors',return_value=True), \
                    patch('codex_watchdog.theme.curses.start_color'), \
                    patch('codex_watchdog.theme.curses.COLORS',colors,create=True), \
                    patch('codex_watchdog.theme.curses.init_pair') as init, \
                    patch('codex_watchdog.theme.curses.color_pair',side_effect=lambda n:n*256):
                palette=Palette('dark')
                palette.initialize(Mock())
                self.assertEqual(len(palette.pairs),10)
                self.assertNotEqual(palette.attr('success'),palette.attr('danger'))
                self.assertTrue(all(0<=value<colors for call in init.call_args_list for value in call.args[1:]))

    def test_no_color_terminal_still_has_selection_and_warning_emphasis(self):
        palette=Palette('mono')
        with patch('codex_watchdog.theme.curses.has_colors',return_value=False):
            palette.initialize(Mock())
        self.assertEqual(palette.attr('selection'),curses.A_REVERSE)
        self.assertEqual(palette.attr('danger'),curses.A_BOLD)


class SetupPresentationTests(unittest.TestCase):
    def test_configuration_fields_and_authorization_fit_both_terminal_sizes(self):
        app=Setup('/tmp/watchdog-config.json')
        app.values[0]='/very/long/路径/'*20
        for height,width in ((18,70),(30,130)):
            for selected in range(4):
                app.index=selected
                screen=Buffer(height,width)
                app.draw(screen)
                self.assertIn('首次配置',screen.text)
                self.assertIn('容量故障自动恢复',screen.text)
                self.assertIn('Enter',screen.text)
                if selected<3:
                    self.assertIn('▏',screen.text)
