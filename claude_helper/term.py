"""Terminal output: colors only when writing to a terminal."""
import sys
from pathlib import Path


def paint(text, code, stream=sys.stdout):
    return f"\033[{code}m{text}\033[0m" if stream.isatty() else str(text)


def red(text):
    print(paint(text, 31, sys.stderr), file=sys.stderr)


def green(text):
    print(paint(text, 32))


def grey(text):
    print(paint(text, 90))


def bold(text):
    return paint(text, 1)


def die(text):
    red(text)
    raise SystemExit(1)


def home_short(path):
    text, home = str(path or "?"), str(Path.home())
    return "~" + text[len(home):] if text.startswith(home) else text
