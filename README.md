# Context Bar for Herdr

A lightweight, dependency-free [Herdr](https://herdr.dev) plugin that displays color-coded context-window usage for Codex, Claude Code, Pi, and Hermes Agent directly in Herdr's Agents sidebar.

## Features

- Glanceable context-window usage without percentages or dashboards
- Green, yellow, and red bars that make high token usage easy to spot
- Responsive bar widths that follow the expanded Agents sidebar
- Local-only operation with no accounts, provider API calls, or network requests
- Support for Codex, Claude Code, Pi, and Hermes Agent

![Herdr Agents sidebar showing green, yellow, and red context-window usage bars](assets/context-bar.png)

Context Bar adds a thin progress line beneath every supported Agents sidebar entry:

- green below 50%
- yellow from 50% through 79%
- red at 80% and above

| Green: below 50% | Yellow: 50–79% | Red: 80% and above |
| --- | --- | --- |
| ![Green context bar at 25%](assets/context-bar-green.png) | ![Yellow context bar at 65%](assets/context-bar-yellow.png) | ![Red context bar at 95%](assets/context-bar-red.png) |

That is the whole feature. There are no notifications, provider quotas, or extra dashboards.

## Requirements

- Herdr 0.8.0 or newer
- Python 3.10 or newer
- macOS or Linux

## Installation

```sh
herdr plugin install pdalinis/herdr-ctx-bar
```

For local development:

```sh
herdr plugin link .
```

Install Herdr's agent integrations so panes are matched to the correct session instead of guessed by working directory:

```sh
herdr integration install codex
herdr integration install claude
herdr integration install pi
herdr integration install hermes
```

Configure the sidebar automatically:

```sh
herdr plugin action invoke herdr-ctx-bar.setup
herdr plugin log list --plugin herdr-ctx-bar
```

`setup` handles the safe common case automatically: it creates a timestamped backup, adds the Context Bar row when no custom Agent layout exists, validates the result, reloads Herdr, and starts the watcher. It is idempotent and can be run again after an upgrade.

If `[ui.sidebar.agents]` already contains a customized layout, `setup` leaves it untouched and prints the following section for you to merge. Add the final `$ctx_*` row to your existing `rows` array rather than declaring the table twice:

```toml
[ui.sidebar.agents]
row_gap = 0
rows = [
  ["state_icon", "machine", "workspace", "tab"],
  ["agent"],
  [
    { token = "$ctx_green", fg = "#3fb950" },
    { token = "$ctx_yellow", fg = "#d29922" },
    { token = "$ctx_red", fg = "#f85149" },
  ],
]
```

Herdr plugin v1 does not provide a declarative way for plugins to contribute sidebar rows. The explicit `setup` action is therefore required once after installation; Context Bar never edits UI configuration merely because it was installed or Herdr started.

## How it works

Context Bar runs one dependency-free Python watcher per Herdr server. It subscribes to pane lifecycle and agent-state events, reads local session records and configuration to calculate usage, and reports one colored metadata token per pane. It does not contact provider APIs, use credentials, or transmit session data.

The bar width follows the expanded sidebar. The watcher reads the live sidebar width from `pane.layout`, then redraws after divider changes. The heavy used segment (`━`) and thin remaining segment (`─`) form a narrow line inside one terminal row; Herdr plugin v1 cannot draw a literal native three-pixel element.

Data sources:

- Codex: the latest `last_token_usage` and `model_context_window` in its rollout JSONL.
- Claude Code: the latest non-sidechain assistant usage in its transcript, respecting compaction boundaries.
- Pi: the latest assistant context snapshot and Pi's local model catalog.
- Hermes: active message token counts in `state.db`, respecting compaction; messages without stored counts use Hermes' conventional characters-per-token estimate. The context window comes from Hermes configuration when present and otherwise follows Hermes' own 256K fallback.

If a session or context-window size cannot be identified safely, its bar is hidden rather than showing a misleading value.

## Usage and commands

```sh
herdr plugin action invoke herdr-ctx-bar.refresh
herdr plugin action invoke herdr-ctx-bar.status
herdr plugin action invoke herdr-ctx-bar.restart
```

Plugin command output is available through:

```sh
herdr plugin log list --plugin herdr-ctx-bar
```

## Troubleshooting and support

If a bar does not appear, first confirm that the matching Herdr agent integration is installed, then run:

```sh
herdr plugin action invoke herdr-ctx-bar.status
herdr plugin action invoke herdr-ctx-bar.refresh
herdr plugin log list --plugin herdr-ctx-bar
```

The bar is intentionally hidden when Context Bar cannot safely identify a session or its context-window size. For bugs, compatibility problems, or feature requests, [open a GitHub issue](https://github.com/pdalinis/herdr-ctx-bar/issues).

## Development

### Screenshot previews

You do not need to consume a real agent context window to test the bar. Focus the
pane you want to preview, then invoke one of the temporary demo states:

```sh
herdr plugin action invoke herdr-ctx-bar.demo-green
herdr plugin action invoke herdr-ctx-bar.demo-yellow
herdr plugin action invoke herdr-ctx-bar.demo-red
herdr plugin action invoke herdr-ctx-bar.demo-clear
```

The presets display 25%, 65%, and 95% usage. A preview affects only the focused
pane, expires automatically after five minutes, and never modifies the agent's
session or context. `demo-clear` restores live measurement immediately. Because
the CLI uses Herdr's currently focused pane, focus the intended agent before
running each command.

After changing the manifest, relink before using these actions:

```sh
herdr plugin link .
```

### Checks

```sh
python3 -m unittest discover -s tests -v
python3 -m py_compile bin/ctx_bar.py
herdr plugin link .
```

Manifest changes require relinking the plugin. Python changes are picked up after `herdr-ctx-bar.restart`.

## License

[MIT](LICENSE)
