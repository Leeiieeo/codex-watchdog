"""Current-terminal application startup and command-line controls."""
import argparse
import curses
import json
import locale
import os
from pathlib import Path
import signal
import subprocess
import sys

from .registry import add_session, discover_sessions, load_config, remove_session, set_session_enabled
from .service import Service
from .setup import default_config, initialize

ROOT = Path(__file__).resolve().parents[2]


def run_application(config_path, config):
    from .tui import App

    locale.setlocale(locale.LC_ALL, '')
    service = Service(config_path, config)
    app = App(str(config_path), service=service)
    handlers = {}

    def terminate(*_):
        app.quit = True

    try:
        handlers[signal.SIGINT] = signal.signal(signal.SIGINT, lambda *_: app.interrupt())
        for sig in (signal.SIGTERM, signal.SIGHUP):
            handlers[sig] = signal.signal(sig, terminate)

        def run(screen):
            if app.quit:
                return
            service.start()
            app.run(screen)

        curses.wrapper(run)
    finally:
        # Further terminal interrupts must not interrupt child cleanup.
        for sig in handlers:
            signal.signal(sig, signal.SIG_IGN)
        try:
            service.stop()
        finally:
            for sig, handler in handlers.items():
                signal.signal(sig, handler)


def main():
    parser = argparse.ArgumentParser(description='Codex Watchdog controls')
    parser.add_argument('--config', default=default_config())
    parser.add_argument('action', nargs='?', default='start', choices=(
        'tui', 'start', 'stop', 'status', 'discover', 'add', 'remove', 'check', 'logs',
        'doctor', 'init', 'pause', 'resume', 'events'))
    parser.add_argument('values', nargs='*')
    args = parser.parse_args()
    path = Path(args.config).expanduser().resolve()
    try:
        if args.action == 'doctor':
            from .diagnostics import doctor
            result = doctor(path)
            for row in result['checks']:
                print(f"[{row['level']}] {row['name']}: {row['message']}")
            if not result['ok']:
                parser.exit(1)
            return
        if args.action == 'init' or (args.action in ('start', 'tui') and not path.exists()):
            saved = initialize(path)
            if args.action == 'init' or not saved:
                return
        config = load_config(path)
        state_dir = Path(config['state_dir'])
        if args.action in ('start', 'tui'):
            run_application(path, config)
        elif args.action == 'stop':
            state_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
            (state_dir/'STOP').touch()
            print('Shutdown requested for the TUI and watchdog service.')
        elif args.action == 'status':
            from .view import dashboard
            data = dashboard(path)
            print('Watchdog: '+data['health'])
            print(json.dumps({k: v for k, v in data.items() if k != 'config'},
                             indent=2, ensure_ascii=False))
        elif args.action == 'discover':
            print(json.dumps(discover_sessions(path), indent=2, ensure_ascii=False))
        elif args.action == 'add':
            if not 1 <= len(args.values) <= 2:
                parser.error('add THREAD_ID [LABEL]')
            print(json.dumps(add_session(path, *args.values), ensure_ascii=False))
        elif args.action == 'remove':
            if len(args.values) != 1:
                parser.error('remove THREAD_ID')
            print('Removed.' if remove_session(path, args.values[0]) else 'Already absent.')
        elif args.action == 'check':
            subprocess.run([sys.executable, '-m', 'codex_watchdog.watchdog',
                            '--config', str(path), '--once'], check=True)
        elif args.action == 'logs':
            from .view import log_lines
            print('\n'.join(log_lines(config)))
        elif args.action in ('pause', 'resume'):
            if len(args.values) != 1:
                parser.error(args.action+' THREAD_ID')
            set_session_enabled(path, args.values[0], args.action == 'resume')
            print('Guarding '+('resumed with a new observation baseline.' if args.action == 'resume' else 'paused.'))
        elif args.action == 'events':
            from .events import read_events
            for event in read_events(config['state_dir'], args.values[0] if args.values else None):
                print(json.dumps(event, ensure_ascii=False))
    except (OSError, ValueError, subprocess.CalledProcessError, RuntimeError, curses.error) as exc:
        parser.exit(1, f'{exc}\n')


if __name__ == '__main__':
    main()
