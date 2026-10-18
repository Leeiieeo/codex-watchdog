"""Explicit failure classification; only capacity failures can be recovered."""
from dataclasses import dataclass
import hashlib
import json
import re

CAPACITY_ERROR_CODE = "serverOverloaded"
CAPACITY_LABEL = "selected model is at capacity"
CAPACITY_MESSAGE = "selected model is at capacity. please try a different model."


@dataclass(frozen=True)
class Pattern:
    id: str
    name: str
    codes: tuple
    messages: tuple = ()
    recoverable: bool = False


PATTERNS = (
    Pattern('capacity', CAPACITY_LABEL, (CAPACITY_ERROR_CODE,), (CAPACITY_MESSAGE,), True),
    Pattern('connection', '模型请求连接失败', (
        'connectionFailed', 'connectionFailure', 'networkError',
        'responseStreamDisconnected', 'responseStreamConnectionFailed'),
        ('connection failed', 'connection failed.', 'connection failure', 'connection failure.')),
    Pattern('quota', '配额或速率限制', ('usageLimitExceeded', 'rateLimitExceeded', 'billingError')),
    Pattern('authentication', '认证失败', ('unauthorized', 'authenticationFailed')),
    Pattern('server_error', '服务内部错误', ('internalServerError',)),
)
DEFAULT_MODES = {p.id: 'recover' if p.recoverable else 'observe' for p in PATTERNS}


def classify(error):
    if not isinstance(error, dict):
        return None
    code = error.get('codexErrorInfo')
    if isinstance(code, dict):
        code = next(iter(code), None) if len(code) == 1 else 'unknown'
    message = str(error.get('message', '')).strip().lower()
    for pattern in PATTERNS:
        if code in pattern.codes or (code in (None, 'other') and message in pattern.messages):
            return pattern
    return None


def pattern_mode(config, pattern):
    if pattern is None:
        return 'observe'
    mode = config.get('pattern_modes', {}).get(pattern.id, DEFAULT_MODES[pattern.id])
    return 'observe' if mode == 'recover' and config.get('observe_only') else mode


def safe_text(value, limit=2000):
    text = str(value)
    # Error messages may contain authenticated URLs or headers. Keep evidence
    # useful without persisting URL credentials, queries, or bearer secrets.
    text = re.sub(r'(https?://)[^/\s@]+@', r'\1[redacted]@', text, flags=re.I)
    text = re.sub(r'(https?://[^\s?#]+)[?#][^\s]*', r'\1?[redacted]', text, flags=re.I)
    text = re.sub(r'\b(Bearer\s+)[\w.\-/+=]+', r'\1[redacted]', text, flags=re.I)
    text = re.sub(r'\b(token|api[_-]?key|authorization|password|secret)(\s*[=:]\s*)[^\s,;]+',
                  r'\1\2[redacted]', text, flags=re.I)
    return text[:limit]


def error_fingerprint(error):
    return hashlib.sha256(json.dumps(error, sort_keys=True, separators=(',', ':')).encode()).hexdigest()
