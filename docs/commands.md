# Commands

## Accounts

```sh
relay setup --count 1
relay setup --count 6
relay add personal
relay list
relay refresh
relay account rename account1 personal
relay account enable personal
relay account disable personal
relay account priority personal 10
relay account cap personal 2
relay account models personal YOUR_MODEL
relay account reauth personal
relay account remove personal
```

Renaming changes the label, so saved logins stay usable. A cap of zero means no
session cap. Removing an account keeps its login unless `--revoke` is requested.
Switch active sessions away from an account before signing it out or replacing it.

## Sessions

```sh
relay run --name website -- --model YOUR_MODEL
relay run -- resume --yolo
relay start --name website --cwd ~/projects/website --prompt "Run the checks"
relay status --watch
relay status --all
relay status --json
relay attach SESSION_ID
relay attach --takeover SESSION_ID
relay pause SESSION_ID
relay unpause SESSION_ID
relay retry SESSION_ID
relay switch SESSION_ID --account personal
relay stop SESSION_ID
relay restart SESSION_ID
```

`relay run --` accepts native Codex arguments. The resume picker defaults to the
current folder; `--all` shows every folder and `-C/--cd` chooses another working
folder. Relative image paths keep the terminal's original directory as their base.

Recovery manages interactive sessions, `resume`, and `fork`. Native utilities
(such as `exec`, `review`, `login`, and `mcp`) run directly, as do native profiles,
extra writable directories, worktrees, local providers, explicit remote servers,
and the hook-trust bypass. These modes keep native behavior and use your normal
Codex login, without Relay account recovery.

Session IDs can be shortened as long as the prefix is unique. `pause` pauses
automatic recovery; it lets a command already running finish. `retry` explicitly
overrides the retry timer. Switching waits for an idle task; it does not interrupt
a command or invent an approval.

## Preferences

```sh
relay config
relay config notifications true
relay config background_on_disconnect true
relay config retry_interval 300
relay config retry_attempts 3
relay config routing quota
relay config preferred_accounts '["account1","account3"]'
relay config fallback_models '["YOUR_CHOSEN_MODEL"]'
relay config retry_interval 60 --profile quick
relay run --profile quick
```

Available routing choices are `balanced`, `quota`, `priority`, and `round-robin`.
No fallback model is selected by default. A capacity error preserves the selected
model and account unless you have enabled a fallback. Setting `retry_attempts` to
zero disables automatic capacity retries.

Project preferences can live in `.relay.toml`, under `[settings]` or
`[profiles.NAME]`. Existing `.codex-accounts.toml` files are also supported. Native Codex
profiles still go after the separator: `relay run -- -p work`.

## Checks and extras

```sh
relay check
relay doctor --self-test --backup
relay doctor --restore-backup
relay events --limit 30
relay usage
relay runtime list
relay runtime preflight /path/to/candidate/codex
relay runtime use installed
relay menubar
relay group save daily ~/projects/website ~/projects/api
relay group start daily
```

Restore requires stopped Relay sessions and preserves a copy of the replaced
database. It does not change Keychain logins. Usage forecasts are estimates based
on local observations. Groups currently start separate tasks in saved folders;
they do not restore a whole previous workspace.
