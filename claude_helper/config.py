"""Paths and user settings.

Everything is computed at call time from $HOME and the XDG variables, so a test
(or a throwaway environment) only has to change the environment.

Settings live in $XDG_CONFIG_HOME/claude-helper/config.toml, all optional:

    main_profile = "main"                  # name of the ~/.claude profile
    claude_bin = "~/.local/bin/claude"     # the binary the official installer manages
    md_roots = ["~"]                       # where `md` looks for CLAUDE.md files
    browser = ""                           # empty: google-chrome, then chromium
"""
import os
import tomllib
from functools import cache
from pathlib import Path

DEFAULTS = {
    "main_profile": "main",
    "claude_bin": "~/.local/bin/claude",
    "md_roots": ["~"],
    "browser": "",
}

REPO = Path(__file__).resolve().parent.parent
ENTRY = REPO / "bin" / "claude-helper"


def _xdg(variable, fallback):
    return Path(os.environ.get(variable) or Path.home() / fallback)


def config_file():
    return _xdg("XDG_CONFIG_HOME", ".config") / "claude-helper" / "config.toml"


def state_dir():
    """Current profile, relaunch markers, switch log."""
    return _xdg("XDG_STATE_HOME", ".local/state") / "claude-helper"


def data_dir():
    """One browser user-data-dir per account."""
    return _xdg("XDG_DATA_HOME", ".local/share") / "claude-helper"


def main_dir():
    """The main profile: owns the shared config and the session database."""
    return Path.home() / ".claude"


@cache
def _load(path, _stamp):
    if not path.exists():
        return {}
    with path.open("rb") as f:
        values = tomllib.load(f)
    unknown = set(values) - set(DEFAULTS)
    if unknown:
        raise SystemExit(f"{path}: unknown setting(s): {', '.join(sorted(unknown))}")
    return values


def setting(key):
    path = config_file()
    stamp = path.stat().st_mtime_ns if path.exists() else None
    return _load(path, stamp).get(key, DEFAULTS[key])


def claude_bin():
    return Path(setting("claude_bin")).expanduser()


def md_roots():
    return [Path(r).expanduser() for r in setting("md_roots")]
