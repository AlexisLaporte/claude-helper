"""Browse the CLAUDE.md files of the machine, and see what a session actually loads.

The browser lists, marked « ● », the files that apply to the current directory in
their load order, then every other one from heaviest to lightest. The preview
refreshes as soon as the editor saves.

  ↑↓ move · Enter open in the desktop editor · / filter · a show all · q quit
"""
import curses
import os
import subprocess
from pathlib import Path

from . import config
from .term import bold, home_short, paint

SKIPPED = {"node_modules", ".git", ".venv", "venv", "site-packages", "__pycache__", "target",
           "dist", "build", ".next", ".cache", "worktrees", "cache"}
NAMES = ("CLAUDE.md", "CLAUDE.local.md")
# Frozen copies: a worktree or a cache does not hold instructions anyone maintains.
FROZEN = ("/worktrees/", "/.cache/")


def tokens(path):
    try:
        return len(path.read_text(errors="replace")) // 4
    except OSError:
        return 0


def walk(skip=SKIPPED):
    """Every CLAUDE.md under the configured roots."""
    for root in config.md_roots():
        for folder, subdirs, files in os.walk(root, followlinks=False):
            subdirs[:] = [d for d in subdirs if d not in skip]
            for name in NAMES:
                if name in files:
                    yield Path(folder, name)


def all_files():
    seen = []
    for path in walk():
        real = path.resolve()
        if real not in seen:
            seen.append(real)
    return seen


def cascade_of(start):
    """Files loaded by a session opened in `start`, in load order: the user file, then
    every parent down to the directory itself — the closest is read last."""
    start = Path(start).resolve()
    if not start.is_dir():
        start = start.parent
    loaded = []
    user = (config.main_dir() / "CLAUDE.md").resolve()
    if user.exists():
        loaded.append(user)
    for level in reversed([start, *start.parents]):
        for candidate in (level / "CLAUDE.md", level / ".claude" / "CLAUDE.md", level / "CLAUDE.local.md"):
            if candidate.exists() and candidate.resolve() not in loaded:
                loaded.append(candidate.resolve())
    return loaded


def cascade(start):
    loaded = cascade_of(start)
    print(bold(f"From {home_short(Path(start).resolve())}") + "\n")
    for path in loaded:
        print(paint(f"{tokens(path):>7}", 32) + f"  {home_short(path)}")
    print("\n" + bold(f"{len(loaded)} files · {sum(map(tokens, loaded))} tokens at every session"))


def junk():
    """Frozen copies and broken links."""
    found = []
    for path in walk(skip={"node_modules", ".git", "site-packages"}):
        if any(part in str(path) for part in FROZEN):
            found.append((path, "frozen copy"))
        elif path.is_symlink() and not path.exists():
            found.append((path, "broken link"))
    skills = config.main_dir() / "skills"
    for path in skills.iterdir() if skills.is_dir() else []:
        if path.is_symlink() and not path.exists():
            found.append((path, "broken skill"))
    size = sum(p.lstat().st_size for p, _ in found)
    for path, reason in found:
        print(paint(f"{reason:>12}", 31) + f"  {home_short(path)}")
    print("\n" + bold(f"{len(found)} entries · {size // 1024} KB") if found else "Nothing to throw away.")


def open_in_editor(path):
    """The desktop editor: xdg-open follows the association of the markdown type."""
    subprocess.Popen(["xdg-open", str(path)], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def preview(path, cache):
    """The lines of the file, read again as soon as the editor saved it."""
    try:
        stamp = path.stat().st_mtime
    except OSError:
        return ["gone"]
    if cache.get("path") != path or cache.get("stamp") != stamp:
        try:
            lines = path.read_text(errors="replace").splitlines()
        except OSError as e:
            lines = [f"unreadable: {e}"]
        cache.update(path=path, stamp=stamp, lines=lines)
    return cache["lines"]


def _screen(screen, files, active, weight):
    curses.curs_set(0)
    curses.use_default_colors()
    for i, color in enumerate([curses.COLOR_CYAN, curses.COLOR_YELLOW, curses.COLOR_GREEN], 1):
        curses.init_pair(i, color, -1)
    cyan, yellow, green = (curses.color_pair(i) for i in (1, 2, 3))

    chosen = top = 0
    query, typing = "", False
    cache = {}
    screen.timeout(700)  # regular wake-up, to see a save arrive

    while True:
        visible = [f for f in files if query.lower() in str(f).lower()]
        chosen = max(0, min(chosen, len(visible) - 1))

        screen.erase()
        h, w = screen.getmaxyx()
        left = min(58, w // 2)
        body, width = h - 2, max(10, w - left - 3)
        if chosen < top:
            top = chosen
        elif chosen >= top + body:
            top = chosen - body + 1

        for i, f in enumerate(visible[top:top + body]):
            text = f"{'●' if f in active else ' '} {weight[f]:>6}  {home_short(f)}"[:left - 1]
            attr = curses.A_REVERSE if top + i == chosen else (green if f in active else 0)
            screen.addnstr(i + 1, 0, text.ljust(left - 1), left - 1, attr)
        for y in range(1, h - 1):
            screen.addch(y, left, curses.ACS_VLINE)

        if visible:
            target = visible[chosen]
            weight[target] = tokens(target)
            y = 1
            for line in preview(target, cache):
                if y >= h - 1:
                    break
                if not line:
                    y += 1
                    continue
                attr = curses.A_BOLD | cyan if line.startswith("#") else 0
                while line and y < h - 1:
                    screen.addnstr(y, left + 2, line[:width], width, attr)
                    line, y = line[width:], y + 1
            screen.addnstr(0, left + 2, home_short(target)[:width], width, curses.A_BOLD)

        here = sum(1 for f in visible if f in active)
        header = f" {len(visible)}/{len(files)} " if query else f" {here} loaded here · {len(visible)} in all "
        screen.addnstr(0, 0, header.ljust(left - 1), left - 1, curses.A_BOLD | yellow)
        footer = f" /{query}" if typing else " ↑↓ move · Enter open · / filter · q quit"
        screen.addnstr(h - 1, 0, footer.ljust(w - 1), w - 1, curses.A_REVERSE)
        screen.refresh()

        try:
            key = screen.get_wch()
        except curses.error:
            continue  # nothing typed: loop to refresh the preview

        if typing:
            if key in ("\n", "\r", "\x1b"):
                typing = False
            elif key in (curses.KEY_BACKSPACE, "\x7f", "\b"):
                query = query[:-1]
            elif isinstance(key, str) and key.isprintable():
                query, chosen = query + key, 0
            continue
        if key in ("q", "\x1b"):
            return
        if key in (curses.KEY_DOWN, "j"):
            chosen += 1
        elif key in (curses.KEY_UP, "k"):
            chosen -= 1
        elif key == curses.KEY_NPAGE:
            chosen += body
        elif key == curses.KEY_PPAGE:
            chosen -= body
        elif key == "/":
            typing, query = True, ""
        elif key == "a":
            query = ""
        elif key in ("\n", "\r", curses.KEY_ENTER) and visible:
            open_in_editor(visible[chosen])


def browse():
    weight = {f: tokens(f) for f in all_files()}
    # Those that apply here first, in load order; the rest by weight.
    active = cascade_of(Path.cwd())
    for f in active:
        weight.setdefault(f, tokens(f))
    rest = sorted((f for f in weight if f not in set(active)), key=weight.get, reverse=True)
    curses.wrapper(_screen, active + rest, set(active), weight)
