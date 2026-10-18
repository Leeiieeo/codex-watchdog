"""Read-only installation, connection, schema and state diagnostics."""
from contextlib import closing
import fcntl
import os
from pathlib import Path
import sqlite3
import stat
import sys

from .patterns import safe_text
from .registry import load_config
from .rpc import Client
from .storage import load_state


def doctor(config_path):
    checks = []
    def add(name, level, message):
        checks.append({'name': name, 'level': level, 'message': safe_text(message)})
    add('Python', 'ok' if sys.version_info >= (3, 10) else 'warning',
        '需要 Python 3.10+；当前 '+sys.version.split()[0])
    try:
        config = load_config(config_path)
        add('配置', 'ok', str(Path(config_path).expanduser().resolve()))
    except (OSError, ValueError, KeyError, TypeError) as exc:
        add('配置', 'error', str(exc)+'；使用 init 创建配置或修正现有文件')
        return {'ok': False, 'checks': checks}
    root = Path(config['state_dir'])
    ancestor = root
    while not ancestor.exists():
        ancestor = ancestor.parent
    writable = os.access(ancestor, os.W_OK | os.X_OK)
    add('状态目录', 'ok' if writable else 'error',
        str(root)+('（尚未创建，启动时创建）' if not root.exists() else ''))
    for name in ('service.lock', 'watchdog.lock'):
        path = root/name
        if path.exists():
            try:
                with path.open('r') as lock:
                    fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                add(name, 'ok', '没有运行实例占用')
            except BlockingIOError:
                add(name, 'warning', '已有运行实例占用；运行期间这是正常状态')
            except OSError as exc:
                add(name, 'error', str(exc))
    try:
        state = load_state(root/'state.json')
        add('恢复状态', 'ok', f'读取成功，保留 {len(state)} 个会话的基线和去重记录')
    except (OSError, ValueError) as exc:
        add('恢复状态', 'error', str(exc))
    database = Path(config['codex_home'])/'goals_1.sqlite'
    try:
        with closing(sqlite3.connect(database.as_uri()+'?mode=ro', uri=True, timeout=1)) as db:
            db.execute('PRAGMA query_only=ON')
            columns = {row[1] for row in db.execute('PRAGMA table_info(thread_goals)')}
            required = {'thread_id', 'goal_id', 'status', 'token_budget', 'tokens_used',
                        'created_at_ms', 'updated_at_ms'}
            if not required <= columns:
                raise ValueError('缺少 Goal 字段：'+', '.join(sorted(required-columns)))
        add('Goal 数据库', 'ok', '只读打开成功，表结构兼容')
    except (sqlite3.Error, ValueError, OSError) as exc:
        add('Goal 数据库', 'error', str(exc)+'；检查 codex_home 和 Codex 版本')
    client = None
    try:
        path = Path(config['socket_path'])
        if not stat.S_ISSOCK(path.stat().st_mode):
            raise ValueError('socket_path 不是 Unix socket')
        client = Client(str(path))
        loaded = client.request('thread/loaded/list', {}).get('data', [])
        add('Codex 连接', 'ok', f'握手及只读 RPC 成功，{len(loaded)} 个已加载会话')
        from .watchdog import snapshot
        for target in config['targets']:
            try:
                snapshot(client, target, config['codex_home'])
                add(target['label'], 'ok', '会话绑定和 Goal/轮次接口兼容')
            except Exception as exc:
                add(target['label'], 'error', str(exc))
    except Exception as exc:
        add('Codex 连接', 'error', str(exc)+'；确认 Codex 已运行且 socket_path 正确')
    finally:
        if client:
            client.close()
    return {'ok': not any(row['level'] == 'error' for row in checks), 'checks': checks}
