# Codex capacity watchdog

Run on the Linux server under the same user as Codex. Recover only explicitly configured Codex threads after a **new, final `serverOverloaded` failure**. Normal work must receive no input or mutations. Use the existing daemon's Unix WebSocket API; never inject tmux keys, start turns, create goals, change models, change budgets, or restart Codex.

## Recovery contract

- Poll every 300 seconds by default with read-only RPCs. `check_interval_seconds` can be changed in the TUI without restarting. Local configuration changes are checked once a second; enrollment changes wake the observer promptly. Read the goal's identity/revision from `goals_1.sqlite` in SQLite read-only mode; fail closed if the schema or database is unavailable.
- On first observation, baseline the latest turn, including any old failure. Recovery requires observing an active goal before a new failed turn. A new goal resets the baseline.
- Only `serverOverloaded` (or the exact known capacity message when no specific contradictory code exists) is eligible. Ignore quota, billing, authentication, approval, interruption, unrelated blocked, completed, and unknown states.
- Runtime must be `idle` or `systemError`, latest turn `failed`, goal `active` or a failure-correlated `paused`/`blocked`. Codex 0.162.1 reports capacity failures as `systemError` + `blocked`; only accept such a block when its millisecond revision is in the same second as the final capacity failure. A paused goal is eligible only when it was previously active and its revision falls within the failed turn, plus at most 2 seconds for timestamp resolution. Any subsequent goal revision cancels the pending recovery.
- Backoff: 60, 120, 240, 300 seconds, with 0–10% additive jitter. Continue on new capacity failures; never submit the same failed turn twice. Persist the attempted turn **before** the mutation. Ambiguous acknowledgements do not cause duplicate writes.
- Retry at the next configured observation after the backoff is due; the displayed retry time includes this scheduling delay. Staleness detection allows the configured interval plus 20 seconds, rather than treating a five-minute wait as an outage.
- Immediately before a mutation, read everything again. Require the same goal revision, failed turn, error, eligible runtime, and remaining budget. Restore only `status=active`, using the user's explicitly authorized resume operation; do not replace the objective or budget.
- A successful API response is only an accepted resume. A subsequent completed turn records recovery. A running turn suspends all recovery writes. No new turn after an accepted resume is logged for inspection and never triggers blind repeated writes.
- State uncertainty, changed goal, changed latest turn, cleared goal, unloaded thread, disconnected daemon, or a stop sentinel prevents mutations.

## Operation and verification

Keep source in a Git checkout and machine configuration/state in a private runtime directory; use small rotating logs, a singleton lock, atomic state files, and an independent remote tmux session. The remote user manager has `Linger=no`; tmux avoids relying on the local SSH connection or changing the user's login settings. Binding is by exact thread ID, not by pane names or a "latest session" guess. New chats need explicit registration.

Test the policy against active work, historical failures, manual pause, completed goals, pending approvals, new user input, capacity backoff, duplicate failures, restart, database/API mismatch, and uncertain acknowledgement. Test the actual Unix WebSocket transport with a local fake server. Verify real server snapshots in observation-only mode; do not manufacture failures or resume the user's paused/completed goals as a test.
