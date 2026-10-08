# Spawn agents

Open several panes of one coding agent from a single prompt.

Press the key bound to `local.agent-spawn.open`. A small popup opens, in the same family as Herdr's keybind panel (`prefix+?`): a text field, a filtered list, Tokyo Night selection. Type an agent and a count.

```text
claude 3
claud 4
codex 2
agy
```

Enter opens that many panes in a new tab, in the focused pane's directory. The count defaults to 1 and stops at 8. A close miss such as `claud` still selects Claude. Esc closes the popup.

The tab is tiled: two panes sit side by side, three use a tall pair plus one, four make a grid. Each pane gets a Herdr agent name like `claude-1`. If the agent stops on a trust prompt, the pane stays open so you can answer it.

Requires Herdr 0.9.0 or newer and `python3` on `PATH`. Only agents whose command is on `PATH` appear. Herdr kinds include `claude`, `codex`, `grok`, `agy`, and `gemini`.

## Install

```bash
git clone https://github.com/varvand/herdr-plugin-agent-spawn.git ~/.config/herdr/plugins/agent-spawn
herdr plugin link ~/.config/herdr/plugins/agent-spawn --enabled
```

```toml
[[keys.command]]
key = "prefix+a"
type = "plugin_action"
command = "local.agent-spawn.open"
description = "spawn agents"
```

`prefix+a` is unbound in Herdr's default keymap. Reload with `herdr server reload-config`.
