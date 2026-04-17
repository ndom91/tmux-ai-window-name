#!/usr/bin/env bash

CURRENT_DIR="$( cd "$( dirname "${BASH_SOURCE[0]}" )" && pwd )"

# ── Read configuration ──────────────────────────────────────────────
mode=$(tmux show-option -gv @ai_window_name_mode 2>/dev/null || echo "local")

# ── Mode: 'plugin' — classic CWD/program rename (requires libtmux) ─
if [ "$mode" = "plugin" ]; then
    LIBTMUX_AVAILABLE=$(python3 -c "import importlib.util; print(importlib.util.find_spec('libtmux') is not None)" 2>/dev/null)
    if [ "$LIBTMUX_AVAILABLE" = "False" ]; then
        tmux display "ERROR: tmux-ai-window-name 'plugin' mode requires Python libtmux (pip install libtmux)"
        exit 0
    fi

    tmux set -g automatic-rename on

    # Always enable plugin renaming for new windows (sesh-compatible)
    tmux set-hook -g 'after-new-window[8921]' 'set -w @ai_window_name_enabled 1 ; set -w automatic-rename off'
    tmux set-hook -g 'after-select-window[8921]' "run-shell -b '$CURRENT_DIR/scripts/rename_session_windows.py'"

    "$CURRENT_DIR"/scripts/rename_session_windows.py --enable_rename_hook
    "$CURRENT_DIR"/scripts/rename_session_windows.py --init_windows

    # tmux-resurrect integration
    tmux set -g @resurrect-hook-pre-restore-all "$CURRENT_DIR/scripts/rename_session_windows.py --disable_rename_hook"
    tmux set -g @resurrect-hook-post-restore-all "$CURRENT_DIR/scripts/rename_session_windows.py --post_restore"

    exit 0
fi

# ── Mode: 'local' or 'claude' — LLM-based rename ───────────────────

# Prevent tmux's built-in rename from competing
tmux set -g automatic-rename on
tmux set-hook -g 'after-new-window[8921]' 'set -w automatic-rename off'

# Run the LLM rename script on window switch and session switch (background, non-blocking).
# Pass #{window_id} explicitly so rapid switches don't cause the script to rename
# the wrong window (the "current" window can change between hook-fire and script run).
tmux set-hook -g 'after-select-window[8921]' "run-shell -b '$CURRENT_DIR/scripts/ai_window_name.py #{window_id}'"
tmux set-hook -g 'client-session-changed[8921]' "run-shell -b '$CURRENT_DIR/scripts/ai_window_name.py #{window_id}'"

# prefix+R: force a fresh LLM query for the current window (bypasses cache).
refresh_key=$(tmux show-option -gv @ai_window_name_refresh_key 2>/dev/null || echo "R")
tmux bind-key "$refresh_key" run-shell -b "$CURRENT_DIR/scripts/ai_window_name.py --force #{window_id}"
