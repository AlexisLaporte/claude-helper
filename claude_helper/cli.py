"""claude-helper — several Claude Code accounts, one configuration, and the tools around it.

The same entry point answers under several names:
  claude          Claude Code on the current profile (put bin/ last in PATH)
  claude-browser  the $BROWSER of the sessions: opens a URL in the account's window
  claude-helper   everything else (`claude-helper --help`), `ch` for short
"""
import argparse
import sys
from pathlib import Path

from . import browser, launch, md, profiles, sessions, usage


def parser():
    p = argparse.ArgumentParser(
        prog="claude-helper",
        description="Several Claude Code accounts, one configuration, and the tools around it.")
    sub = p.add_subparsers(dest="command", required=True, metavar="command")

    prof = sub.add_parser("profile", help="accounts: list, use, switch, init…")
    psub = prof.add_subparsers(dest="action", required=True, metavar="action")
    s = psub.add_parser("list", help="profiles, account, organization, quota (5 h / 7 days); * = current")
    s.add_argument("--offline", action="store_true", help="do not call the API for the quota")
    psub.add_parser("current", help="print the current profile")
    s = psub.add_parser("use", help="set the current profile: the one `claude` launches")
    s.add_argument("name")
    s = psub.add_parser("switch", help="use + relaunch every open session on that profile (--resume)")
    s.add_argument("name")
    s.add_argument("--force", action="store_true", help="do not wait for a session to be idle")
    psub.add_parser("live", help="open sessions: pid, profile, status, how they can be relaunched")
    s = psub.add_parser("init", help="create ~/.claude-<name> and link it to the main profile")
    s.add_argument("name")
    s = psub.add_parser("sync", help="lay the links again, reconcile the MCP servers")
    s.add_argument("name", nargs="?", help="default: every profile")
    psub.add_parser("run", help="sync, launch Claude on <name> with the remaining arguments, sync again",
                    usage="claude-helper profile run <name> [claude arguments…]")
    s = psub.add_parser("doctor", help="check the setup without changing anything")
    s.add_argument("name", nargs="?")

    s = sub.add_parser("sessions", help="find past conversations: latest, by word typed, by name")
    s.add_argument("word", nargs="?", help="a word you typed in the conversation")
    s.add_argument("--name", metavar="WORD", help="match the session name (/rename) or its project folder")
    s.add_argument("--here", action="store_true", help="only the project of the current directory")
    s.add_argument("-n", type=int, default=15, metavar="N", help="how many (default 15)")

    s = sub.add_parser("recall", help="wake a session up, even closed: it answers, then exits")
    s.add_argument("target", help="session name (/rename) or id")
    s.add_argument("question")
    s.add_argument("--write", action="store_true",
                   help="move the real conversation forward instead of a fork")

    s = sub.add_parser("md", help="browse the CLAUDE.md files, or show what a directory loads")
    s.add_argument("directory", nargs="?", help="show the cascade a session opened there loads")
    s.add_argument("--junk", action="store_true", help="frozen copies and broken links")

    s = sub.add_parser("browser", help="open URLs in the browser window of a profile's account")
    s.add_argument("urls", nargs="*")
    s.add_argument("--profile", "-p", help="default: the profile of this process, else the current one")
    s.add_argument("--list", action="store_true", help="windows already created")
    s.add_argument("--dry-run", "-n", action="store_true", help="print the command, open nothing")
    return p


def dispatch(args):
    # `run` passes everything after the profile name to Claude untouched.
    if args[:2] == ["profile", "run"]:
        if len(args) < 3:
            raise SystemExit("usage: claude-helper profile run <name> [claude arguments…]")
        return launch.run(args[2], args[3:])
    if args[:2] == ["profile", "_switch-worker"]:
        return launch.switch_worker(args[2], args[3] == "1")

    a = parser().parse_args(args)
    if a.command == "profile":
        match a.action:
            case "list":
                usage.show(online=not a.offline)
            case "current":
                print(profiles.current())
            case "use":
                launch.use(a.name)
            case "switch":
                launch.switch(a.name, a.force)
            case "live":
                launch.live()
            case "init":
                profiles.init(a.name)
            case "sync":
                profiles.sync(a.name) if a.name else profiles.sync_all()
            case "doctor":
                return 1 if profiles.doctor(a.name) else 0
    elif a.command == "sessions":
        if a.name:
            sessions.by_name(a.name, a.n, a.here)
        elif a.word:
            sessions.search(a.word, a.n, a.here)
        else:
            sessions.latest(a.n, a.here)
    elif a.command == "recall":
        return sessions.recall(a.target, a.question, a.write)
    elif a.command == "md":
        if a.junk:
            md.junk()
        elif a.directory:
            md.cascade(a.directory)
        else:
            md.browse()
    elif a.command == "browser":
        if a.list:
            browser.listing()
        else:
            browser.open_urls(a.urls, a.profile, a.dry_run)
    return 0


def main():
    name, args = Path(sys.argv[0]).name, sys.argv[1:]
    if name == "claude":
        code = launch.run(profiles.of_this_process(), args)
    elif name == "claude-browser":
        code = browser.open_urls(args) or 0
    else:
        code = dispatch(args)
    sys.exit(code or 0)
