# Commands

## Help

```sh
relay help
relay help account
relay help workspace
relay run --help
relay run -- exec --help
relay help codex resume
relay completion zsh
relay completion bash
```

`relay run --help` shows the installed Codex command and flag list, followed by
Relay's launch options. Put native arguments after `relay run --`.

Enable tab completion with `source <(relay completion zsh)` after Zsh's `compinit`,
or `source <(relay completion bash)` in Bash. Put that line in `.zshrc` or `.bashrc`
to load it in new shells. Management choices come from Relay's parser and local
saved metadata, including account labels/emails, sessions, profiles and workspaces.
Native arguments use the installed Codex completion, loaded once per shell on
first use; start a new shell after updating Codex to load its new completion.
Completion does not read Keychain credentials, fetch allowance, or start a task.

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
relay account reset personal --yes
relay account remove personal
```

Accounts display their saved email addresses. `account identify` fills in emails
for earlier installations without another sign-in. Commands accept an account's
label, unique email, or chosen display name. Renaming keeps the login usable.
A session cap of zero means unlimited. Removing an account retains its login
unless you request `--revoke`.

The account menu shows **Usage resets available** and **Use available reset…**
when Codex reports an earned reset. Choose it and confirm to use one. If the count
is unknown, choose **Check allowance now** first. Reset availability is separate
from purchased workspace credits. Resets are never spent by automatic recovery.

The equivalent terminal command is `relay account reset ACCOUNT --yes`. If a
response is lost, use the same action again: Relay saves and reuses the pending
request rather than submitting a new redemption. The menu calls this **Check
previous reset…**. An optional `--request-id UUID` also identifies a specific
logical attempt. Codex chooses the available reset; Relay checks the returned
result and reads allowance again afterward.

A reset only releases quota blocks when refreshed allowance permits it. Paused
tasks, required reviews, and unrelated retry delays are retained. If the reset
was used but allowance refresh failed, check allowance again; do not spend
another reset to refresh the display.
This uses the [documented Codex reset interface](https://learn.chatgpt.com/docs/app-server#8-earned-rate-limit-resets-chatgpt).

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
Opening it reads a fresh local snapshot, and visible rows update in place.
Short-window and weekly allowance are labeled separately.
Accounts assigned to live Relay sessions have a checkmark and an “In use” label;
shared accounts also show their session count. To switch, open the Relay icon,
then your session, then **Switch account**, and choose the account email. The
current choice is marked **Current**. The menu tracks tasks started through Relay.
Each session menu can switch to another enabled account after work and approvals
finish. The worker checks allowance and session capacity before switching, then
checks again that the session is idle before changing its login. Rejected actions
show their reason. Pending menu actions cannot be submitted twice, and results
remain visible until dismissed or another action starts. Stopped workers offer
Restart; restarting retains any review hold.

Account menus can disable or enable future routing. Disabling an account does not
interrupt work already using it. The account chooser marks the current account
and disables accounts excluded by their saved routing/model settings.
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

Foreground jobs close after their launcher disappears and current activity is
idle. Explicit background tasks and takeovers remain running. An interrupted action
is retained for review rather than being silently retried.

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
