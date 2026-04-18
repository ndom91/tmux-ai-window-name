# tmux-ai-window-name

Automatically rename tmux windows using an LLM. The plugin captures the visible content of all panes in a window, sends it to an LLM, and sets a concise kebab-case title describing what you're working on.

It prioritizes git branch names (from shell prompts and neovim statuslines) to generate context-aware titles like `billing-flag-cleanup` or `ms-teams-channels`. Detected apps (`nvim`, `claude`, `opencode`, …) are prefixed automatically: `nvim:billing-flag-cleanup`, `claude:rate-limit-fix`. Remote shells are named after the destination, e.g. `ssh:llama-server.puff.lan` — no LLM call needed.

Fork of [ofirgall/tmux-window-name](https://github.com/ofirgall/tmux-window-name) with LLM-powered naming and a classic fallback mode.

## Modes

| Mode | Speed | Cost | Description |
|------|-------|------|-------------|
| `local` | ~200ms* | Free | Any OpenAI-compatible API (llama.cpp, Ollama, vLLM, etc.) |
| `claude` | ~3-4s | Subscription | Claude CLI (`claude -p`) — no API key needed |
| `plugin` | Instant | Free | Classic CWD/program-based rename (original plugin behavior) |

\* First query per window state. Cache hits are <50ms (no LLM call).

## Install

### With [TPM](https://github.com/tmux-plugins/tpm)

```tmux
set -g @plugin 'ndom91/tmux-ai-window-name'
```

Then press `prefix + I` to install.

### Dependencies

- **Python 3.7+**
- **`local` mode**: A running OpenAI-compatible server (e.g. [llama.cpp server](https://github.com/ggml-org/llama.cpp/tree/master/examples/server))
- **`claude` mode**: [Claude Code CLI](https://docs.anthropic.com/en/docs/claude-code) installed and authenticated
- **`plugin` mode**: `pip install libtmux`

## Configuration

Add these to your `tmux.conf` **before** the TPM `run` line:

```tmux
# Required: set the mode
set -g @ai_window_name_mode 'local'  # 'local', 'claude', or 'plugin'
```

### Local mode options

```tmux
# Server URL (default: http://localhost:8080/v1/chat/completions)
set -g @ai_window_name_local_url 'http://my-server:8080/v1/chat/completions'

# Model name sent in the request (default: 'default')
set -g @ai_window_name_local_model 'gemma-4-26B-A4B-it'

# API key — sent as Bearer token in Authorization header (default: none)
set -g @ai_window_name_local_api_key 'sk-...'
```

### Claude mode options

```tmux
# Path to claude binary (default: ~/.local/bin/claude)
set -g @ai_window_name_claude_bin '/usr/local/bin/claude'

# Model (default: haiku)
set -g @ai_window_name_claude_model 'haiku'
```

### Shared options (LLM modes)

```tmux
# Max tokens for LLM response (default: 30)
set -g @ai_window_name_max_tokens '30'

# Custom system prompt (overrides the built-in prompt)
set -g @ai_window_name_system_prompt 'Generate a 2-word kebab-case title for this terminal.'

# Apps to prefix on the title when detected in a pane (default: 'nvim:nvim,vim:vim,claude:claude,opencode:opencode,ssh:ssh')
# Format: comma-separated 'command:prefix' pairs. Bare 'foo' is shorthand for 'foo:foo'.
# ssh gets special treatment: the pane's ssh argv is parsed and the title becomes 'ssh:<hostname>'
# (no LLM call). Remove 'ssh' from the list to disable.
set -g @ai_window_name_prefix_apps 'nvim,vim,claude,opencode,ssh,docker:docker'

# Key (under prefix) to force-refresh the current window's title (default: 'R')
set -g @ai_window_name_refresh_key 'R'

# Enable verbose logging to /tmp/tmux-ai-window-names.log for diagnostics (default: off)
set -g @ai_window_name_debug 'on'
```

### Plugin mode options

Plugin mode uses the same options as [ofirgall/tmux-window-name](https://github.com/ofirgall/tmux-window-name) but with the `@ai_window_name_` prefix:

```tmux
set -g @ai_window_name_shells "['bash', 'fish', 'sh', 'zsh']"
set -g @ai_window_name_dir_programs "['nvim', 'vim', 'vi', 'git']"
set -g @ai_window_name_max_name_len '20'
```

## Keybindings

| Key | Action |
|-----|--------|
| `prefix + R` | Force-refresh the current window's title (bypasses cache, queries the LLM fresh) |

Override the key with `@ai_window_name_refresh_key`.

## How it works

### LLM modes (`local` / `claude`)

1. On every window switch (`after-select-window`) and session change (`client-session-changed`), the script runs in the background. The hook passes the target `#{window_id}` directly so rapid switches don't cause the script to operate on the wrong window.
2. The script hashes pane **metadata** — `pane_current_command + pane_current_path + git branch` for each pane — not terminal content.
3. **Cache hit** (hash matches the cached entry for that window): apply the cached title instantly. No LLM call.
4. **Cache miss** — four sub-paths in order:
   - **Plain-shell fast path**: if every pane is just a shell (`bash`, `zsh`, `fish`, `sh`), use the directory basename as the title. No LLM call.
   - **ssh fast path**: if any pane's foreground is `ssh`, walk the pane's process tree to find the actual ssh process, parse its argv (handling flags like `-p`, `-i`, `-o` and `user@host` syntax), and use `ssh:<hostname>` as the title. No LLM call.
   - **Prefix detection**: scan each pane's `pane_current_command`. If it matches a known prefix app, remember it. If not, walk the pane's process tree and look for a known name there too — this handles wrapper binaries (e.g. `claude` exec'ing into `~/.local/share/claude/2.1.112/claude` where tmux reports the version as the command).
   - **LLM call**: capture the last 40 lines of each pane and ask the LLM for a title. Apply the prefix if detected.
5. The result is written back to the cache, atomically, under a file lock to prevent concurrent script invocations from clobbering each other.
6. The window is renamed.

This means:
- Terminal output changes (log lines, build progress) do **not** trigger re-queries
- Switching git branches **does** trigger a re-query
- Starting a different program (e.g. `zsh` → `nvim`) **does** trigger a re-query
- Switching back and forth between cached windows is instant
- Rapid-fire window switching doesn't cause a thundering herd of duplicate queries

### Plugin mode

Uses the original [tmux-window-name](https://github.com/ofirgall/tmux-window-name) logic:
- Shows the running program name or current directory
- Smart path disambiguation when multiple windows share a directory name
- Nerd font icon support

## Troubleshooting

### Wrong title applied to a window

Press `prefix + R` to force a fresh LLM query for that window. If it keeps coming back wrong, the issue is upstream (LLM choice or system prompt) — try tuning `@ai_window_name_system_prompt` or switching models.

### Way too many LLM requests

Enable debug logging:

```tmux
set -g @ai_window_name_debug 'on'
```

Reload tmux, switch around, then check the log:

```sh
tail -50 "$(python3 -c 'import tempfile,os;print(os.path.join(tempfile.gettempdir(),"tmux-ai-window-names.log"))')"
```

Each invocation logs `HIT` or `MISS` with the window_id, current hash, previous hash, and pane metadata. `MISS hash=X prev=X` means the lock or arg-passing fix would help — make sure you're on the latest plugin version. `MISS hash=X prev=Y` with different values means metadata legitimately changed (a new program, a `cd`, a branch switch). Disable debug logging when done — the log is append-only.

### Clearing the cache

The cache lives at `$TMPDIR/tmux-ai-window-names.json` (typically `/tmp/...` on Linux, `/var/folders/.../T/...` on macOS). It's safe to delete at any time — the next window switch will rebuild it.

```sh
rm "$(python3 -c 'import tempfile,os;print(os.path.join(tempfile.gettempdir(),"tmux-ai-window-names.json"))')"
```

## Credits

- [ofirgall/tmux-window-name](https://github.com/ofirgall/tmux-window-name) — original plugin and path utilities
