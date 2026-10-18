"""Semantic color roles with 256-color, basic-color and monochrome fallbacks."""
import curses
import os

THEMES = {
    'dark': {'text':(252,234), 'heading':(255,234), 'muted':(245,234), 'border':(239,234),
             'accent':(81,234), 'success':(114,234), 'warning':(222,234), 'danger':(210,234),
             'selection':(255,24), 'dialog':(252,236)},
    'light': {'text':(235,255), 'heading':(232,255), 'muted':(241,255), 'border':(249,255),
              'accent':(25,255), 'success':(28,255), 'warning':(130,255), 'danger':(160,255),
              'selection':(255,25), 'dialog':(235,253)},
}
PAIR_IDS = {role:n for n,role in enumerate(THEMES['dark'],1)}


def resolve_theme(requested=None):
    requested = requested or os.environ.get('CODEX_WATCHDOG_THEME','auto')
    if requested not in ('auto','dark','light','mono'):
        requested = 'auto'
    if os.environ.get('NO_COLOR') is not None:
        return 'mono'
    if requested != 'auto':
        return requested
    try:
        background = int(os.environ.get('COLORFGBG','').split(';')[-1])
    except ValueError:
        background = 0
    return 'light' if background in (7,15) else 'dark'


class Palette:
    def __init__(self, requested=None):
        self.name = resolve_theme(requested)
        self.pairs = {}

    def initialize(self, screen):
        self.pairs = {}
        if self.name != 'mono' and curses.has_colors():
            try:
                curses.start_color()
                extended = curses.COLORS >= 256
                if extended:
                    colors = THEMES[self.name]
                else:
                    background = curses.COLOR_WHITE if self.name == 'light' else curses.COLOR_BLACK
                    foreground = curses.COLOR_BLACK if self.name == 'light' else curses.COLOR_WHITE
                    colors = {role:(foreground,background) for role in PAIR_IDS}
                    colors.update(accent=(curses.COLOR_BLUE if self.name=='light' else curses.COLOR_CYAN,background),
                                  success=(curses.COLOR_GREEN,background), warning=(curses.COLOR_MAGENTA if self.name=='light' else curses.COLOR_YELLOW,background),
                                  danger=(curses.COLOR_RED,background), selection=(curses.COLOR_WHITE,curses.COLOR_BLUE))
                for role,(foreground,background) in colors.items():
                    try:
                        pair = PAIR_IDS[role]
                        curses.init_pair(pair,foreground,background)
                        self.pairs[role] = pair
                    except curses.error:
                        pass
            except curses.error:
                pass
        try:
            screen.bkgd(' ',self.attr('text'))
        except curses.error:
            pass

    def attr(self, role='text'):
        if role in self.pairs:
            try:
                return curses.color_pair(self.pairs[role])
            except curses.error:
                pass
        if role == 'selection':
            return curses.A_REVERSE
        if role in ('muted','border'):
            return curses.A_DIM
        if role in ('heading','warning','danger'):
            return curses.A_BOLD
        return 0
