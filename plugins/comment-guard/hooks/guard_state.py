#!/usr/bin/env python3
"""Where the guards keep their per-session memory, and how they reach git.

Two hooks fire on the same events, so they cannot share one state file without
racing each other over it. They share the directory, the pruning and the git
plumbing; the file each one writes is its own.
"""

import hashlib
import json
import os
import re
import subprocess
import time

STATE_DIR = os.environ.get("COMMENT_GUARD_STATE_DIR") or os.path.expanduser(
    "~/.claude/comment-guard/state"
)
STATE_TTL_DAYS = 7
MAX_FILE_BYTES = 200_000

# The directory can be pointed anywhere, so pruning touches only the files
# these hooks write: <kind>-<16 hex chars>.json. Nothing else in it, however
# old, is ever removed.
STATE_FILE = re.compile(r"^(?:comments|docs)-[0-9a-f]{16}\.json$")


def git(cwd, *args):
    try:
        done = subprocess.run(
            ["git", "-C", cwd, *args], capture_output=True, text=True, timeout=10
        )
        return done.stdout if done.returncode == 0 else ""
    except Exception:
        return ""


def toplevel(path):
    """The git work tree containing path, or "" when there is none."""
    start = path if os.path.isdir(path) else os.path.dirname(path)
    return git(start or ".", "rev-parse", "--show-toplevel").strip()


def read_text(path):
    try:
        if os.path.getsize(path) > MAX_FILE_BYTES:
            return ""
        with open(path, encoding="utf-8", errors="replace") as handle:
            return handle.read()
    except Exception:
        return ""


def state_file(session_id, kind):
    os.makedirs(STATE_DIR, exist_ok=True)
    tag = hashlib.sha1((session_id or "nosession").encode()).hexdigest()[:16]
    return os.path.join(STATE_DIR, f"{kind}-{tag}.json")


def prune_old_state():
    cutoff = time.time() - STATE_TTL_DAYS * 86400
    try:
        for name in os.listdir(STATE_DIR):
            path = os.path.join(STATE_DIR, name)
            if STATE_FILE.match(name) and os.path.getmtime(path) < cutoff:
                os.remove(path)
    except Exception:
        pass


def load_state(path, version):
    """What was stored for this session, or None when there is nothing usable."""
    try:
        with open(path) as handle:
            stored = json.load(handle)
        return stored if stored.get("version") == version else None
    except Exception:
        return None


def save_state(path, state):
    try:
        with open(path, "w") as handle:
            json.dump(state, handle)
    except Exception:
        pass
