"""The man page, rendered from the argparse parser so it never drifts from --help.

    python3 -m claude_helper.manpage > man/man1/claude-helper.1

man finds it with no install step: man-db looks in <dir>/../man for every <dir> of PATH,
and bin/ is in PATH.
"""
import argparse

from . import cli

PAGE = "claude-helper.1"

DESCRIPTION = """\
Several Claude Code accounts on one Linux machine, with one configuration and one session
database, plus the small tools that make living with many sessions bearable.
.PP
The same entry point answers under several names:
.TP
.B claude
Claude Code on the current profile. bin/ must come last in PATH, so this wrapper wins over
the binary of the official installer.
.TP
.B claude\\-browser
the $BROWSER of the sessions: opens a URL in the browser window of the account.
.TP
.BR claude\\-helper ", " ch
everything else, described below."""

PROFILES = """\
A profile is a CLAUDE_CONFIG_DIR. The main profile is ~/.claude itself; every other profile
is ~/.claude\\-<name>, with its own login, token and account, and everything else (CLAUDE.md,
settings, skills, plugins, projects/ with the transcripts and memory) a symlink to the main
profile.
.PP
A /login inside a session does not move the claude.ai MCP servers to the new account: they
stay bound to the account the process started with. That is why
.B profile switch
stops every open session once idle and resumes it with \\-\\-resume on the new profile."""

FILES = """\
.TP
.I ~/.config/claude\\-helper/config.toml
settings, every key optional: main_profile, claude_bin, md_roots, browser.
.TP
.I ~/.local/state/claude\\-helper/
current profile, relaunch markers, switch.log (resume commands of the sessions that could
not be relaunched).
.TP
.I ~/.local/share/claude\\-helper/browser/
one browser profile per account.
.TP
.I <config>/sessions/<pid>.json
Claude Code's own registry of open sessions, read by switch and live."""

ENVIRONMENT = """\
.TP
.B CLAUDE_PROFILE
the profile of this process; a session never changes account under its own feet.
.TP
.B CLAUDE_CONFIG_DIR
Claude Code's configuration directory, set by the wrapper from the profile."""


def esc(text):
    return text.replace("\\", "\\\\").replace("-", "\\-")


def subcommands(p):
    """(name, help, parser) of each subcommand of p, in declaration order."""
    action = next((a for a in p._actions if isinstance(a, argparse._SubParsersAction)), None)
    if action is None:
        return []
    helps = {c.dest: c.help for c in action._choices_actions}
    return [(name, helps.get(name, ""), sp) for name, sp in action.choices.items()]


def arguments(p):
    lines = []
    for a in p._actions:
        if isinstance(a, (argparse._HelpAction, argparse._SubParsersAction)) or not a.help:
            continue
        if a.option_strings:
            label = ", ".join(f"\\fB{esc(o)}\\fR" for o in a.option_strings)
            if a.nargs != 0:
                label += f" \\fI{esc(a.metavar or a.dest.upper())}\\fR"
        else:
            label = f"\\fI{esc(a.metavar or a.dest)}\\fR"
        lines += [".TP", label, esc(a.help or "")]
    return lines


def usage(p):
    return esc(" ".join(w for w in p.format_usage().split()[1:] if w != "[-h]"))


def command(name, help_, p, depth):
    if depth == 0:
        out = [f".SS {esc(name)}", esc(help_)]
    else:
        out = [".PP", f"\\fB{usage(p)}\\fR", ".RS", esc(help_)]
    children = subcommands(p)
    if children:
        for child in children:
            out += command(*child, depth + 1)
    else:
        if depth == 0:
            out += [".PP", f"\\fB{usage(p)}\\fR"]
        out += arguments(p)
    if depth:
        out.append(".RE")
    return out


def render():
    p = cli.parser()
    out = [
        '.TH CLAUDE\\-HELPER 1 "" "claude-helper" "User Commands"',
        ".SH NAME",
        "claude\\-helper, ch \\- " + esc(p.description.rstrip(".")),
        ".SH SYNOPSIS",
        "\\fBclaude\\-helper\\fR \\fIcommand\\fR [\\fIargs\\fR...]",
        ".br",
        "\\fBclaude\\fR [\\fIclaude arguments\\fR...]",
        ".SH DESCRIPTION",
        DESCRIPTION,
        ".SH COMMANDS",
    ]
    for c in subcommands(p):
        out += command(*c, 0)
    out += [".SH PROFILES", PROFILES, ".SH FILES", FILES, ".SH ENVIRONMENT", ENVIRONMENT,
            ".SH SEE ALSO", "\\fBclaude\\-helper\\fR \\fIcommand\\fR \\fB\\-\\-help\\fR,",
            "the README: https://github.com/AlexisLaporte/claude\\-helper"]
    return "\n".join(out) + "\n"


if __name__ == "__main__":
    print(render(), end="")
