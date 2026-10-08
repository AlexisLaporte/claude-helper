"""One browser window per account.

Reconnecting an MCP server, logging in again, opening the console: all of it
assumes the browser is already signed into the right account. This keeps ONE
Chrome window per account, with its own cookies (--user-data-dir), and sends
the URL there. Two profiles on the same account share the window.

`profile run` exports claude-browser as $BROWSER: from a Claude session, the
authentication URL of an MCP server opens by itself in the right place.
"""
import shutil
import subprocess
from urllib.parse import urlsplit

from . import config, profiles
from .term import die, grey

DEFAULT_URL = "https://claude.ai/"


def windows_dir():
    return config.data_dir() / "browser"


def binary():
    for candidate in filter(None, (config.setting("browser"), "google-chrome", "chromium")):
        if shutil.which(candidate):
            return candidate
    die("no browser found: install google-chrome or chromium, or set `browser`")


def window_of(name):
    """The window key of a profile: its account, or the profile while it is not logged in
    (it becomes the account's window after the first /login)."""
    email = profiles.account(profiles.require(name)).get("emailAddress")
    return email.replace("@", "_at_") if email else f"profile-{name}"


def listing():
    found = sorted(p for p in windows_dir().glob("*") if p.is_dir())
    for path in found:
        print(f"  {path.name}")
    if not found:
        print("no window yet")


def checked(urls):
    """Only web URLs reach the browser: as $BROWSER, it receives whatever a session asks
    to open, and an argument such as `--renderer-cmd-prefix=…` would be a Chrome switch."""
    for url in urls:
        if urlsplit(url).scheme not in ("http", "https"):
            die(f"not an http(s) URL, refused: {url}")
    return urls


def open_urls(urls, name=None, dry_run=False):
    name = name or profiles.of_this_process()
    # `--` ends Chrome's switches: what follows is only ever a URL.
    command = [binary(), f"--user-data-dir={windows_dir() / window_of(name)}",
               "--no-first-run", "--no-default-browser-check", "--", *checked(urls or [DEFAULT_URL])]
    grey(f"→ {name} · {command[1].split('=', 1)[1]}")
    if dry_run:
        print(" ".join(command))
        return
    (windows_dir() / window_of(name)).mkdir(parents=True, exist_ok=True)
    subprocess.Popen(command, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                     stderr=subprocess.DEVNULL, start_new_session=True)
