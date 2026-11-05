<p align="center"><img src="https://raw.githubusercontent.com/blackspider-ops/relay/main/assets/relay.svg" alt="Relay — keep Codex moving" width="840"></p>

Relay keeps your Codex sessions moving between your own accounts. When an account
runs out, it finds another available account. When all accounts are waiting for a
reset, it waits too, then continues the saved task.

**macOS · Native Codex terminal · One account or many**

## Get started

You'll need [Codex](https://learn.chatgpt.com/docs/cli), Node.js 20 or newer, and
Python 3.11 or newer. If you use Homebrew, `brew install node python@3.13` installs
Node and Python.

```sh
npm install -g relay-codex
relay setup --count 2
relay run
```

Choose however many accounts you have. Each person signs into their own accounts;
this project does not include anyone's saved logins. Existing logins from the
earlier `codex-accounts` tool are picked up automatically.

The first run downloads Relay's Python libraries. Later runs use the saved copy.
Your usual `codex` command keeps working.

Prefer trying it without a global install?

```sh
npx --yes relay-codex setup --count 2
npx --yes relay-codex run
```

## Use it like Codex

```sh
relay run -- resume --yolo
relay run -- -C ~/projects/website resume
relay run -- exec --json "Review this project"
relay status --watch
relay menubar
```

`--yolo` keeps its usual full-access meaning. Without it, Relay keeps the task's
permissions. Resume starts with sessions from your current folder, just like
`codex resume`. Add `--all` to browse every folder, or `-C PATH` to choose a folder.
Different terminals can work on different conversations at once.

Put native Codex commands and options after `relay run --`. Use `relay run --help`
for the full Codex command and flag list, or `relay help` for Relay management.
Profiles, extra writable directories, Git worktrees, and noninteractive tasks can
use the account pool. `relay explain -- <arguments>` shows how a launch will run.
Native utilities, external/local-provider endpoints, and a few native-only setup
flags run directly; `--require-recovery` rejects a launch outside Relay recovery.

If a conversation is already open in another app, close it there before resuming
it here, or use Codex's `fork` command for a separate copy. To move a Relay task
between terminals:

```sh
relay attach --takeover SESSION_ID
```

Accounts show their saved email addresses. The watch view updates only when local
status changes; it uses no model allowance. The menu bar uses an icon and opens
at Mac login. `relay menubar --no-startup --configure-only` disables login startup.
Use a session's menu to switch accounts after its work finishes. Account menus can
enable or disable future routing without stopping current tasks. Actions show
their result, and stopped workers offer a restart.

For tab completion in Zsh, run `source <(relay completion zsh)` after `compinit`.
Bash users can run `source <(relay completion bash)`. Add the same line to your
shell's startup file to keep it enabled. Relay completes its own commands and
saved account/session names; arguments after `relay run --` use Codex's completion.

## What happens when something stops?

- **Account limit:** use an available account, or wait for the earliest usable reset.
- **One account:** wait for its reset and continue the same conversation.
- **Connection or sign-in failure:** retry at 5, 10, and 15 minutes, then try another account.
- **“Selected model is at capacity”:** wait five minutes and try the same model again.
  Relay keeps waiting rather than cycling logins. A different model is used only
  if you explicitly configure a fallback.
- **An unclear interruption during a command:** pause for review before continuing.
  Relay cannot guarantee that an external action happened exactly once.

## Leave work running

```sh
relay start --name website --cwd ~/projects/website --prompt "Finish the remaining checks"
relay attach SESSION_ID
relay stop SESSION_ID
```

Background tasks keep running after their terminal closes. Use `relay status --all`
to find saved sessions and `relay restart SESSION_ID` to recover a stopped one.
Opt into task recovery at Mac login with `relay startup enable SESSION_ID`.
`relay workspace save daily` and `relay workspace restore daily` retain the same
conversations across several terminals.

## A few useful commands

```sh
relay add personal
relay account disable personal
relay pause SESSION_ID
relay unpause SESSION_ID
relay retry SESSION_ID
relay doctor --self-test
relay menubar
```

Accounts stay in your Mac's Keychain. Relay keeps its settings and task records
in your private Application Support folder. Never share that folder.

## Updates and limits

Relay checks the installed Codex version and keeps a working copy for running
sessions. Future Codex changes may still need a Relay update.
Update Relay with `npm install -g relay-codex@latest`.

This is an independent tool, not an OpenAI product. Short local tests cover
parallel tasks and recovery; they are not an overnight reliability guarantee.

[Commands](docs/commands.md) · [Troubleshooting](docs/troubleshooting.md) ·
[Publishing to npm or Homebrew](docs/publishing.md) · [Contributing](CONTRIBUTING.md)
