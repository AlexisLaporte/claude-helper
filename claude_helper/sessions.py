"""Find past Claude Code conversations, and recall one.

A session's name is read from its transcript itself (last `customTitle`, set by
/rename): nothing to keep up to date, so nothing that drifts.
"""
import json
import re
import subprocess
import sys
from datetime import datetime
from itertools import islice
from pathlib import Path

from . import config
from .term import bold, home_short, paint

# Turns that look typed by the user but are injected by Claude Code.
NOISE = ("<system-reminder>", "<local-command", "<task-notification>", "Caveat:",
         "<command-name>", "[Request interrupted", "Another Claude session sent",
         "This session is being continued", "## Context Usage", "<user-prompt-submit-hook>")
INJECTED = re.compile(r"<(system-reminder|local-command-stdout)>.*?</\1>", re.DOTALL)
HEAD_LINES = 3000
GREP_BATCH = 400  # keeps the argument list within execve limits
BUSY_MINUTES = 15


def projects_dir():
    return config.main_dir() / "projects"


def project_of(cwd):
    """Claude Code names a project folder after its path, every non-alphanumeric as '-'."""
    return projects_dir() / re.sub(r"[^A-Za-z0-9]", "-", str(cwd))


def transcripts(here=False):
    root = project_of(Path.cwd()) if here else projects_dir()
    if not root.is_dir():
        raise SystemExit("No conversation for this directory." if here else f"{root} not found.")
    files = [p for p in root.rglob("*.jsonl") if "subagents" not in p.parts and p.stat().st_size]
    return sorted(files, key=lambda p: p.stat().st_mtime, reverse=True)


def typed_text(entry):
    """The text of a turn typed at the keyboard — not a tool result nor a system reminder."""
    text = entry.get("lastPrompt")
    if text is None and entry.get("type") == "user":
        content = (entry.get("message") or {}).get("content")
        text = content if isinstance(content, str) else None
    if not text:
        return None
    text = INJECTED.sub(" ", text).strip()
    return text if text and not text.startswith(NOISE) else None


def head(path):
    """Title, directory and first sentence — all near the top of a transcript."""
    title = cwd = first = None
    with path.open(errors="replace") as f:
        for line in islice(f, HEAD_LINES):
            if not ("Title" in line or '"cwd"' in line or '"last-prompt"' in line
                    or '"type":"user"' in line):
                continue
            try:
                entry = json.loads(line)
            except ValueError:
                continue
            title = entry.get("customTitle") or entry.get("aiTitle") or title
            cwd = cwd or entry.get("cwd")
            first = first or typed_text(entry)
    return title, cwd, first


def _grep(args, files):
    out = []
    for i in range(0, len(files), GREP_BATCH):
        r = subprocess.run(["grep", "-a", *args, *map(str, files[i:i + GREP_BATCH])],
                           capture_output=True, text=True)
        out += r.stdout.splitlines()
    return out


def names(files):
    """{path: current name}: the LAST `customTitle`, wherever it is in the file —
    `head` only reads the top and would miss a late /rename."""
    found = {}
    for line in _grep(["-o", "-H", '"customTitle":"[^"]*"'], files):
        path, _, name = line.partition(':"customTitle":"')
        found[path] = name[:-1]
    return found


def whole_word(word):
    return re.compile(rf"(?<!\w){re.escape(word)}(?!\w)", re.IGNORECASE)


def cut(text, word=None, width=110):
    text = re.sub(r"\s+", " ", text).strip()
    if len(text) <= width:
        return text
    hit = whole_word(word).search(text) if word else None
    if not hit:
        return text[:width] + "…"
    start = max(0, hit.start() - width // 3)
    return ("…" if start else "") + text[start:start + width] + "…"


def show(path, excerpt=None, word=None, title=None):
    head_title, cwd, first = head(path)
    title = title or head_title
    when = datetime.fromtimestamp(path.stat().st_mtime).strftime("%d/%m %H:%M")
    print(paint(when, 33) + "  " + paint(home_short(cwd), 36) + (f"  {bold(title)}" if title else ""))
    sentence = excerpt or first
    if sentence:
        print("        " + paint(f"« {cut(sentence, word)} »", 90))
    resume = f"claude --resume {path.stem}"
    if cwd and cwd != str(Path.cwd()):
        resume = f"cd {cwd} && {resume}"
    print(f"        {resume}\n")


def latest(limit, here):
    files = transcripts(here)
    for path in files[:limit]:
        show(path)
    print(bold(f"{min(limit, len(files))} of {len(files)} conversations"))


def by_name(word, limit, here):
    """Sessions whose name or project folder contains the word, most recent first."""
    files = transcripts(here)
    titles, word = names(files), word.casefold()
    n = 0
    for path in files:
        title = titles.get(str(path), "")
        if word in title.casefold() or word in path.parent.name.casefold():
            show(path, title=title)
            n += 1
            if n >= limit:
                break
    print(bold(f"{n} session(s) named « {word} »") if n else f"No session named « {word} ».")


def search(word, limit, here):
    """Conversations where the user typed the word: grep narrows down, the parser confirms."""
    files = transcripts(here)
    hits = set(_grep(["-l", "-i", "-F", "-w", "--", word], files))
    candidates = [p for p in files if str(p) in hits]
    pattern, n = whole_word(word), 0
    for path in candidates:
        found = None
        for line in _grep(["-h", "-i", "-F", "-w", "--", word], [path]):
            try:
                text = typed_text(json.loads(line))
            except ValueError:
                continue
            if text and pattern.search(text):
                found = text
                break
        if found:
            show(path, found, word)
            n += 1
            if n >= limit:
                break
    if n:
        print(bold(f"{n} conversation(s) where you typed « {word} »"))
    else:
        print(f"Not in your messages. {len(candidates)} transcripts mention « {word} » "
              "elsewhere (tool outputs, code read).")


def recall(target, question, write=False):
    """Wake a session up, even closed: it reloads its whole history, answers, exits.
    On a fork by default; `write` makes the real conversation move forward."""
    files = transcripts()
    titles = names(files)
    matches = [p for p in files if p.stem.startswith(target)] or \
              [p for p in files if titles.get(str(p), "").casefold() == target.casefold()]
    if not matches:
        close = sorted({t for t in titles.values() if target.casefold() in t.casefold()})
        raise SystemExit(f"No session « {target} »." + (f" Close names: {', '.join(close)}" if close else ""))
    path = matches[0]  # most recent first
    if len(matches) > 1:
        print(paint(f"{len(matches)} sessions « {target} »: the most recent wins ({path.stem}).", 90),
              file=sys.stderr)
    _, cwd, _ = head(path)
    if not cwd or not Path(cwd).is_dir():
        raise SystemExit(f"Session folder not found ({cwd}): --resume would not find it.")
    age = (datetime.now().timestamp() - path.stat().st_mtime) / 60
    if write and age < BUSY_MINUTES:
        # two processes on the same history collide
        raise SystemExit(f"Active {age:.0f} min ago: it may be open. Message it with SendMessage, "
                         "or recall it without --write (on a fork).")
    command = ["claude", "-p", "--resume", path.stem, *([] if write else ["--fork-session"]), question]
    return subprocess.run(command, cwd=cwd).returncode
