"""Launching Claude on a profile, and switching every open session to another one.

A `/login` inside a session is not enough to change account: the claude.ai MCP
servers stay bound to the account the process started with. Switching therefore
means stopping each session and resuming it (`--resume`) on the new profile.

Claude Code's own registry says what to relaunch and where: it writes
<config>/sessions/<pid>.json for each live session (pid, sessionId, cwd,
status busy|idle|waiting, kind).
"""
import json
import os
import shlex
import shutil
import signal
import subprocess
import time
from pathlib import Path

from . import config, profiles
from .term import die, green, grey

LAUNCHER_VAR = "CLAUDE_HELPER_LAUNCHER_PID"
# Options of the original command line kept on relaunch. Everything else (initial
# prompt, --resume, --continue…) is dropped: a relaunch always resumes the same session.
OPTIONS_WITH_VALUE = ("--permission-mode", "--model", "--effort", "--add-dir", "--mcp-config",
                      "--agent", "--settings", "--append-system-prompt")
FLAGS = ("--dangerously-skip-permissions", "--verbose", "--chrome", "--no-chrome", "--ide",
         "--strict-mcp-config")
SWITCH_TIMEOUT = 1800


def relaunch_dir():
    return config.state_dir() / "relaunch"


def switch_log():
    return config.state_dir() / "switch.log"


def kept_options(args):
    kept, args = [], list(args)
    while args:
        arg = args.pop(0)
        if arg in FLAGS:
            kept.append(arg)
        elif arg in OPTIONS_WITH_VALUE:
            kept.append(arg)
            if args:
                kept.append(args.pop(0))
    return kept


def has_transcript(session_id):
    """A session with no exchange yet has no transcript: --resume would fail."""
    return any((config.main_dir() / "projects").glob(f"*/{session_id}.jsonl"))


def run(name, args):
    """Sync, launch Claude, sync again — and start over on another profile when `switch`
    left a marker <state>/relaunch/<pid of this launcher>."""
    path = profiles.require(name)
    real = config.claude_bin()
    if real.resolve() == config.ENTRY.resolve():
        die(f"{real} is the claude-helper wrapper itself: the Claude Code install is gone?")
    relaunch_dir().mkdir(parents=True, exist_ok=True)
    marker = relaunch_dir() / str(os.getpid())
    # Ctrl+C belongs to Claude: the launcher must survive it to keep its loop.
    signal.signal(signal.SIGINT, lambda *_: None)
    while True:
        marker.unlink(missing_ok=True)
        profiles.sync(name, quiet=True)
        for cwd in {os.getcwd(), os.environ.get("PWD") or os.getcwd()}:
            profiles.propagate_trust(path, cwd)
        env = dict(os.environ, CLAUDE_PROFILE=name, BROWSER=str(config.ENTRY.parent / "claude-browser"))
        env[LAUNCHER_VAR] = str(os.getpid())
        if name == profiles.main_name():
            env.pop("CLAUDE_CONFIG_DIR", None)
        else:
            env["CLAUDE_CONFIG_DIR"] = str(path)
        code = subprocess.call([str(real), *args], env=env)
        profiles.sync(name, quiet=True)  # what the session changed goes up to the main profile
        if not marker.exists():
            break
        target, session_id = marker.read_text().split()[:2]
        marker.unlink()
        if not profiles.exists(target):
            break
        args = kept_options(args)
        if has_transcript(session_id):
            args += ["--resume", session_id]
            grey(f"↻ resuming the session on profile {target}")
        else:
            grey(f"↻ empty session: new session on profile {target}")
        name, path = target, profiles.directory(target)
    return code if code >= 0 else 128 - code  # killed by a signal: the shell convention


def use(name):
    profiles.set_current(name)
    green(f"current profile: {name} — `claude` now launches it")


# --- live sessions -------------------------------------------------------------

def env_of(pid, key):
    try:
        entries = Path(f"/proc/{pid}/environ").read_bytes().split(b"\0")
    except OSError:
        return ""
    prefix = key.encode() + b"="
    return next((e[len(prefix):].decode() for e in entries if e.startswith(prefix)), "")


def alive(pid):
    try:
        os.kill(int(pid), 0)
        return True
    except (OSError, ValueError):
        return False


def live_sessions():
    """Interactive sessions whose process is alive, one dict per pid."""
    seen = {}
    for path in profiles.all_dirs():
        for file in (path / "sessions").glob("*.json"):
            entry = profiles.read_json(file)
            pid = entry.get("pid")
            if entry.get("kind") != "interactive" or pid in seen or not alive(pid):
                continue
            seen[pid] = dict(entry, file=file, profile=profiles.name_of(path))
    return list(seen.values())


def launched_by(pid):
    if alive(env_of(pid, LAUNCHER_VAR)):
        return "launcher"
    if env_of(pid, "KITTY_WINDOW_ID"):
        return "kitty"
    return "manual"


def live():
    row = "%-8s %-10s %-8s %-9s %-38s %s"
    print(row % ("PID", "PROFILE", "STATUS", "RELAUNCH", "SESSION", "CWD"))
    for s in live_sessions():
        print(row % (s["pid"], s["profile"], s.get("status", "?"), launched_by(s["pid"]),
                     s.get("sessionId", ""), s.get("cwd", "")))


# --- switch --------------------------------------------------------------------

def switch(name, force=False):
    use(name)
    sessions = live_sessions()
    todo = [s for s in sessions if s["profile"] != name]
    if not todo:
        grey(f"no open session to relaunch ({len(sessions)} already on {name})")
        return
    print(f"{len(todo)} session(s) to relaunch on {name}, {len(sessions) - len(todo)} already on it. "
          "Each one resumes with --resume as soon as it is idle"
          + (" (--force: without waiting)." if force else "."))
    grey(f"log: {switch_log()}")
    config.state_dir().mkdir(parents=True, exist_ok=True)
    # Detached: the session that gave the order is relaunched too.
    with switch_log().open("a") as log:
        subprocess.Popen([str(config.ENTRY), "profile", "_switch-worker", name, str(int(force))],
                         stdin=subprocess.DEVNULL, stdout=log, stderr=log, start_new_session=True)


def log(text):
    print(time.strftime("%H:%M:%S"), text, flush=True)


def stop(pid):
    """SIGTERM (clean exit), then SIGKILL after 10 s."""
    for sig in (signal.SIGTERM, signal.SIGKILL):
        try:
            os.kill(pid, sig)
        except ProcessLookupError:
            return
        for _ in range(20):
            if not alive(pid):
                return
            time.sleep(0.5)


def kitty_foreground(listen_on, window_id):
    """Names of the foreground processes of a kitty window."""
    out = subprocess.run(["kitty", "@", "--to", listen_on, "ls"], capture_output=True, text=True)
    if out.returncode:
        return None
    for os_window in json.loads(out.stdout):
        for tab in os_window["tabs"]:
            for window in tab["windows"]:
                if window["id"] == int(window_id):
                    return [Path(p["cmdline"][0]).name for p in window["foreground_processes"]]
    return None


SHELLS = ("bash", "zsh", "fish", "sh", "-bash", "-zsh")


def back_to_shell(listen_on, window_id):
    """The window is back to its shell once the shell is its ONLY foreground process
    (a launcher script running bash would otherwise pass for it)."""
    for _ in range(30):
        foreground = kitty_foreground(listen_on, window_id) or []
        if len(foreground) == 1 and foreground[0] in SHELLS:
            return True
        time.sleep(0.5)
    return False


def relaunch(session, target):
    pid, sid, cwd = session["pid"], session["sessionId"], session["cwd"]
    # Absolute path: the shell of the window may predate the PATH that has claude-helper.
    command = f"cd {shlex.quote(cwd)} && {shlex.quote(str(config.ENTRY))} profile run {target} --resume {sid}"
    launcher = env_of(pid, LAUNCHER_VAR)
    listen_on, window_id = env_of(pid, "KITTY_LISTEN_ON"), env_of(pid, "KITTY_WINDOW_ID")
    if alive(launcher):
        (relaunch_dir() / launcher).write_text(f"{target}\n{sid}\n")
        stop(pid)
        log(f"  {pid} relaunched by its launcher ({sid})")
    elif listen_on and window_id and shutil.which("kitty") and kitty_foreground(listen_on, window_id):
        stop(pid)
        if back_to_shell(listen_on, window_id):
            subprocess.run(["kitty", "@", "--to", listen_on, "send-text", "--match", f"id:{window_id}",
                            command + "\r"])
            log(f"  {pid} relaunched in its kitty window ({sid})")
        else:
            log(f"  ✗ {pid}: kitty window {window_id} did not come back to its shell. By hand:\n      {command}")
    else:
        log(f"  ✗ {pid}: neither launcher nor kitty window, left running. To relaunch it:\n      {command}")


def switch_worker(target, force):
    start = time.time()
    relaunch_dir().mkdir(parents=True, exist_ok=True)
    log(f"=== switch to {target} (force={int(force)})")
    waiting = {s["pid"]: s for s in live_sessions() if s["profile"] != target}
    done = 0
    while waiting:
        for pid, session in list(waiting.items()):
            if not alive(pid):
                log(f"  {pid} ended by itself")
                del waiting[pid]
                continue
            status = profiles.read_json(session["file"]).get("status", "?")
            if force or status != "busy":
                relaunch(session, target)
                done += 1
                del waiting[pid]
        if not waiting:
            break
        if time.time() - start > SWITCH_TIMEOUT:
            log(f"  ✗ giving up after {SWITCH_TIMEOUT // 60} min, still busy: {' '.join(map(str, waiting))}")
            break
        time.sleep(3)
    log(f"=== done: {done} session(s) relaunched on {target}")
    if shutil.which("notify-send"):
        subprocess.run(["notify-send", f"Claude → {target}", f"{done} session(s) relaunched"])
