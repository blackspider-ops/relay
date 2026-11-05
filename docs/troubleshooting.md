# Troubleshooting

**“This conversation is open in another app.”**

Close or unload that conversation in its original app before resuming it. Or fork
it into a separate conversation. For a Relay-owned task, use
`relay attach --takeover SESSION_ID`.

**A session needs attention.**

Run `relay status`, then `relay events`. Once the cause is resolved, use
`relay restart SESSION_ID` if the worker stopped, or `relay retry SESSION_ID` if it
is still running and you have reviewed the interrupted work.

**Setup was interrupted.**

Run the command again. Relay retries unfinished dependency installation. If Python
is missing or too old, install it with `brew install python@3.13`.

**An account needs another sign-in.**

Move its live tasks to another account, then run `relay account reauth ACCOUNT`.
Other accounts can keep working.

**The terminal disconnected after a worker restart.**

Relay reconnects the terminal when a replacement worker publishes a recovered
connection. If recovery requires review or the project folder is unavailable,
check `relay status` and `relay events`, then reattach with `relay attach SESSION_ID`.

**Something still seems wrong.**

Run `relay doctor --self-test`. Include the Relay and Codex versions when reporting
a bug. Do not post auth files, tokens, connection files, or private task prompts.

Long runs, physical sleep/wake cycles, real revoked logins, and future Codex
versions need broader live testing. The tests do not remove those limits.

**The dashboard repeats or an email is missing.**

Check `relay --version` and update Relay. Watch now prints only meaningful changes
from local state, without using model allowance. Earlier enrolled accounts can be
identified with `relay account identify`; this reads saved login metadata locally.

**Switching accounts says the task is busy.**

Wait for its current work, tools and approvals to finish, then switch again.
Relay rechecks this after checking the target account, so work starting during
that check also prevents switching. A rejected menu action keeps its explanation
visible. Use Restart for a stopped worker rather than sending it live controls.

**A usage-limit panel remains open and Relay says “waiting for activity.”**

Update Relay if you are using a version older than 0.7.3. A missed tool-completion
notification could leave a finished tool marked as active. Relay now checks full
native history for the matching tool before continuing account recovery. It keeps
waiting when work is still running or completion cannot be verified; approvals
and paused recovery still need attention. Existing workers retain the code they
started with, so an update takes effect after that worker is relaunched.

**Tab completion is missing.**

Load `source <(relay completion zsh)` after `compinit`, or use
`source <(relay completion bash)`. Put native arguments after `relay run --`.
Native completion has the same choices and limitations as Codex's own generated
completion. Start a new shell after a Codex update to reload those choices.

**Why does npm ask for authentication?**

That verification protects package publishing. Installing and running the public
Relay package does not require npm sign-in. Maintainers can use the repository's
trusted-publishing workflow once npm has authorized that workflow.
