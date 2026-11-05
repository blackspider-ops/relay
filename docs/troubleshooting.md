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

**Why does npm ask for authentication?**

That verification protects package publishing. Installing and running the public
Relay package does not require npm sign-in. Maintainers can use the repository's
trusted-publishing workflow once npm has authorized that workflow.
