# Commands

## Help

```sh
relay help
relay help account
relay help workspace
relay run --help
relay run -- exec --help
relay help codex resume
```

`relay run --help` shows the installed Codex command and flag list, followed by
Relay's launch options. Put native arguments after `relay run --`.

## Accounts

```sh
relay setup --count 1
relay setup --count 6
relay add personal
relay list
relay account identify
relay refresh
relay account rename personal "My account"
relay account disable personal
relay account enable personal
relay account priority personal 10
relay account cap personal 2
relay account models personal YOUR_MODEL
relay account reauth personal
relay account remove personal
```

Accounts display their saved email addresses. `account identify` fills in emails
for earlier installations without another sign-in. Commands accept an account's
label, unique email, or chosen display name. Renaming keeps the login usable.
A session cap of zero means unlimited. Removing an account retains its login
unless you request `--revoke`.

## Launch and recovery

```sh
relay run
relay run -- resume --yolo
relay run -- -C ~/projects/website resume
relay run -- -p work --add-dir ~/shared
relay run -- --worktree "Try the next change"
relay run -- exec --json "Run the checks"
relay run -- review --uncommitted
relay explain -- resume --yolo
relay run --require-recovery -- resume
```

Resume defaults to the current folder. Add `--all` to browse other folders or
`-C/--cd` to choose another working folder. Native profile files are read from
`$CODEX_HOME/NAME.config.toml`; explicit config flags override the profile.
Relay worktrees are detached Git worktrees under its private state folder. Their
files are retained for review; cleanup does not delete worktrees.

Interactive work, resume/fork, profiles, extra directories, worktrees, and supported
noninteractive exec/review runs use the account pool. Exec supports text/stdin,
images, JSON events, output schemas, last-message files, and resume/fork. Review
supports the working tree, base branch, commit, or custom instructions.

Native utility commands are forwarded unchanged. Local providers, explicitly
remote servers, hook-trust bypass, and ignoring native config/rules use native
Codex directly with its ordinary login. Help/version also run directly. These
modes do not use Relay recovery. `--require-recovery` rejects such a launch, and
`explain` shows which mode applies without making an inference request.

Noninteractive runs cannot approve a tool request on your behalf. Use explicit
native permissions such as `--yolo` only when intended, or resume interactively
when input is required. Automatic review is supported by `--approve-for-me`.

## Dashboard and session controls

```sh
relay status
relay status --watch
relay status --all
relay status --json
relay menubar
relay menubar --no-startup
relay attach SESSION_ID
relay attach SESSION_ID --takeover
relay pause SESSION_ID --wait
relay unpause SESSION_ID --wait
relay retry SESSION_ID --wait
relay switch SESSION_ID --account personal --wait
relay stop SESSION_ID --wait
relay restart SESSION_ID
```

The watch view waits for local file changes and redraws only when useful status
changes. Heartbeats do not repeatedly print the table. Watching uses no model
allowance and does not fetch account allowance. `refresh` explicitly checks it.
The menu uses one local status stream, a template icon, and native session actions.
It opens at Mac login after `relay menubar`; disable that in its menu or with
`relay menubar --no-startup --configure-only`.

IDs may be shortened to a unique prefix. Without `--wait`, controls are queued;
with it, Relay waits for the worker's confirmation and reports a rejection.
Pause stops automatic recovery after current work finishes. Retry clears a review
hold: inspect an interrupted external action in the conversation first.

## Background tasks and saved workspaces

```sh
relay start --name website --cwd ~/projects/website --prompt "Run the checks"
relay workspace save daily
relay workspace list
relay workspace restore daily
relay workspace remove daily
relay startup enable SESSION_ID
relay startup list
relay startup disable SESSION_ID
```

A workspace saves the same supervised session IDs, rather than starting fresh
conversations. Restore restarts stopped jobs and prints reconnect commands; live
jobs are retained. Save specific sessions by appending their IDs to `workspace save`.

The normal command remains available after reboot. Background task recovery at
login is a separate opt-in setting; enabling the menu does not enable task startup.
Uncertain work still pauses for review after recovery.

## Preferences

```sh
relay config
relay config notifications true
relay config background_on_disconnect true
relay config keep_awake true
relay config retry_interval 300
relay config retry_attempts 3
relay config routing quota
relay config preferred_accounts '["personal"]'
relay config fallback_models '["YOUR_CHOSEN_MODEL"]'
relay config retry_interval 60 --profile quick
relay run --profile quick
```

Routing choices are `balanced`, `quota`, `priority`, and `round-robin`. Model
fallback is opt-in. Capacity errors otherwise retain the selected model/account.
Keep-awake prevents idle system sleep only while a managed task is active; it does
not keep the display on. Project preferences may live in `.relay.toml` under
`[settings]` or `[profiles.NAME]`. Legacy `.codex-accounts.toml` files still work.
Relay recovery profiles go before `--`; native Codex profiles go after it.

## Diagnostics and cleanup

```sh
relay doctor --self-test
relay doctor --backup
relay doctor --restore-backup
relay diagnostics relay-diagnostics.json
relay clean
relay clean --older-than 30
relay clean --apply
relay events --limit 30
relay usage
relay runtime list
relay runtime preflight /path/to/codex
```

Diagnostics export allowlisted versions, event types, and counts without account
emails, task names, project paths, prompts, or credentials. Review other files
before sharing them; private state and connection files must stay private.

Cleanup is a dry run unless `--apply` is given. It retains live/restartable runtime
references and saved workspaces. `--jobs` also allows removal of old stopped-job
records. Stop supervised jobs before applying cleanup. Worktrees and account
logins are retained. Metadata restore also requires stopped tasks and preserves a
copy of the replaced database. Usage forecasts are local estimates.
