# Changes

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
