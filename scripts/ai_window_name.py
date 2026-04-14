#!/usr/bin/env python3
"""Generate smart tmux window names using an LLM.

Captures visible text from all panes in the current tmux window,
sends it to a configurable LLM backend, and renames the window.

Backends (set via @ai_window_name_mode in tmux.conf):
  'local'  - Any OpenAI-compatible API (llama.cpp, Ollama, vLLM, etc.)
  'claude' - Claude CLI via subscription

Caching strategy:
  - Hash is based on pane METADATA (command + path + git branch),
    not full content. Terminal redraws/resizes don't trigger re-queries.
  - A TTL ensures we re-query periodically even if metadata is unchanged.
"""

import subprocess
import hashlib
import json
import os
import sys
import tempfile
import time
import urllib.request
import urllib.error

OPTIONS_PREFIX = '@ai_window_name_'
CACHE_FILE = os.path.join(tempfile.gettempdir(), 'tmux-ai-window-names.json')
MAX_LINES_PER_PANE = 40

# ── Defaults (overridable via tmux options) ──────────────────────────

DEFAULT_LOCAL_URL = 'http://localhost:8080/v1/chat/completions'
DEFAULT_LOCAL_MODEL = 'default'
DEFAULT_CLAUDE_MODEL = 'haiku'
DEFAULT_CACHE_TTL = 300
DEFAULT_MAX_TOKENS = 30

# Apps that get prefixed to the title when detected running in a pane.
# Format: {pane_current_command: display_prefix}
DEFAULT_PREFIX_APPS = {
    'nvim': 'nvim',
    'vim': 'vim',
}

SHELLS = {'bash', 'fish', 'sh', 'zsh'}

DEFAULT_SYSTEM_PROMPT = (
    'You are naming a tmux window based on its terminal content. '
    'Your goal: figure out WHAT the user is working on and produce a '
    '2-3 word kebab-case title.\n\n'
    'Priority for deciding the title:\n'
    '1. Git branch name — look in shell prompts and neovim statuslines. '
    'The branch name is the BEST signal for the task. Condense it to '
    '2-3 words capturing the intent (e.g. "add-ms-teams-shared-channel-support" '
    '→ "ms-teams-channels", "cleanup-data-importers-billing-feature-flags-2" '
    '→ "billing-flag-cleanup").\n'
    '2. If branch is just "main" or "master", fall back to the project name '
    'and what tool/command is running (e.g. "support-app-dev", "services-sst").\n'
    '3. If no branch or project is visible, describe what the panes are doing.\n\n'
    'Reply with ONLY the kebab-case title. No explanation, no quotes, no backticks.'
)


# ── tmux option helpers ──────────────────────────────────────────────

def get_option(name, default=''):
    """Read a tmux user option (@ai_window_name_*)."""
    try:
        out = subprocess.check_output(
            ['tmux', 'show-option', '-gv', f'{OPTIONS_PREFIX}{name}'],
            stderr=subprocess.DEVNULL,
        ).decode().strip()
        return out if out else default
    except subprocess.CalledProcessError:
        return default


def get_prefix_apps():
    """Load prefix apps from tmux option or use defaults.

    User can set: set -g @ai_window_name_prefix_apps 'nvim:nvim,vim:vim,docker:docker'
    Format: command:prefix pairs, comma-separated.
    """
    raw = get_option('prefix_apps', '')
    if not raw:
        return dict(DEFAULT_PREFIX_APPS)

    apps = {}
    for pair in raw.split(','):
        pair = pair.strip()
        if ':' in pair:
            cmd, prefix = pair.split(':', 1)
            apps[cmd.strip()] = prefix.strip()
        elif pair:
            apps[pair] = pair  # command is its own prefix
    return apps


def detect_prefix(pane_meta, prefix_apps):
    """Check if any pane is running a prefix-worthy app. Returns prefix or ''."""
    for line in pane_meta.split('\n'):
        fields = line.split('\t')
        command = fields[1] if len(fields) > 1 else ''
        if command in prefix_apps:
            return prefix_apps[command]
    return ''


def try_plain_shell_title(pane_meta):
    """If all panes are plain shells, return a title from the directory path.

    Returns a title string, or None if any pane is running a non-shell program.
    """
    paths = []
    for line in pane_meta.split('\n'):
        fields = line.split('\t')
        command = fields[1] if len(fields) > 1 else ''
        path = fields[2] if len(fields) > 2 else ''
        if command not in SHELLS:
            return None
        paths.append(path)

    if not paths:
        return None

    # Use the first pane's path as the title basis
    home = os.path.expanduser('~')
    path = paths[0]
    if path == home:
        return '~'
    return os.path.basename(path) or '~'


# ── Pane capture ─────────────────────────────────────────────────────

def get_pane_metadata(window_id):
    return subprocess.check_output([
        'tmux', 'list-panes', '-t', window_id,
        '-F', '#{pane_id}\t#{pane_current_command}\t#{pane_current_path}'
    ]).decode().strip()


def get_git_branch(path):
    try:
        return subprocess.check_output(
            ['git', '-C', path, 'rev-parse', '--abbrev-ref', 'HEAD'],
            stderr=subprocess.DEVNULL,
        ).decode().strip()
    except (subprocess.CalledProcessError, FileNotFoundError):
        return ''


def capture_window_content(window_id, pane_meta):
    parts = []
    for line in pane_meta.split('\n'):
        fields = line.split('\t')
        pane_id = fields[0]
        command = fields[1] if len(fields) > 1 else ''
        path = fields[2] if len(fields) > 2 else ''

        text = subprocess.check_output([
            'tmux', 'capture-pane', '-p', '-t', pane_id
        ]).decode().rstrip()

        lines = text.split('\n')[-MAX_LINES_PER_PANE:]
        trimmed = '\n'.join(lines)
        parts.append(f'[Pane: command={command} path={path}]\n{trimmed}')

    return '\n---\n'.join(parts)


def metadata_hash(pane_meta):
    """Hash pane commands, paths, AND git branches."""
    stable = []
    seen_paths = set()
    for line in pane_meta.split('\n'):
        fields = line.split('\t')
        command = fields[1] if len(fields) > 1 else ''
        path = fields[2] if len(fields) > 2 else ''
        branch = ''
        if path and path not in seen_paths:
            seen_paths.add(path)
            branch = get_git_branch(path)
        stable.append(f'{command}:{path}:{branch}')
    return hashlib.md5('\n'.join(stable).encode()).hexdigest()[:12]


# ── Cache ────────────────────────────────────────────────────────────

def load_cache():
    try:
        with open(CACHE_FILE) as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def save_cache(cache):
    with open(CACHE_FILE, 'w') as f:
        json.dump(cache, f)


# ── LLM backends ────────────────────────────────────────────────────

def generate_title_local(content, system_prompt):
    url = get_option('local_url', DEFAULT_LOCAL_URL)
    model = get_option('local_model', DEFAULT_LOCAL_MODEL)
    max_tokens = int(get_option('max_tokens', str(DEFAULT_MAX_TOKENS)))

    payload = json.dumps({
        'model': model,
        'messages': [
            {'role': 'system', 'content': system_prompt},
            {'role': 'user', 'content': content},
        ],
        'temperature': 0.3,
        'max_tokens': max_tokens,
    }).encode()

    req = urllib.request.Request(url, data=payload, headers={'Content-Type': 'application/json'})
    with urllib.request.urlopen(req, timeout=15) as resp:
        data = json.loads(resp.read())

    return data['choices'][0]['message']['content'].strip().strip('`"\' ')


def generate_title_claude(content, system_prompt):
    claude_bin = get_option('claude_bin', os.path.expanduser('~/.local/bin/claude'))
    model = get_option('claude_model', DEFAULT_CLAUDE_MODEL)

    result = subprocess.run(
        [
            claude_bin, '-p',
            '--model', model,
            '--no-session-persistence',
            '--tools', '',
            '--disable-slash-commands',
            '--strict-mcp-config',
            system_prompt,
        ],
        input=content,
        capture_output=True,
        text=True,
        timeout=30,
    )
    if result.returncode != 0:
        raise RuntimeError(f'claude CLI failed: {result.stderr}')

    return result.stdout.strip().strip('`"\' ')


def generate_title(content, mode, system_prompt):
    if mode == 'local':
        return generate_title_local(content, system_prompt)
    else:
        return generate_title_claude(content, system_prompt)


# ── Main ─────────────────────────────────────────────────────────────

def apply_prefix(title, pane_meta, prefix_apps):
    """Prefix title with detected app name (e.g. 'nvim:billing-cleanup')."""
    prefix = detect_prefix(pane_meta, prefix_apps)
    if prefix:
        # Strip prefix if the LLM already included it
        for sep in (':', '-'):
            tag = f'{prefix}{sep}'
            if title.startswith(tag):
                title = title[len(tag):]
                break
        return f'{prefix}:{title}'
    return title


def main():
    mode = get_option('mode', 'local')
    ttl = int(get_option('cache_ttl', str(DEFAULT_CACHE_TTL)))
    system_prompt = get_option('system_prompt', DEFAULT_SYSTEM_PROMPT)
    prefix_apps = get_prefix_apps()

    window_id = subprocess.check_output([
        'tmux', 'display-message', '-p', '#{window_id}'
    ]).decode().strip()

    pane_meta = get_pane_metadata(window_id)
    h = metadata_hash(pane_meta)
    now = time.time()

    # Check cache
    cache = load_cache()
    cached = cache.get(window_id)
    if cached and cached.get('hash') == h:
        age = now - cached.get('time', 0)
        if age < ttl:
            title = cached['title']
            subprocess.run(['tmux', 'set-window-option', '-t', window_id, 'automatic-rename', 'off'])
            subprocess.run(['tmux', 'rename-window', '-t', window_id, title])
            return

    # Cache miss or expired — check if we can skip the LLM entirely
    plain_title = try_plain_shell_title(pane_meta)
    if plain_title is not None:
        title = plain_title
    else:
        content = capture_window_content(window_id, pane_meta)
        try:
            title = generate_title(content, mode, system_prompt)
        except Exception as e:
            print(f'tmux-ai-window-name: {e}', file=sys.stderr)
            return

        title = apply_prefix(title, pane_meta, prefix_apps)

    cache[window_id] = {'hash': h, 'title': title, 'time': now}
    save_cache(cache)

    subprocess.run(['tmux', 'set-window-option', '-t', window_id, 'automatic-rename', 'off'])
    subprocess.run(['tmux', 'rename-window', '-t', window_id, title])


if __name__ == '__main__':
    main()
