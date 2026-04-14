# tmux-ai-window-name

Automatically rename tmux windows using an LLM. The plugin captures the visible content of all panes in a window, sends it to an LLM, and sets a concise kebab-case title describing what you're working on.

It prioritizes git branch names (from shell prompts and neovim statuslines) to generate context-aware titles like `billing-flag-cleanup` or `ms-teams-channels`.

Fork of [ofirgall/tmux-window-name](https://github.com/ofirgall/tmux-window-name) with LLM-powered naming and a classic fallback mode.

## Modes

| Mode | Speed | Cost | Description |
|------|-------|------|-------------|
| `local` | ~200ms* | Free | Any OpenAI-compatible API (llama.cpp, Ollama, vLLM, etc.) |
| `claude` | ~3-4s | Subscription | Claude CLI (`claude -p`) — no API key needed |
| `plugin` | Instant | Free | Classic CWD/program-based rename (original plugin behavior) |

\* First query per window. Cache hits are <100ms.

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
```

### Claude mode options

```tmux
# Path to claude binary (default: ~/.local/bin/claude)
set -g @ai_window_name_claude_bin '/usr/local/bin/claude'

# Model (default: haiku)
set -g @ai_window_name_claude_model 'haiku'
```

### Shared options

```tmux
# Cache TTL in seconds — how long before re-querying the same window (default: 300)
set -g @ai_window_name_cache_ttl '600'

# Max tokens for LLM response (default: 30)
set -g @ai_window_name_max_tokens '30'

# Custom system prompt (overrides the built-in prompt)
set -g @ai_window_name_system_prompt 'Generate a 2-word kebab-case title for this terminal.'
```

### Plugin mode options

Plugin mode uses the same options as [ofirgall/tmux-window-name](https://github.com/ofirgall/tmux-window-name) but with the `@ai_window_name_` prefix:

```tmux
set -g @ai_window_name_shells "['bash', 'fish', 'sh', 'zsh']"
set -g @ai_window_name_dir_programs "['nvim', 'vim', 'vi', 'git']"
set -g @ai_window_name_max_name_len '20'
```

## How it works

### LLM modes (`local` / `claude`)

1. On every window switch (`after-select-window`), the script runs in the background
2. It hashes pane **metadata** (running command + directory + git branch) — not terminal content
3. If the hash matches the cache and the TTL hasn't expired, it applies the cached title instantly (<100ms)
4. On a cache miss, it captures the last 40 lines of each pane and queries the LLM
5. The LLM response is cached and the window is renamed

This means:
- Terminal output changes (log lines, build progress) do **not** trigger re-queries
- Switching git branches **does** trigger a re-query
- Starting a different program (e.g. `zsh` → `nvim`) **does** trigger a re-query
- Switching back and forth between cached windows is instant

### Plugin mode

Uses the original [tmux-window-name](https://github.com/ofirgall/tmux-window-name) logic:
- Shows the running program name or current directory
- Smart path disambiguation when multiple windows share a directory name
- Nerd font icon support

## Credits

- [ofirgall/tmux-window-name](https://github.com/ofirgall/tmux-window-name) — original plugin and path utilities
