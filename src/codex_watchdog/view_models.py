"""Validated display models. These never authorize or perform Goal recovery."""
from dataclasses import asdict, dataclass, field, replace
from enum import Enum
import math

from .patterns import safe_text

_UNREAD = object()


class ViewDataError(ValueError):
    pass


class Observation(str, Enum):
    PENDING = 'pending'
    CURRENT = 'current'
    FAILED = 'failed'
    STALE = 'stale'


class GoalPresence(str, Enum):
    UNKNOWN = 'unknown'
    ABSENT = 'absent'
    PRESENT = 'present'


def text_value(value, name):
    if value is not None and not isinstance(value, str):
        raise ViewDataError(name+' must be a string or null')
    return value


def number_value(value, name, default=None, *, integer=False):
    if value is None:
        return default
    try:
        valid = (not isinstance(value, bool) and isinstance(value, (int, float))
                 and math.isfinite(value) and value >= 0
                 and (not integer or isinstance(value, int)))
    except OverflowError:
        valid = False
    if not valid:
        raise ViewDataError(name+' must be a finite nonnegative '+('integer' if integer else 'number'))
    return value


def flag_value(value, name, default=False):
    if value is None:
        return default
    if not isinstance(value, bool):
        raise ViewDataError(name+' must be a boolean')
    return value


def object_value(value, name):
    if not isinstance(value, dict):
        raise ViewDataError(name+' must be an object')
    return value


@dataclass(frozen=True)
class GoalView:
    presence: GoalPresence = GoalPresence.UNKNOWN
    status: str | None = None

    @classmethod
    def parse(cls, observation):
        if 'goal_status' not in observation:
            return cls()
        status = text_value(observation['goal_status'], 'goal_status')
        if status is None:
            return cls(GoalPresence.ABSENT)
        if status in ('', '-'):
            return cls()
        return cls(GoalPresence.PRESENT, status)


@dataclass(frozen=True)
class FailureView:
    message: str = ''
    code: str | None = None
    pattern: str | None = None
    pattern_name: str | None = None
    turn_id: str | None = None
    at: float | None = None

    @classmethod
    def parse(cls, raw):
        data = object_value(raw, 'last_failure')
        return cls(message=safe_text(text_value(data.get('message'), 'failure.message') or ''),
                   **{key: text_value(data.get(key), 'failure.'+key)
                      for key in ('code', 'pattern', 'pattern_name', 'turn_id')},
                   at=number_value(data.get('at'), 'failure.at'))


@dataclass(frozen=True)
class MetricsView:
    failures: int = 0
    attempts: int = 0
    accepted: int = 0
    recoveries: int = 0
    completed_goals: int = 0
    downtime_seconds: float = 0
    last_event_at: float | None = None

    @classmethod
    def parse(cls, raw):
        data = object_value(raw, 'metrics')
        counters = {key: number_value(data.get(key), 'metrics.'+key, 0, integer=True)
                    for key in ('failures', 'attempts', 'accepted', 'recoveries', 'completed_goals')}
        return cls(**counters, downtime_seconds=number_value(data.get('downtime_seconds'), 'metrics.downtime_seconds', 0),
                   last_event_at=number_value(data.get('last_event_at'), 'metrics.last_event_at'))


@dataclass(frozen=True)
class SessionView:
    thread_id: str
    cwd: str
    label: str
    title: str = ''
    registration_id: str | None = None
    guard_revision: str | None = None
    enabled: bool = True
    observation: Observation = Observation.PENDING
    runtime: str | None = None
    goal: GoalView = field(default_factory=GoalView)
    goal_id: str | None = None
    turn_id: str | None = None
    turn_status: str | None = None
    phase: str = 'pending_observation'
    reason: str = ''
    reason_code: str | None = None
    error: str | None = None
    attempts: int = 0
    updated_at: float | None = None
    checked_at: float | None = None
    last_success_at: float | None = None
    next_retry_at: float | None = None
    outage_started_at: float | None = None
    capacity_error: bool = False
    attention: bool = False
    stalled: bool = False
    recovery_unconfirmed: bool = False
    last_failure: FailureView | None = None
    stats: MetricsView = field(default_factory=MetricsView)
    lifetime_stats: MetricsView = field(default_factory=MetricsView)
    recovery: dict | None = None

    def to_dict(self):
        data = asdict(self)
        data.pop('goal')
        data.update(goal_status=self.goal.status, goal_presence=self.goal.presence.value,
                    observation=self.observation.value)
        return data


def session_view(target, observed=_UNREAD, metrics=None, *, stale=False, error=None):
    """Keep authoritative identity separate from optional, fallible observations."""
    identity = {key: text_value(target.get(key), key) for key in ('thread_id', 'cwd', 'label')}
    if not all(identity.values()):
        raise ViewDataError('session identity needs thread_id, cwd and label')
    base = SessionView(**identity,
        registration_id=text_value(target.get('registration_id'), 'registration_id'),
        guard_revision=text_value(target.get('guard_revision'), 'guard_revision'),
        enabled=flag_value(target.get('enabled'), 'enabled', True))
    try:
        data = {} if observed is _UNREAD else object_value(observed, 'session observation')
        if (data.get('registration_id', base.registration_id) != base.registration_id
                or data.get('guard_revision') != base.guard_revision):
            data = {}
        phase = text_value(data.get('phase'), 'phase') or 'pending_observation'
        problem = error or text_value(data.get('error'), 'error')
        if problem and phase == 'pending_observation':
            phase = 'suspended'
        observation = (Observation.FAILED if problem else Observation.PENDING if phase == 'pending_observation'
                       else Observation.STALE if stale else Observation.CURRENT)
        strings = {key: text_value(data.get(key), key)
                   for key in ('runtime', 'goal_id', 'turn_id', 'turn_status', 'reason_code')}
        timestamps = {key: number_value(data.get(key), key)
                      for key in ('updated_at', 'checked_at', 'last_success_at', 'next_retry_at', 'outage_started_at')}
        flags = {key: flag_value(data.get(key), key)
                 for key in ('capacity_error', 'attention', 'stalled', 'recovery_unconfirmed')}
        recovery = data.get('recovery')
        if recovery is not None:
            recovery = object_value(recovery, 'recovery')
        return replace(base, **strings, **timestamps, **flags,
            title=text_value(target.get('title') or data.get('title'), 'title') or '',
            observation=observation, goal=GoalView.parse(data), phase=phase,
            error=safe_text(problem) if problem else None,
            reason=text_value(data.get('reason'), 'reason') or ('读取失败，无法确认会话状态' if problem else ''),
            attempts=number_value(data.get('attempts'), 'attempts', 0, integer=True),
            last_failure=FailureView.parse(data['last_failure']) if data.get('last_failure') is not None else None,
            stats=MetricsView.parse(data.get('stats', {})),
            lifetime_stats=MetricsView.parse(metrics if metrics is not None else {}), recovery=recovery)
    except ViewDataError as exc:
        return replace(base, observation=Observation.FAILED, phase='suspended',
                       reason_code='invalid_observation', reason='会话状态数据无效，请查看诊断或日志',
                       error=str(exc))


def discovered_session(raw):
    """Discovery has thread metadata but has not read the Goal or recovery state."""
    data = object_value(raw, 'discovered session')
    return session_view(data, {key: data[key] for key in ('runtime', 'updated_at') if key in data})


@dataclass(frozen=True)
class DashboardView:
    config: dict = field(default_factory=dict)
    rows: tuple[SessionView, ...] = ()
    health: str = 'stopped'
    service_health: str = 'stopped'
    monitor_health: str = 'empty'
    age: float | None = None
    pid: int | None = None
    error: str | None = None
    check_interval_seconds: int = 300
    next_check_at: float | None = None
    successful_targets: int = 0
    last_success_at: float | None = None
    last_success_age: float | None = None
    service: dict = field(default_factory=dict)

    def to_dict(self):
        data = asdict(self)
        data.pop('config')
        data['rows'] = [row.to_dict() for row in self.rows]
        return data

    def unavailable(self, message):
        rows = tuple(replace(row, observation=Observation.STALE, next_retry_at=None)
                     if row.observation == Observation.CURRENT else row for row in self.rows)
        return replace(self, rows=rows, health='error', monitor_health='error',
                       successful_targets=0, error=message)
