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

Update Relay if you are using a version older than 0.7.4. Background terminals
and missed tool notifications could leave a finished turn marked as active.
Relay checks native history and the current backend's status. Commands explicitly
listed by Codex as background terminals can keep running while accounts change
after the model turn ends. The usage panel does not need to be clicked. Relay keeps
waiting when work is still running or completion cannot be verified; approvals
and paused recovery still need attention. Existing workers retain the code they
started with, so an update takes effect after that worker is relaunched.

**Codex says “auth refresh request failed: code=-32000.”**

Relay retries authentication on its normal retry schedule, then tries another
available account. If Codex reports that a refresh token was revoked, sign in
again with `relay account reauth LABEL`. Switch any sessions using that account
first. A revoked login cannot be repaired by retrying the same token.

**An account shows allowance remaining but switching to it fails.**

Allowance and login health are separate. With Relay 0.7.6 or newer, a login that
Codex reports as expired or revoked is labelled “Sign-in required” in the menu
and dashboard. Its percentages are the last successful allowance reading. Copy
its sign-in command from the account menu or run `relay account reauth LABEL`.
Completing sign-in wakes waiting recovery without spending a usage reset.

**The menu displays a Python traceback.**

Update to Relay 0.7.6 or newer. Earlier command launches could load the main
module twice, causing expected management errors to bypass their normal handler.
The menu now shows a short failure message and authentication-specific guidance.

**A running Relay task is missing from the menu.**

Update to Relay 0.7.7 or newer and restart the menu with `relay menubar`. Live
worker metadata and recent heartbeats can restore a missing session registration
without stopping the task. An unavailable process-start probe no longer removes
a live registration. Positively identified dead or replaced processes are still
removed.

**Keychain keeps asking even after “Always Allow.”**

Relay 0.7.7 prefers each account's native Codex helper and shares valid access
tokens in memory across sessions. Normal credential requests no longer launch
`security` for every session. The direct Keychain reader remains a compatibility
fallback, with a longer approval window and a shared pause after access failures
to avoid repeated prompts. Keychain permissions are not changed.

A system prompt may still be required for a locked Keychain, a new login, or an
unsupported native credential export. Your Mac login password belongs only in
the macOS dialog. Completing permission approval is separate from signing into
ChatGPT again.

**Tab completion is missing.**

Load `source <(relay completion zsh)` after `compinit`, or use
`source <(relay completion bash)`. Put native arguments after `relay run --`.
Native completion has the same choices and limitations as Codex's own generated
completion. Start a new shell after a Codex update to reload those choices.

**Why does npm ask for authentication?**

That verification protects package publishing. Installing and running the public
Relay package does not require npm sign-in. Maintainers can use the repository's
trusted-publishing workflow once npm has authorized that workflow.
