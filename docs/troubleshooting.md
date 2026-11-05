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

The background task may still be running. Check `relay status` and reattach with
`relay attach SESSION_ID`. Automatic terminal reconnection across a full worker
replacement is not guaranteed.

**Something still seems wrong.**

Run `relay doctor --self-test`. Include the Relay and Codex versions when reporting
a bug. Do not post auth files, tokens, connection files, or private task prompts.

Long runs, physical sleep/wake cycles, real revoked logins, and future Codex
versions need broader live testing. The tests do not remove those limits.
