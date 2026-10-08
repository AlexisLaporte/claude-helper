"""`profile list`: each profile, its account and its quota consumption.

Consumption is asked to the API (one GET per profile, with its own token) rather
than read from the cachedUsageUtilization of its .claude.json: that cache is only
refreshed by a running session, it is hours late on an idle profile.
"""
import json
import urllib.error
import urllib.request
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime

from . import config, profiles
from .term import paint

USAGE_URL = "https://api.anthropic.com/api/oauth/usage"
WIDTH = 16


def fetch(path):
    """(usage, None) or (None, reason)."""
    try:
        creds = json.loads((path / ".credentials.json").read_text())
        token = creds["claudeAiOauth"]["accessToken"]
    except (OSError, ValueError, KeyError):
        return None, "not logged in"
    request = urllib.request.Request(USAGE_URL, headers={
        "Authorization": f"Bearer {token}",
        "anthropic-beta": "oauth-2025-04-20",
    })
    try:
        with urllib.request.urlopen(request, timeout=10) as response:
            return json.load(response), None
    except urllib.error.HTTPError as e:
        return None, "token expired" if e.code == 401 else f"HTTP {e.code}"
    except (urllib.error.URLError, TimeoutError):
        return None, "unreachable"
    except ValueError:
        return None, "unreadable answer"


def window(usage, key, fmt):
    """A quota window: its load and the local time it resets."""
    w = (usage or {}).get(key) or {}
    if w.get("utilization") is None:
        return "—".ljust(WIDTH)
    load = round(w["utilization"])
    text = f"{load}%"
    if w.get("resets_at"):
        text += " →" + datetime.fromisoformat(w["resets_at"]).astimezone().strftime(fmt)
    text = text.ljust(WIDTH)
    return paint(text, 31) if load >= 90 else paint(text, 33) if load >= 70 else text


def sharing(path):
    if path == config.main_dir():
        return "main"
    return "shared" if (path / "projects").is_symlink() else "isolated"


def show(online=True):
    dirs = profiles.all_dirs()
    if online:
        with ThreadPoolExecutor(max_workers=8) as pool:
            usages = list(pool.map(fetch, dirs))
    else:
        usages = [(None, "·")] * len(dirs)

    row = "%s %-10s %-28s %-14s %-14s %-9s %s %s"
    print(row % (" ", "PROFILE", "ACCOUNT", "ORGANIZATION", "ROLE", "SESSIONS",
                 "5 HOURS".ljust(WIDTH), "7 DAYS"))
    current = profiles.current()
    by_account = defaultdict(list)
    for path, (usage, error) in zip(dirs, usages):
        name, acc = profiles.name_of(path), profiles.account(path)
        org = acc.get("organizationName") or "—"
        if org.endswith("'s Organization"):
            org = "(personal)"  # the default org of an individual account
        if acc.get("accountUuid"):
            by_account[acc["accountUuid"]].append(name)
        if error:
            five, seven = paint(error.ljust(WIDTH), 90), ""
        else:
            five = window(usage, "five_hour", "%H:%M")
            seven = window(usage, "seven_day", "%d/%m %H:%M")
        print(row % ("*" if name == current else " ", name,
                     acc.get("emailAddress") or "not logged in", org,
                     acc.get("organizationRole") or "—", sharing(path), five, seven))
    for uuid, names in by_account.items():
        if len(names) > 1:
            print(paint(f"≡ {', '.join(names)}: one and the same account ({uuid[:8]})", 90))
    if online:
        print(paint("→ reset times are local. --offline does not call the API.", 90))
