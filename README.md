# claude-helper

Several Claude Code accounts on one Linux machine, with **one** configuration and **one** session database — plus the small tools that make living with many sessions bearable.

```
claude                         Claude Code on the current profile
claude-helper profile …        accounts: list, use, switch, init, doctor
claude-helper sessions …       find past conversations
claude-helper recall …         wake a past session up and ask it something
claude-helper md …             browse CLAUDE.md files, see what a directory loads
claude-helper browser …        one browser window per account
```

`ch` is a short alias of `claude-helper`. Python 3.11+, standard library only, Linux.

## Install

```bash
git clone https://github.com/AlexisLaporte/claude-helper
claude-helper/install.sh
```

Then put `claude-helper/bin` **last** in your `PATH` (end of `~/.bashrc`, and of `~/.profile` if it re-adds `~/.local/bin`):

```bash
export PATH="/path/to/claude-helper/bin:$PATH"
```

`bin/claude` is a wrapper that must come before the binary of the official installer (`~/.local/bin/claude`). The installer rewrites that one on every update, so it is left alone. If anything re-exports `~/.local/bin` afterwards, the plain binary wins again and nothing tells you: `claude` always runs the main profile and `use`/`switch` have no effect. `claude-helper profile doctor` checks for it.

## Profiles

A profile is a `CLAUDE_CONFIG_DIR` — the one thing Claude Code really partitions: the token in the system keyring is named after a hash of the config directory, so two profiles never log each other out.

- The **main profile** is `~/.claude` itself (named `main`, configurable).
- Every other profile is `~/.claude-<name>`. Its login, token and account are its own; **everything else is a symlink to the main profile**: `CLAUDE.md`, `settings*.json`, `agents/`, `hooks/`, `skills/`, `plugins/`, `commands/`… and `projects/`, which holds the transcripts and memory. `/resume` finds any conversation from any account.
- Never shared: `.credentials.json` and `.claude.json` (account, token, organization).

| Command | |
|---|---|
| `profile list [--offline]` | profiles, account, organization and role, quota used (5 hours / 7 days, asked to the API with each profile's token), current one marked `*`; a `≡` note when two profiles are the same account |
| `profile use <name>` | the current profile — the one `claude` launches |
| `profile switch <name> [--force]` | `use`, **and** relaunch every open session on that account with `--resume` |
| `profile live` | open sessions: pid, profile, status, how they can be relaunched |
| `profile init <name>` | create `~/.claude-<name>` and link it to the main profile; then `/login` in it |
| `profile run <name> [args…]` | sync, launch Claude on that profile, sync again |
| `profile sync [<name>]` | lay the links again, reconcile the MCP servers |
| `profile doctor [<name>]` | check the setup without changing anything |

For a per-profile shortcut: `alias claude-work='claude-helper profile run work'`.

A process already attached to a profile stays on it: `claude` run from inside a session (`CLAUDE_PROFILE` or `CLAUDE_CONFIG_DIR` inherited) never changes account.

**Why `switch` relaunches sessions.** A `/login` inside a session is not enough: the claude.ai MCP servers stay bound to the account the process started with. `switch` reads Claude Code's own registry (`<config>/sessions/<pid>.json`: pid, sessionId, cwd, busy/idle) and, detached, waits for each session to be idle (unless `--force`), stops it (SIGTERM, clean exit) and resumes it on the new profile, in the same terminal. Three ways, depending on how the session was started:

1. through the `claude` wrapper: its launcher loops on a marker and resumes by itself;
2. otherwise, in a [kitty](https://sw.kovidgoyal.net/kitty/) window with remote control on: the command is typed back into the window;
3. otherwise the session is left running and the resume command is written to `~/.local/state/claude-helper/switch.log`.

A session that has not exchanged anything yet has no transcript: it starts fresh.

**What is reconciled.** Trust given to a folder (« Do you trust this folder? ») lives in each profile's `.claude.json`: it is copied from a profile that already gave it before every launch, otherwise each switch would reopen the dialog in every window. Local MCP servers live there too: their union is kept on both sides, the main profile winning on a name clash. A file or folder found in a profile where a link should be is merged into the main profile (folders) or set aside as `<name>.replaced-<date>` (files that differ), then linked. Backups expire after 30 days.

**Editing shared files.** Claude Code writes settings through a symlink without trouble (`/config`, `/model`, permission grants). Its Edit/Write tools refuse a symlinked file — loudly, without breaking anything: an agent editing `CLAUDE.md` must target `~/.claude/CLAUDE.md`. A script that rewrites a settings file with `mv tmp file` replaces the link with a plain file: write to `~/.claude/settings.json` only.

## Sessions

```bash
claude-helper sessions                 # the 15 latest, every project — after a crash
claude-helper sessions datastore       # those where you typed that word
claude-helper sessions --here          # only the project of the current directory
claude-helper sessions --name review   # by session name (/rename) or project folder
claude-helper recall review "what did we decide about X?"   # on a fork
claude-helper recall review "go on" --write                  # moves the real conversation forward
```

Each entry prints its resume command. Names are read from the transcripts themselves (last `customTitle`): nothing to maintain. `recall` resumes the session headless (`claude -p --resume`): it reloads its whole history, answers, and exits — on a fork unless `--write`, which refuses a session active less than 15 minutes ago (two processes on one history collide).

## CLAUDE.md files

```bash
claude-helper md               # list on the left, preview on the right; Enter opens the desktop editor
claude-helper md ~/src/app     # what a session opened there loads, and what it costs in tokens
claude-helper md --junk        # frozen copies (worktrees, caches) and broken links
```

Files that apply to the current directory come first, in load order, marked `●`; the rest follow from heaviest to lightest. The preview reloads when the editor saves. Enter uses `xdg-open`: set the markdown association of your choice (`xdg-mime default <app>.desktop text/markdown`).

## Browser

```bash
claude-helper browser [url…]               # the window of the current profile's account
claude-helper browser -p work https://…    # a given profile
claude-helper browser --list
```

One Chrome/Chromium window per account, each with its own cookies (`--user-data-dir` under `~/.local/share/claude-helper/browser/`). `profile run` exports `claude-browser` as `$BROWSER`, so the authentication URL of an MCP server opens by itself in the right account.

## Codex bridge

`codex/skills/claude-bridge` is a [Codex](https://github.com/openai/codex) skill: it talks to the open Claude Code sessions through their native socket and keeps a persistent inbox for their answers. `install.sh` links it into `~/.codex/skills/` when Codex is installed. See its [SKILL.md](codex/skills/claude-bridge/SKILL.md).

## Settings

`~/.config/claude-helper/config.toml`, every key optional:

```toml
main_profile = "main"                # name of the ~/.claude profile
claude_bin = "~/.local/bin/claude"   # the binary of the official installer
md_roots = ["~"]                     # where `md` looks for CLAUDE.md files
browser = ""                         # empty: google-chrome, then chromium
```

State lives in `~/.local/state/claude-helper/` (current profile, relaunch markers, switch log).

## Tests

```bash
python3 -m unittest discover -s tests
(cd codex/skills/claude-bridge/scripts && python3 -m unittest discover -p 'test_*.py')
```

Tests run in a throwaway `$HOME`.

## License

MIT
