# AGENTS.md

Guidance for AI coding agents working in this repo. Read this before making changes.

## What this project is

A tmux plugin that automatically renames tmux windows. Three backends:

- **`local`** — any OpenAI-compatible HTTP API (llama.cpp, Ollama, vLLM). Fast, free, private.
- **`claude`** — shells out to the Claude Code CLI (`claude -p`). No API key needed but slower (~3-4s).
- **`plugin`** — classic CWD/program-based rename. Fork of [ofirgall/tmux-window-name](https://github.com/ofirgall/tmux-window-name), no LLM involved.

The LLM modes generate kebab-case titles (e.g. `billing-flag-cleanup`) derived mainly from git branch names visible in shell prompts and neovim statuslines.

## Repository layout

```
.
├── tmux_ai_window_name.tmux      # TPM entry point — wires tmux hooks based on @ai_window_name_mode
├── scripts/
│   ├── ai_window_name.py         # LLM-based rename (local / claude modes)
│   ├── rename_session_windows.py # Classic rename (plugin mode)
│   └── path_utils.py             # Path disambiguation helpers (shared with upstream)
├── tests/
│   ├── test_icons.py
│   ├── test_get_uncommon_path.py
│   └── test_exclusive_paths.py
├── README.md                     # User-facing docs — KEEP IN SYNC (see below)
└── AGENTS.md                     # This file
```

## Key concepts in `ai_window_name.py`

- **Metadata-based cache.** The cache key is a hash of `pane_current_command + pane_current_path + git_branch` per pane, NOT the pane's visible text. Terminal redraws and log spam don't trigger re-queries; switching branches or starting a new program does.
- **File-locked cache.** `/tmp/tmux-ai-window-names.json` under `/tmp/tmux-ai-window-names.lock`. Rapid window switches can spawn overlapping script invocations — the lock prevents them from clobbering each other's cache entries.
- **Hook passes window_id explicitly.** `after-select-window` fires with `#{window_id}` as an argv so the background script renames the window the user *left*, not whatever is current when the script happens to run.
- **Resolution order on cache miss**, in `main()`:
  1. Plain-shell fast path (`try_plain_shell_title`) — all panes are shells → directory basename.
  2. ssh fast path (`try_ssh_title`) — any pane running `ssh` → `ssh:<hostname>` parsed from argv.
  3. LLM call → `apply_prefix` wraps with any detected prefix app (nvim, claude, etc.).
- **Prefix detection walks the process tree.** Wrappers like `claude` exec into a versioned sub-binary, so `pane_current_command` reports the version string. `detect_prefix` falls back to scanning `ps -A` descendants of `pane_pid` for a known comm name.
- **SSL verification is configurable.** `@ai_window_name_local_ssl_verify` accepts `true` (default, system CA bundle), `false` (skip verification), or a file path to a custom CA PEM bundle. The SSL context is built per-request in `generate_title_local`.
- **User options** live under the `@ai_window_name_` prefix in tmux. `get_option()` in `ai_window_name.py` reads them lazily.

## Key concepts in `rename_session_windows.py` (plugin mode)

- Requires `libtmux` (optional dependency — the `.tmux` entry point checks).
- Icon style options (`name`, `icon`, `name_and_icon`) with nerd-font defaults in `DEFAULT_PROGRAM_ICONS`.
- Integrates with tmux-resurrect via the `@resurrect-hook-*` options.

## Tests

```sh
cd /opt/ndomino/tmux-ai-window-name
python3 -m pytest tests/
```

Tests currently cover `rename_session_windows.py` (plugin mode). `ai_window_name.py` has no formal tests — exercise it manually or with ad-hoc `python3 -c` one-liners (see the `parse_ssh_host` sanity check used during ssh support development for a good pattern).

## Ground rules when making changes

### ALWAYS update docs (README + AGENTS.md) when adding or changing behavior

The README is the only thing users read. AGENTS.md is the only thing AI agents read. Both must stay in sync with the code. If you:

- add a new `@ai_window_name_*` option → document it under the right section
- change a default value → update the `(default: …)` note in both the README and the code comment
- add a new detection path or title-generation mode → describe it in the "How it works" section
- add a new keybinding → add it to the Keybindings table
- add a new prefix app to `DEFAULT_PREFIX_APPS` → update the example default in the `@ai_window_name_prefix_apps` section
- add a new dependency → note it under "Dependencies"
- change internal behavior (caching, SSL, process detection, etc.) → update the "Key concepts" section in AGENTS.md

No exceptions. A feature that isn't in the README effectively doesn't exist — users won't discover it, and future contributors will assume it's internal and rip it out. Likewise, internal behavior not described in AGENTS.md will be misunderstood or broken by AI agents.

### Other conventions

- **Keep the hook-fire path cheap.** Everything in `after-select-window` runs in the background (`run-shell -b`) but still adds up. Cache hits must stay LLM-free.
- **Don't leak tmux state.** Always read user options through `get_option()`. Never `subprocess.check_output(['tmux', 'show-option', ...])` directly — the helper handles missing options and the `@ai_window_name_` prefix consistently.
- **Prefer short-circuit paths to LLM calls.** If you can deterministically produce a good title (like the ssh fast path), do that instead of asking the model. The LLM is the last resort.
- **Match upstream style in `rename_session_windows.py`.** That file is close to the ofirgall original — keep diffs minimal so upstream changes are easy to merge.
- **Don't add emojis to source files or docs** unless explicitly asked.
