# Changes

## 0.7.1

- Enable reset only with weekly allowance fully exhausted (0%) and its reset more than 48 hours away.
- Keep reset disabled when weekly allowance or timing is unknown, with the reason shown in the menu.
- Recheck fresh allowance before redemption and pending retries; exactly 48 hours remains locked.
- Disable an open menu’s reset action at the time boundary without network polling.

## 0.7.0

- Show each account’s available earned usage resets and add an explicit confirmed redemption action.
- Reuse a durable request ID when checking an uncertain reset, including after a restart.
- Refresh allowance after a reset and wake eligible waiting tasks while retaining pause/review holds.
- Report no available credit, no eligible limit, unsupported runtimes, and uncertain responses distinctly.

## 0.6.1

- Mark accounts in use in the main menu and show how many live sessions share them.
- Label the current account explicitly in each session's switch menu.
- Explain when no Relay sessions are running instead of implying a global active account.

## 0.6.0

- Keep dependency setup logs off stdout so first-run completion and JSON remain usable.
- Add Bash and Zsh completion for management commands, saved identifiers, and native Codex arguments.
- Add account switching and routing controls to the menu, with visible pending and completed results.
- Offer restart for stopped workers and retain their displayed account identity.
- Reject manual switches if work starts during allowance/credential reads or while waiting for authentication.
- Serialize automatic continuation with authentication changes and return specific, allowlisted control rejections.

## 0.5.3

- Show short and weekly reset times separately from scheduled allowance checks.
- Replace an empty check schedule with “Allowance checked when needed”; ignore null timestamps.

## 0.5.2

- Update open menu rows in place and refresh the local snapshot when opening the menu.
- Label short and weekly allowance separately in account menu rows.
- Retire foreground workers when their launcher exits, retaining explicit background jobs and takeovers.
- Preserve a review hold when a terminal disappears during an active action.

## 0.5.1

- Apply an explicit reviewed retry even when a previous retry timer is still pending.
- Show account emails in session details and recovery messages as well as the account list.
- Normalize npm repository metadata for trusted publishing.

## 0.5.0

- Show saved account emails in the dashboard and menu bar.
- Update watched status only when local data changes.
- Add a native menu icon, session actions, and persistent Mac login startup.
- Enforce review holds, preserve launch permissions and settings, and reconnect replacement workers.
- Keep effective project folders synchronized across resume and restart.
- Replace recursive recovery with an iterative driver.
- Support managed native profiles, extra workspace roots, Git worktrees, and noninteractive tasks.
- Resolve saved sessions with native filters and names.
- Add full help, launch previews, confirmed controls, saved workspaces, diagnostics, and safe cleanup previews.
- Add optional background login recovery and keep-awake while tasks are active.
- Add a GitHub trusted-publishing workflow.

## 0.4.2

- Default the resume picker to the current project; honor explicit working directories.
- Preserve option values, attached short options, prompt separators, and relative image paths.
- Forward native utility commands and native-only launch settings without a remote connection.
- Support automatic approval review and private-server launches in managed sessions.
- Keep pooled authentication in memory even when native config overrides are supplied.

## 0.4.1

- Keep manual review required after a worker crash during an external action.
- Recognize capacity status codes as well as the displayed message.

## 0.4.0

- First public release as Relay.
- Added recovery for the selected-model capacity message.
- Preserved retry deadlines when unrelated settings change.
- Corrected settings restoration for workers with multiple conversations.
- Preserved pause state after worker replacement.
- Added visibility and restart support for stopped sessions.
- Added an npm launcher that preserves running sessions across package updates.
