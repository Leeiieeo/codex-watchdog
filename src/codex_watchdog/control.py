"""CLI compatibility and tmux process supervision."""
import argparse
import json
import os
from pathlib import Path
import shlex
import subprocess
import sys
import time

from .registry import add_session, discover_sessions, load_config, remove_session

ROOT = Path(__file__).resolve().parents[2]
SESSION = 'codex-watchdog'


def tmux(*args, check=True):
    return subprocess.run(['tmux',*args],text=True,stdout=subprocess.PIPE,
                          stderr=subprocess.PIPE,check=check)


def windows():
    result = tmux('list-windows','-t','='+SESSION,'-F',
                  '#{window_name}\t#{window_id}\t#{pane_dead}\t#{pane_current_command}',check=False)
    return {parts[0]: parts[1:] for line in result.stdout.splitlines()
            if len(parts := line.split('\t')) == 4}


def command(config_path, module, *args):
    return shlex.join(['env','PYTHONPATH='+str(ROOT/'src'),sys.executable,'-m',module,
                      '--config',str(config_path),*args])


def show_tui(config_path,replace=False):
    current = windows().get('status')
    target = command(config_path,'codex_watchdog.tui')
    if current and current[1] == '0':
        if replace or current[2] == 'watch':
            tmux('respawn-pane','-k','-t',current[0],target)
    else:
        tmux('new-window','-d','-t',SESSION+':','-n','status','-c',str(ROOT),target)
    tmux('select-window','-t','='+SESSION+':status')


def supervise(config_path, config):
    stop = Path(config['state_dir'])/'STOP'
    print('Codex watchdog worker. Use the status window for the TUI.',flush=True)
    env = {**os.environ,'PYTHONPATH':str(ROOT/'src')}
    while not stop.exists():
        try:
            result = subprocess.run([sys.executable,'-m','codex_watchdog.watchdog',
                                     '--config',str(config_path)],env=env)
        except KeyboardInterrupt:
            return
        if stop.exists(): return
        print(f'Worker exited ({result.returncode}); retry in 10 seconds.',flush=True)
        deadline = time.monotonic()+10
        while not stop.exists() and time.monotonic() < deadline: time.sleep(.2)


def main():
    parser = argparse.ArgumentParser(description='Codex Watchdog controls')
    parser.add_argument('--config',default=os.environ.get('CODEX_WATCHDOG_CONFIG',str(ROOT/'config.json')))
    parser.add_argument('action',nargs='?',default='tui',choices=(
        'tui','start','stop','status','discover','add','remove','check','logs','supervise'))
    parser.add_argument('values',nargs='*')
    args = parser.parse_args()
    path = Path(args.config).expanduser().resolve()
    if args.action == 'tui':
        from .tui import App
        import curses
        import locale
        locale.setlocale(locale.LC_ALL,'')
        try: curses.wrapper(App(str(path)).run)
        except KeyboardInterrupt: pass
        return
    try:
        config = load_config(path)
        state_dir = Path(config['state_dir'])
        if args.action == 'start':
            state_dir.mkdir(parents=True,exist_ok=True,mode=0o700)
            worker = windows().get('worker')
            if worker and worker[1] == '0':
                show_tui(path)
                print('Watchdog worker already running; TUI selected.')
                return
            (state_dir/'STOP').unlink(missing_ok=True)
            target = command(path,'codex_watchdog.control','supervise')
            if tmux('has-session','-t','='+SESSION,check=False).returncode == 0:
                tmux('new-window','-d','-t',SESSION+':','-n','worker','-c',str(ROOT),target)
            else:
                tmux('new-session','-d','-s',SESSION,'-n','worker','-c',str(ROOT),target)
            show_tui(path,replace=True)
            print('Started watchdog and TUI on this server.')
        elif args.action == 'stop':
            state_dir.mkdir(parents=True,exist_ok=True,mode=0o700)
            (state_dir/'STOP').touch()
            print('Stop requested. Codex itself is unaffected.')
        elif args.action == 'status':
            from .view import dashboard
            data = dashboard(path)
            print('Remote watchdog: '+data['health'])
            print(json.dumps({k:v for k,v in data.items() if k!='config'},indent=2,ensure_ascii=False))
        elif args.action == 'discover':
            print(json.dumps(discover_sessions(path),indent=2,ensure_ascii=False))
        elif args.action == 'add':
            if not 1 <= len(args.values) <= 2: parser.error('add THREAD_ID [LABEL]')
            print(json.dumps(add_session(path,*args.values),ensure_ascii=False))
        elif args.action == 'remove':
            if len(args.values) != 1: parser.error('remove THREAD_ID')
            print('Removed.' if remove_session(path,args.values[0]) else 'Already absent.')
        elif args.action == 'check':
            subprocess.run([sys.executable,'-m','codex_watchdog.watchdog','--config',str(path),'--once'],check=True)
        elif args.action == 'logs':
            from .view import log_lines
            print('\n'.join(log_lines(config)))
        elif args.action == 'supervise': supervise(path,config)
    except (OSError,ValueError,subprocess.CalledProcessError,RuntimeError) as exc:
        parser.exit(1,f'{exc}\n')


if __name__ == '__main__': main()
