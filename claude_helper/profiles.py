"""Profiles: several Claude Code accounts, one shared configuration.

A profile is a CLAUDE_CONFIG_DIR. The main profile is ~/.claude itself
(CLAUDE_CONFIG_DIR unset, like a plain Claude Code); every other profile is
~/.claude-<name>. Login, token and account belong to each profile; everything
else — instructions, settings, agents, hooks, skills, plugins and the session
database — is a symlink to the main profile, so /resume finds every
conversation whatever the account.

Claude Code writes settings through a symlink (`/config`, `/model`, permission
grants, `claude plugin …`). Its Edit/Write tools refuse a symlinked file, loudly
and without breaking it: an agent editing CLAUDE.md must target ~/.claude/.
Never shared: .credentials.json and .claude.json carry the account identity.
"""
import filecmp
import json
import os
import shutil
import time
from pathlib import Path

from . import config
from .term import die, green, grey, red

SHARED_DIRS = (
    "agents", "hooks", "skills", "skills-disabled", "commands", "output-styles", "rules",
    "routines", "plugins", "projects", "tasks", "file-history", "plans",
)
SHARED_FILES = ("CLAUDE.md", "settings.json", "settings.local.json")
# Copied by `init` from the main profile, minus what identifies its account.
IDENTITY_KEYS = (
    "oauthAccount", "userID", "organizationUuid", "cachedUsageUtilization",
    "modelAccessCache", "subscriptionNoticeCount", "statsigMetadata",
)
BACKUP_DAYS = 30


def main_name():
    return config.setting("main_profile")


def directory(name):
    return config.main_dir() if name == main_name() else Path.home() / f".claude-{name}"


def name_of(path):
    suffix = Path(path).name.removeprefix(".claude").lstrip("-")
    return suffix or main_name()


def all_dirs():
    others = sorted(p for p in Path.home().glob(".claude-*") if p.is_dir())
    return [config.main_dir(), *others]


def exists(name):
    return directory(name).is_dir()


def require(name):
    if not exists(name):
        die(f"unknown profile: {name}  (claude-helper profile init {name})")
    return directory(name)


def current():
    try:
        return (config.state_dir() / "current").read_text().strip() or main_name()
    except FileNotFoundError:
        return main_name()


def set_current(name):
    require(name)
    config.state_dir().mkdir(parents=True, exist_ok=True)
    (config.state_dir() / "current").write_text(name + "\n")


def of_this_process():
    """A process already attached to a profile stays on it: a sub-call never changes account."""
    if os.environ.get("CLAUDE_PROFILE"):
        return os.environ["CLAUDE_PROFILE"]
    if os.environ.get("CLAUDE_CONFIG_DIR"):
        return name_of(os.environ["CLAUDE_CONFIG_DIR"])
    return current()


def identity_file(path):
    """The .claude.json of a profile; the main one may still use the historic ~/.claude.json."""
    path = Path(path)
    own = path / ".claude.json"
    if path == config.main_dir() and not own.exists():
        return Path.home() / ".claude.json"
    return own


def read_json(path):
    try:
        return json.loads(Path(path).read_text())
    except (OSError, ValueError):
        return {}


def write_json(path, data):
    """Atomic, re-read before replacing: a broken .claude.json breaks the profile."""
    path = Path(path)
    tmp = path.with_name(f".{path.name}.tmp-{os.getpid()}")
    tmp.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n")
    json.loads(tmp.read_text())
    if path.exists():
        tmp.chmod(path.stat().st_mode & 0o777)
    os.replace(tmp, path)


def account(path):
    return read_json(identity_file(path)).get("oauthAccount") or {}


# --- linking ------------------------------------------------------------------

def _stamp():
    return time.strftime("%Y%m%d-%H%M%S")


def _keep_existing(src, dst):
    if not os.path.lexists(dst):
        shutil.copy2(src, dst, follow_symlinks=False)


def _link(shared, own):
    """Make `own` a symlink to `shared`. Returns what was done, or None if nothing was."""
    if own.is_symlink():
        if own.resolve() == shared.resolve():
            return None
        own.unlink()
    elif own.exists():
        if not shared.exists():
            shutil.move(own, shared)
            own.symlink_to(shared)
            return "adopted into the main profile"
        if own.is_dir():
            # Nothing the profile had is lost: what the main profile lacks is copied over.
            shutil.copytree(own, shared, symlinks=True, dirs_exist_ok=True,
                            copy_function=_keep_existing)
            own.rename(own.with_name(f"{own.name}.replaced-{_stamp()}"))
        elif filecmp.cmp(own, shared, shallow=False):
            own.unlink()
        else:
            aside = own.with_name(f"{own.name}.replaced-{_stamp()}")
            own.rename(aside)
            red(f"    ! {own.name} differed from the main profile: set aside as {aside.name}")
    if not shared.exists():
        return None
    own.symlink_to(shared)
    return "linked"


def _prune_backups(path, patterns=("*.replaced-*", ".claude.json.bak-*")):
    """A backup is there to recover what was just lost, not to keep history."""
    limit = time.time() - BACKUP_DAYS * 86400
    for p in (p for pattern in patterns for p in path.glob(pattern)):
        if p.is_file() and not p.is_symlink() and p.stat().st_mtime < limit:
            p.unlink()


def _servers(data):
    """{None: global servers, project path: its servers}."""
    found = {None: dict(data.get("mcpServers") or {})}
    for path, project in (data.get("projects") or {}).items():
        if project.get("mcpServers"):
            found[path] = dict(project["mcpServers"])
    return found


def _set_servers(data, union):
    changed = False
    if (data.get("mcpServers") or {}) != union.get(None, {}):
        if union.get(None):
            data["mcpServers"] = union[None]
        else:
            data.pop("mcpServers", None)
        changed = True
    for path, servers in union.items():
        if path is None:
            continue
        project = data.setdefault("projects", {}).setdefault(path, {})
        if (project.get("mcpServers") or {}) != servers:
            project["mcpServers"] = servers
            changed = True
    return changed


def reconcile_mcp(path):
    """Local MCP servers live in .claude.json, which is not shared since it carries the
    account. Union of both sides, the main profile wins on a name clash: a server added
    from any profile goes up to the main one, then down to the others."""
    main_file, own_file = identity_file(config.main_dir()), Path(path) / ".claude.json"
    if not (main_file.exists() and own_file.exists()):
        return []
    try:
        main, own = json.loads(main_file.read_text()), json.loads(own_file.read_text())
    except ValueError as e:
        red(f"    ! mcp: unreadable .claude.json, nothing done ({e})")
        return []
    on_main, on_own = _servers(main), _servers(own)
    union, moves = {}, []
    for key in set(on_main) | set(on_own):
        merged = {**on_own.get(key, {}), **on_main.get(key, {})}
        if merged:
            union[key] = merged
        moves += [f"{n}→{main_name()}" for n in set(merged) - set(on_main.get(key, {}))]
        moves += [f"{n}→{name_of(path)}" for n in set(merged) - set(on_own.get(key, {}))]
    for file, data in ((main_file, main), (own_file, own)):
        if _set_servers(data, union):
            shutil.copy2(file, file.with_name(f".claude.json.bak-mcp-{_stamp()}"))
            write_json(file, data)
    return sorted(set(moves))


def sync(name, quiet=False):
    """Lay the links of a profile and reconcile its MCP servers. Idempotent."""
    say = (lambda *_: None) if quiet else green
    if name == main_name():
        return
    path = require(name)
    main = config.main_dir()
    if not quiet:
        print(f"· {name}  ({path})")
    for entry in (*SHARED_DIRS, *SHARED_FILES):
        done = _link(main / entry, path / entry)
        if done:
            say(f"    → {entry} {done}")
    moves = reconcile_mcp(path)
    if moves:
        say(f"    → mcp reconciled: {', '.join(moves)}")
    _prune_backups(path)
    _prune_backups(main)
    _prune_backups(identity_file(main).parent, (".claude.json.bak-*",))


def sync_all():
    for path in all_dirs()[1:]:
        sync(name_of(path))


def init(name):
    if name == main_name():
        die(f"« {name} » is the main profile ~/.claude, it already exists.")
    path = directory(name)
    if path.exists():
        grey(f"{path} already exists — sync only.")
    else:
        path.mkdir()
        # Non-identity settings of the main profile (trusted folders, display options).
        source = read_json(identity_file(config.main_dir()))
        for key in IDENTITY_KEYS:
            source.pop(key, None)
        (path / ".claude.json").write_text(json.dumps(source, indent=2, ensure_ascii=False) + "\n")
        (path / ".claude.json").chmod(0o600)
        green(f"profile {name} created: {path}")
    sync(name)
    print(f"\nLog in:  claude-helper profile run {name}   then  /login")


def propagate_trust(path, cwd):
    """The answer to « Do you trust this folder? » lives in each profile's .claude.json.
    Take it from a profile that already gave it: otherwise every switch reopens the dialog."""
    target = identity_file(path)
    data = read_json(target)
    if not data or (data.get("projects") or {}).get(cwd, {}).get("hasTrustDialogAccepted"):
        return
    for other in all_dirs():
        if other.resolve() == Path(path).resolve():
            continue
        project = (read_json(identity_file(other)).get("projects") or {}).get(cwd) or {}
        if project.get("hasTrustDialogAccepted"):
            data.setdefault("projects", {}).setdefault(cwd, {})["hasTrustDialogAccepted"] = True
            write_json(target, data)
            return


def doctor(name=None):
    """Check the setup without changing anything. Returns the number of problems."""
    problems = 0
    wrapper = shutil.which("claude")
    if not wrapper or Path(wrapper).resolve() != config.ENTRY.resolve():
        red(f"✗ `claude` resolves to {wrapper}, not to the claude-helper wrapper: "
            f"put {config.ENTRY.parent} LAST in PATH")
        problems += 1
    if not config.claude_bin().exists():
        red(f"✗ {config.claude_bin()} not found (setting claude_bin)")
        problems += 1
    main = config.main_dir()
    for path in [directory(name)] if name else all_dirs()[1:]:
        print(f"· {name_of(path)}")
        for entry in (*SHARED_DIRS, *SHARED_FILES):
            shared, own = main / entry, path / entry
            if not shared.exists():
                continue
            if own.is_symlink() and own.resolve() == shared.resolve():
                grey(f"    ✓ {entry}")
            elif own.exists():
                red(f"    ✗ {entry} is not linked to the main profile (fixed on next run)")
                problems += 1
            else:
                grey(f"    · {entry} missing (linked on next run)")
        if not (path / ".credentials.json").exists():
            grey("    · not logged in")
    return problems
