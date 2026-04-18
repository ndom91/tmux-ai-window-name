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
  - Metadata change is the only invalidation signal; unchanged metadata
    means the cached title stays valid.
"""

import subprocess
import hashlib
import json
import os
import sys
import tempfile
import ssl
import urllib.request
import urllib.error
import fcntl
from contextlib import contextmanager

OPTIONS_PREFIX = '@ai_window_name_'
CACHE_FILE = os.path.join(tempfile.gettempdir(), 'tmux-ai-window-names.json')
LOCK_FILE = os.path.join(tempfile.gettempdir(), 'tmux-ai-window-names.lock')
DEBUG_LOG = os.path.join(tempfile.gettempdir(), 'tmux-ai-window-names.log')
MAX_LINES_PER_PANE = 40

# ── Defaults (overridable via tmux options) ──────────────────────────

DEFAULT_LOCAL_URL = 'http://localhost:8080/v1/chat/completions'
DEFAULT_LOCAL_MODEL = 'default'
DEFAULT_CLAUDE_MODEL = 'haiku'
DEFAULT_MAX_TOKENS = 30

# Apps that get prefixed to the title when detected running in a pane.
# Format: {pane_current_command: display_prefix}
DEFAULT_PREFIX_APPS = {
    'nvim': 'nvim',
    'vim': 'vim',
    'claude': 'claude',
    'opencode': 'opencode',
    'ssh': 'ssh',
}

# ssh option flags that take a separate argument — used when parsing the
# remote destination out of a full `ssh …` command line.
SSH_ARG_FLAGS = {
    '-b', '-B', '-c', '-D', '-E', '-e', '-F', '-I', '-i', '-J', '-L',
    '-l', '-m', '-O', '-o', '-p', '-Q', '-R', '-S', '-W', '-w',
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
    """Check if any pane (or its descendants) is running a prefix-worthy app.

    Some tools (claude, opencode) install as a wrapper that exec's into a
    versioned sub-binary at ~/.local/share/<app>/<version>/<bin>. When that
    happens, tmux's pane_current_command shows the version (e.g. "2.1.112")
    rather than the friendly name. To stay robust across upgrades, we fall
    back to scanning the pane's process descendants for a known name.
    """
    pane_pids = []
    for line in pane_meta.split('\n'):
        fields = line.split('\t')
        command = fields[1] if len(fields) > 1 else ''
        if command in prefix_apps:
            return prefix_apps[command]
        if len(fields) > 3:
            try:
                pane_pids.append(int(fields[3]))
            except ValueError:
                pass

    if not pane_pids:
        return ''

    # Build a parent->children map of all processes once, then DFS each pane.
    try:
        out = subprocess.check_output(
            ['ps', '-A', '-o', 'pid=,ppid=,comm='],
            stderr=subprocess.DEVNULL,
        ).decode()
    except (subprocess.CalledProcessError, FileNotFoundError):
        return ''

    children = {}
    comms = {}
    for line in out.splitlines():
        parts = line.split(None, 2)
        if len(parts) < 3:
            continue
        try:
            pid, ppid = int(parts[0]), int(parts[1])
        except ValueError:
            continue
        # comm may include a path (BSD ps does this for some procs); take basename
        comms[pid] = parts[2].rsplit('/', 1)[-1]
        children.setdefault(ppid, []).append(pid)

    for root in pane_pids:
        stack = list(children.get(root, []))
        while stack:
            pid = stack.pop()
            comm = comms.get(pid, '')
            if comm in prefix_apps:
                return prefix_apps[comm]
            stack.extend(children.get(pid, []))
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


def parse_ssh_host(args):
    """Extract the remote destination from an `ssh …` command line.

    Skips flag options (with or without their argument) and returns the
    first positional token, stripped of any `user@` prefix. Returns ''
    if no destination is present (e.g. bare `ssh` with no args).
    """
    parts = args.split()
    i = 1  # skip the ssh binary itself
    while i < len(parts):
        tok = parts[i]
        if tok in SSH_ARG_FLAGS:
            i += 2
            continue
        if tok.startswith('-'):
            i += 1
            continue
        return tok.split('@', 1)[-1]
    return ''


def find_ssh_host(pane_pid):
    """Find an ssh process descended from pane_pid and parse its hostname.

    pane_current_command reports 'ssh' but pane_pid is the parent shell;
    the ssh process itself is a descendant, so we walk the process tree
    to get its argv.
    """
    try:
        out = subprocess.check_output(
            ['ps', '-A', '-o', 'pid=,ppid=,comm=,args='],
            stderr=subprocess.DEVNULL,
        ).decode()
    except (subprocess.CalledProcessError, FileNotFoundError):
        return ''

    children = {}
    procs = {}
    for line in out.splitlines():
        fields = line.split(None, 3)
        if len(fields) < 4:
            continue
        try:
            pid, ppid = int(fields[0]), int(fields[1])
        except ValueError:
            continue
        comm = fields[2].rsplit('/', 1)[-1]
        procs[pid] = (comm, fields[3])
        children.setdefault(ppid, []).append(pid)

    stack = list(children.get(pane_pid, []))
    while stack:
        pid = stack.pop()
        comm, args = procs.get(pid, ('', ''))
        if comm == 'ssh':
            return parse_ssh_host(args)
        stack.extend(children.get(pid, []))
    return ''


def try_ssh_title(pane_meta, prefix_apps):
    """If any pane is running ssh, return 'ssh:hostname' as the full title.

    ssh sessions get deterministic, hostname-based names — the LLM has
    nothing useful to add and would just invent something. Falls back to
    bare 'ssh' if the destination can't be parsed.
    """
    if 'ssh' not in prefix_apps:
        return None

    for line in pane_meta.split('\n'):
        fields = line.split('\t')
        command = fields[1] if len(fields) > 1 else ''
        if command != 'ssh' or len(fields) <= 3:
            continue
        try:
            pane_pid = int(fields[3])
        except ValueError:
            continue
        prefix = prefix_apps['ssh']
        host = find_ssh_host(pane_pid)
        return f'{prefix}:{host}' if host else prefix
    return None


# ── Pane capture ─────────────────────────────────────────────────────

def get_pane_metadata(window_id):
    return subprocess.check_output([
        'tmux', 'list-panes', '-t', window_id,
        '-F', '#{pane_id}\t#{pane_current_command}\t#{pane_current_path}\t#{pane_pid}'
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

@contextmanager
def cache_lock():
    """Exclusive lock across all script invocations — closes a read-modify-write
    race where rapid window switches spawn overlapping scripts that clobber
    each other's cache entries, causing re-queries on the next visit."""
    with open(LOCK_FILE, 'w') as f:
        fcntl.flock(f.fileno(), fcntl.LOCK_EX)
        yield


def load_cache():
    try:
        with open(CACHE_FILE) as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def save_cache(cache):
    with open(CACHE_FILE, 'w') as f:
        json.dump(cache, f)


def debug_log(msg):
    """Append a line to the debug log if @ai_window_name_debug is set."""
    if get_option('debug', '') not in ('1', 'true', 'yes', 'on'):
        return
    try:
        with open(DEBUG_LOG, 'a') as f:
            f.write(msg + '\n')
    except OSError:
        pass


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

    headers = {'Content-Type': 'application/json'}
    api_key = get_option('local_api_key', '')
    if api_key:
        headers['Authorization'] = f'Bearer {api_key}'
    ssl_verify = get_option('local_ssl_verify', 'true')
    ssl_ctx = None
    if ssl_verify.lower() in ('0', 'false', 'no', 'off'):
        ssl_ctx = ssl.create_default_context()
        ssl_ctx.check_hostname = False
        ssl_ctx.verify_mode = ssl.CERT_NONE
    elif ssl_verify.lower() not in ('1', 'true', 'yes', 'on', ''):
        ssl_ctx = ssl.create_default_context(cafile=ssl_verify)

    req = urllib.request.Request(url, data=payload, headers=headers)
    with urllib.request.urlopen(req, timeout=15, context=ssl_ctx) as resp:
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
    system_prompt = get_option('system_prompt', DEFAULT_SYSTEM_PROMPT)
    prefix_apps = get_prefix_apps()

    # Prefer window_id passed in by the hook; fall back to "current window" for
    # manual invocations. The hook-supplied value avoids a race where the user
    # switches windows between hook-fire and this script running in the background.
    args = [a for a in sys.argv[1:] if a]
    force = '--force' in args
    args = [a for a in args if a != '--force']
    if args and args[0].startswith('@'):
        window_id = args[0]
    else:
        window_id = subprocess.check_output([
            'tmux', 'display-message', '-p', '#{window_id}'
        ]).decode().strip()

    pane_meta = get_pane_metadata(window_id)
    h = metadata_hash(pane_meta)

    # Phase 1: quick cache check under the lock. If we hit, rename and exit
    # without doing any LLM work. --force skips the cache read entirely.
    with cache_lock():
        cache = load_cache()
        cached = cache.get(window_id)
        if not force and cached and cached.get('hash') == h:
            title = cached['title']
            debug_log(f'[{os.getpid()}] {window_id} HIT  hash={h} title={title!r}')
            subprocess.run(['tmux', 'set-window-option', '-t', window_id, 'automatic-rename', 'off'])
            subprocess.run(['tmux', 'rename-window', '-t', window_id, title])
            return
        prev_hash = cached.get('hash') if cached else None
        debug_log(
            f'[{os.getpid()}] {window_id} MISS hash={h} prev={prev_hash} '
            f'force={force} meta={pane_meta!r}'
        )

    # Phase 2: heavy work outside the lock so concurrent scripts for OTHER
    # windows don't block waiting for our LLM call to finish.
    plain_title = try_plain_shell_title(pane_meta)
    ssh_title = try_ssh_title(pane_meta, prefix_apps)
    if plain_title is not None:
        title = plain_title
    elif ssh_title is not None:
        title = ssh_title
    else:
        content = capture_window_content(window_id, pane_meta)
        try:
            title = generate_title(content, mode, system_prompt)
        except Exception as e:
            print(f'tmux-ai-window-name: {e}', file=sys.stderr)
            return

        title = apply_prefix(title, pane_meta, prefix_apps)

    # Phase 3: atomic update — re-read the cache under the lock so any entries
    # written by concurrent scripts (for other windows) aren't clobbered.
    with cache_lock():
        cache = load_cache()
        cache[window_id] = {'hash': h, 'title': title}
        save_cache(cache)

    subprocess.run(['tmux', 'set-window-option', '-t', window_id, 'automatic-rename', 'off'])
    subprocess.run(['tmux', 'rename-window', '-t', window_id, title])


if __name__ == '__main__':
    main()
