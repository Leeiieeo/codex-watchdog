"""Stable decision identifiers and human-readable explanations."""

REASONS = {
    'baseline': '首次检查已建立基线；已有失败不会自动恢复',
    'no_goal': '该会话没有 Goal',
    'historical_failure': '该失败已在观察基线中，不属于新的失败',
    'guard_paused': '已暂停此会话的自动守护；仍继续读取状态',
    'guard_resumed': '守护或规则设置已变更；当前轮次已作为基线',
    'running': 'Codex 正在执行，无需恢复',
    'healthy': '最新轮次已完成',
    'goal_complete': 'Goal 已完成',
    'goal_limited': 'Goal 已因用量或预算限制停止',
    'budget_exhausted': 'Goal 的 token 预算已耗尽',
    'manual_pause': 'Goal 已暂停，未确认是当前容量故障引起',
    'blocked': 'Goal 的阻塞与当前容量失败不符合时间关联条件',
    'unavailable': '会话未加载、等待交互或运行状态不可确认',
    'observe_only': '当前规则只记录失败，不执行自动恢复',
    'pattern_disabled': '此失败规则已禁用',
    'unmatched_failure': '此失败没有匹配到已知规则，已记录供检查',
    'backoff': '已匹配容量故障，等待退避到期后复核',
    'due': '退避已到期，准备重新读取并复核',
    'changed': 'Goal、轮次或错误发生变化，已取消待恢复操作',
    'changed_before_resume': '提交前复核未通过，已取消恢复',
    'already_attempted': '本失败轮次已提交过恢复，等待新的执行轮次',
    'unknown_outcome': '恢复请求结果不明确，本轮不会重复提交',
    'resume_intent': '已保存本轮恢复记录，准备提交恢复',
    'resume_accepted': 'Codex 已接受恢复请求，等待观察后续轮次',
    'watching': '正在监测，尚无符合条件的新容量故障',
    'observation_error': '读取失败，无法确认会话状态；停止恢复操作',
}


def decision(state, phase, code, **changes):
    state.update(phase=phase, reason_code=code, reason=REASONS[code], **changes)
