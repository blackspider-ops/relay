"""Shell adapters and local completion. Never start Codex or read credentials here."""

import argparse
import json
from pathlib import Path
import sqlite3
import tomllib


def document(path):
    try:
        value = (
            tomllib.loads(path.read_text())
            if path.suffix == ".toml"
            else json.loads(path.read_text())
        )
        return value if isinstance(value, dict) else {}
    except (OSError, ValueError):
        return {}


def rows(state, query):
    path = state / "pool.sqlite"
    if not path.exists():
        return []
    try:
        db = sqlite3.connect(
            path.resolve().as_uri() + "?mode=ro", uri=True, timeout=0.15
        )
        try:
            return db.execute(query).fetchall()
        finally:
            db.close()
    except sqlite3.Error:
        return []


def values(action, context, chosen, state):
    from router import labels
    from commands import account_identity
    from settings import DEFAULTS

    if action.choices is not None and not isinstance(
        action, argparse._SubParsersAction
    ):
        return list(action.choices)
    name = action.dest
    if name in {"first", "account", "label"}:
        if name == "label" and context[-1:] == ["add"]:
            return []
        accounts = labels(state)
        aliases = [account_identity(state, label).get("email") for label in accounts]
        for label, raw in rows(state, "SELECT label,options FROM account_options"):
            if label in accounts:
                try:
                    aliases.append(json.loads(raw).get("name"))
                except ValueError:
                    pass
        # Do not offer an ambiguous email/display name.
        return accounts + [
            a for a in aliases if isinstance(a, str) and aliases.count(a) == 1
        ]
    if name in {"session", "target", "sessions"}:
        from pool import Pool

        live = [
            ident
            for ident, pid in rows(state, "SELECT id,pid FROM sessions")
            if Pool.alive(pid)
        ]
        if name == "target":
            return live + ["all"]
        saved = [
            p.name.removesuffix(".job.json")
            for p in (state / "services").glob("*.job.json")
        ]
        return live if name == "sessions" else live + saved
    if name == "profile":
        result = []
        for path in [
            state / "settings.json",
            Path.cwd() / ".relay.toml",
            Path.cwd() / ".codex-accounts.toml",
        ]:
            profiles = document(path).get("profiles", {})
            if isinstance(profiles, dict):
                result.extend(profiles)
        return result
    if name == "key" and context[:1] == ["config"]:
        return list(DEFAULTS)
    if name == "value" and context[:1] == ["config"]:
        key = chosen.get("key")
        if key == "routing":
            return ["balanced", "quota", "priority", "round-robin"]
        if isinstance(DEFAULTS.get(key), bool):
            return ["true", "false"]
    if (
        name == "name"
        and context[:1] in (["workspace"], ["group"])
        and context[-1:] != ["save"]
    ):
        return list(
            document(
                state
                / ("workspaces.json" if context[0] == "workspace" else "groups.json")
            )
        )
    if name == "topic":
        from router import parser

        sub = next(
            a for a in parser()._actions if isinstance(a, argparse._SubParsersAction)
        )
        return [k for k in sub.choices if not k.startswith("_")] + ["codex"]
    return []


def candidates(arguments, state):
    """Last word is the word being completed. @codex:N delegates after N words."""
    from router import parser

    arguments = list(arguments) or [""]
    prefix, previous = arguments[-1], arguments[:-1]
    current = parser()
    context, chosen = [], {}
    pending = None
    positional = 0
    options = True
    for index, word in enumerate(previous):
        if pending is not None:
            chosen[pending.dest] = word
            pending = None
            continue
        positionals = [a for a in current._actions if not a.option_strings]
        action = positionals[positional] if positional < len(positionals) else None
        if options and word == "--":
            if action is not None and action.nargs == argparse.REMAINDER:
                return [f"@codex:{index + 1}"]
            options = False
            continue
        if options and word.startswith("-"):
            flag, equal, value = word.partition("=")
            option = current._option_string_actions.get(flag)
            if option is None:
                return []
            if option.nargs != 0:
                if equal:
                    chosen[option.dest] = value
                else:
                    pending = option
            continue
        if isinstance(action, argparse._SubParsersAction):
            if word not in action.choices or word.startswith("_"):
                return []
            context.append(word)
            current = action.choices[word]
            positional = 0
        elif action is not None:
            if action.nargs == argparse.REMAINDER:
                return [f"@codex:{index}"]
            chosen[action.dest] = word
            if action.nargs not in {"*", "+"}:
                positional += 1
    if pending is not None:
        result = values(pending, context, chosen, state)
    elif options and prefix.startswith("-"):
        flag, equal, value = prefix.partition("=")
        action = current._option_string_actions.get(flag)
        if equal and action is not None and action.nargs != 0:
            return sorted(
                {
                    flag + "=" + item
                    for item in values(action, context, chosen, state)
                    if item.startswith(value) and "\n" not in item and "\r" not in item
                }
            )
        result = list(current._option_string_actions)
    else:
        positionals = [a for a in current._actions if not a.option_strings]
        action = positionals[positional] if positional < len(positionals) else None
        if isinstance(action, argparse._SubParsersAction):
            result = [k for k in action.choices if not k.startswith("_")]
        elif action is not None and action.nargs == argparse.REMAINDER:
            # Show wrapper flags until the user enters native arguments or --.
            result = list(current._option_string_actions)
            if not prefix:
                result.append("--")
            else:
                return [f"@codex:{len(previous)}"]
        else:
            result = values(action, context, chosen, state) if action else []
    return sorted(
        {
            str(item)
            for item in result
            if isinstance(item, str)
            and item.startswith(prefix)
            and "\n" not in item
            and "\r" not in item
        }
    )


def script(shell):
    if shell == "zsh":
        return r"""# Relay completion. Load after compinit: source <(relay completion zsh)
_relay() {
    local -a replies
    replies=("${(@f)$(command relay _complete -- "${words[@]:1:$((CURRENT - 1))}")}")
    if [[ ${replies[1]} == @codex:<-> ]]; then
        local skip=${replies[1]#@codex:}
        if (( ! $+functions[_codex] )); then
            local native
            native=$(command codex completion zsh 2>/dev/null) || { _files; return; }
            eval "$native"
        fi
        local -a words=(codex "${words[@]:$((skip + 1))}")
        local CURRENT=$((CURRENT - skip))
        _codex
        return
    fi
    [[ -n ${replies[1]} ]] && compadd -a replies || _files
}
compdef _relay relay"""
    if shell == "bash":
        return r"""# Relay completion: source <(relay completion bash)
_relay() {
    local item skip native cur="${COMP_WORDS[COMP_CWORD]}"
    local -a replies=()
    while IFS= read -r item; do
        [[ -n "$item" ]] && replies+=("$item")
    done < <(command relay _complete -- "${COMP_WORDS[@]:1:COMP_CWORD}")
    if [[ ${replies[0]} == @codex:* ]]; then
        skip=${replies[0]#@codex:}
        [[ "$skip" =~ ^[0-9]+$ ]] || return
        if ! declare -F _codex >/dev/null; then
            native=$(command codex completion bash 2>/dev/null) || return
            eval "$native"
        fi
        local -a COMP_WORDS=(codex "${COMP_WORDS[@]:$((skip + 1))}")
        local COMP_CWORD=$((COMP_CWORD - skip))
        _codex codex "$cur" "${COMP_WORDS[COMP_CWORD-1]}"
        return
    fi
    COMPREPLY=("${replies[@]}")
}
complete -o default -F _relay relay"""
    raise ValueError("Choose zsh or bash")
