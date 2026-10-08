"""Every test runs in a throwaway $HOME: the real ~/.claude is never touched."""
import io
import json
import os
import signal
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from claude_helper import browser, config, launch, md, profiles, sessions  # noqa: E402


class Home(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.home = Path(self.tmp.name)
        env = {"HOME": str(self.home), "XDG_CONFIG_HOME": "", "XDG_STATE_HOME": "",
               "XDG_DATA_HOME": "", "CLAUDE_PROFILE": "", "CLAUDE_CONFIG_DIR": ""}
        patcher = mock.patch.dict(os.environ, env)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.addCleanup(self.tmp.cleanup)
        self.main = self.home / ".claude"
        (self.main / "projects").mkdir(parents=True)
        (self.main / "skills").mkdir()
        (self.main / "CLAUDE.md").write_text("# user\n")
        (self.main / "settings.json").write_text('{"theme": "dark"}\n')
        self.write_json(self.home / ".claude.json", {
            "oauthAccount": {"emailAddress": "main@example.com"},
            "projects": {"/work": {"hasTrustDialogAccepted": True}},
        })

    def write_json(self, path, data):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(data))

    def configure(self, text):
        (self.home / ".config" / "claude-helper").mkdir(parents=True, exist_ok=True)
        (self.home / ".config" / "claude-helper" / "config.toml").write_text(text)

    def quiet(self, fn, *args):
        with redirect_stdout(io.StringIO()):
            return fn(*args)


class Names(Home):
    def test_main_profile_name_is_configurable(self):
        self.assertEqual(profiles.name_of(self.main), "main")
        self.configure('main_profile = "pro"\n')
        self.assertEqual(profiles.name_of(self.main), "pro")
        self.assertEqual(profiles.directory("pro"), self.main)
        self.assertEqual(profiles.directory("work"), self.home / ".claude-work")

    def test_unknown_setting_is_an_error(self):
        self.configure('main_profil = "pro"\n')
        with self.assertRaises(SystemExit):
            profiles.main_name()

    def test_process_keeps_its_profile(self):
        self.quiet(profiles.init, "work")
        profiles.set_current("work")
        self.assertEqual(profiles.of_this_process(), "work")
        with mock.patch.dict(os.environ, {"CLAUDE_CONFIG_DIR": str(self.home / ".claude-other")}):
            self.assertEqual(profiles.of_this_process(), "other")
        with mock.patch.dict(os.environ, {"CLAUDE_PROFILE": "main"}):
            self.assertEqual(profiles.of_this_process(), "main")


class Linking(Home):
    def test_init_links_and_drops_identity(self):
        self.quiet(profiles.init, "work")
        work = self.home / ".claude-work"
        for entry in ("projects", "skills", "CLAUDE.md", "settings.json"):
            self.assertTrue((work / entry).is_symlink(), entry)
            self.assertEqual((work / entry).resolve(), (self.main / entry).resolve())
        self.assertFalse((work / "agents").exists())  # absent from the main profile: not linked
        own = json.loads((work / ".claude.json").read_text())
        self.assertNotIn("oauthAccount", own)
        self.assertIn("/work", own["projects"])
        self.assertEqual((work / ".claude.json").stat().st_mode & 0o777, 0o600)

    def test_sync_adopts_merges_and_sets_aside(self):
        self.quiet(profiles.init, "work")
        work = self.home / ".claude-work"
        (work / "settings.json").unlink()
        (work / "settings.json").write_text('{"theme": "light"}\n')  # a copy that drifted
        (work / "skills").unlink()
        (work / "skills" / "mine").mkdir(parents=True)                # a real dir with its own content
        (work / "agents").mkdir()                                     # unknown to the main profile
        (work / "agents" / "a.md").write_text("agent")
        with redirect_stdout(io.StringIO()), mock.patch("sys.stderr", io.StringIO()):
            profiles.sync("work")
        self.assertTrue((work / "settings.json").is_symlink())
        self.assertEqual(json.loads((self.main / "settings.json").read_text())["theme"], "dark")
        self.assertEqual(len(list(work.glob("settings.json.replaced-*"))), 1)
        self.assertTrue((self.main / "skills" / "mine").is_dir())
        self.assertTrue((self.main / "agents" / "a.md").exists())
        self.assertTrue((work / "agents").is_symlink())
        out = io.StringIO()
        with redirect_stdout(out), mock.patch("sys.stderr", io.StringIO()):
            profiles.doctor("work")
        self.assertNotIn("✗", out.getvalue())

    def test_mcp_servers_reconciled_main_wins(self):
        self.quiet(profiles.init, "work")
        work = self.home / ".claude-work" / ".claude.json"
        main = json.loads((self.home / ".claude.json").read_text())
        main["mcpServers"] = {"a": {"command": "main"}, "b": {"command": "b"}}
        self.write_json(self.home / ".claude.json", main)
        own = json.loads(work.read_text())
        own["mcpServers"] = {"a": {"command": "work"}, "c": {"command": "c"}}
        own["projects"]["/p"] = {"mcpServers": {"d": {"command": "d"}}}
        self.write_json(work, own)
        moves = profiles.reconcile_mcp(work.parent)
        for path in (self.home / ".claude.json", work):
            data = json.loads(path.read_text())
            self.assertEqual(set(data["mcpServers"]), {"a", "b", "c"})
            self.assertEqual(data["mcpServers"]["a"]["command"], "main")
            self.assertIn("d", data["projects"]["/p"]["mcpServers"])
        self.assertIn("c→main", moves)
        self.assertIn("b→work", moves)

    def test_trust_propagates(self):
        self.quiet(profiles.init, "work")
        work = self.home / ".claude-work"
        own = json.loads((work / ".claude.json").read_text())
        own["projects"] = {}
        self.write_json(work / ".claude.json", own)
        profiles.propagate_trust(work, "/work")
        self.assertTrue(json.loads((work / ".claude.json").read_text())
                        ["projects"]["/work"]["hasTrustDialogAccepted"])


class Launching(Home):
    def test_kept_options(self):
        self.assertEqual(
            launch.kept_options(["--model", "opus", "fix it", "--resume", "x", "--verbose", "--add-dir"]),
            ["--model", "opus", "--verbose", "--add-dir"])

    def test_run_relaunches_on_the_marked_profile(self):
        self.quiet(profiles.init, "work")
        (self.main / "projects" / "p").mkdir()
        (self.main / "projects" / "p" / "sid-1.jsonl").write_text("{}\n")
        calls = self.home / "calls"
        fake = self.home / "fake-claude"
        # The first launch leaves the marker `switch` would write, the second one does not.
        fake.write_text(f"""#!/bin/sh
echo "${{CLAUDE_CONFIG_DIR:-none}} $CLAUDE_PROFILE $*" >> {calls}
marker="$HOME/.local/state/claude-helper/relaunch/$CLAUDE_HELPER_LAUNCHER_PID"
[ -e {calls}.once ] || {{ touch {calls}.once; printf 'work\\nsid-1\\n' > "$marker"; }}
""")
        fake.chmod(0o755)
        self.configure(f'claude_bin = "{fake}"\n')
        previous = signal.getsignal(signal.SIGINT)
        try:
            with redirect_stdout(io.StringIO()):
                code = launch.run("main", ["--model", "opus", "hello"])
        finally:
            signal.signal(signal.SIGINT, previous)
        self.assertEqual(code, 0)
        self.assertEqual(calls.read_text().splitlines(), [
            "none main --model opus hello",
            f"{self.home / '.claude-work'} work --model opus --resume sid-1",
        ])


class Sessions(Home):
    def transcript(self, project, sid, *entries):
        path = self.main / "projects" / project / f"{sid}.jsonl"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("".join(json.dumps(e, separators=(",", ":")) + "\n" for e in entries))
        return path

    def test_typed_text_ignores_injections(self):
        self.assertIsNone(sessions.typed_text({"type": "user", "message": {"content": "<system-reminder>x"}}))
        self.assertEqual(sessions.typed_text({"type": "user", "message": {
            "content": "<system-reminder>a</system-reminder> deploy now"}}), "deploy now")

    def test_search_and_names(self):
        self.transcript("-work", "s1",
                        {"type": "user", "cwd": "/work", "message": {"content": "migrate the datastore"}},
                        {"type": "custom-title", "customTitle": "first"},
                        {"type": "custom-title", "customTitle": "datastore"})
        self.transcript("-other", "s2",
                        {"type": "user", "cwd": "/other", "message": {"content": "hello"}},
                        {"type": "tool_result", "content": "datastore"})
        files = sessions.transcripts()
        self.assertEqual(sessions.names(files)[str(files[0].parent.parent / "-work" / "s1.jsonl")], "datastore")
        out = io.StringIO()
        with redirect_stdout(out):
            sessions.search("datastore", 10, False)
        self.assertIn("1 conversation(s)", out.getvalue())
        self.assertIn("claude --resume s1", out.getvalue())
        out = io.StringIO()
        with redirect_stdout(out):
            sessions.by_name("datastore", 10, False)
        self.assertIn("1 session(s)", out.getvalue())

    def test_project_folder_name(self):
        self.assertEqual(sessions.project_of("/home/a/.claude").name, "-home-a--claude")


class Md(Home):
    def test_cascade_order(self):
        work = self.home / "w" / "repo"
        (work / ".claude").mkdir(parents=True)
        (self.home / "w" / "CLAUDE.md").write_text("parent")
        (work / ".claude" / "CLAUDE.md").write_text("repo")
        (work / "CLAUDE.local.md").write_text("local")
        self.assertEqual(md.cascade_of(work), [
            (self.main / "CLAUDE.md").resolve(), (self.home / "w" / "CLAUDE.md").resolve(),
            (work / ".claude" / "CLAUDE.md").resolve(), (work / "CLAUDE.local.md").resolve()])

    def test_roots_come_from_config(self):
        self.configure(f'md_roots = ["{self.home / "w"}"]\n')
        (self.home / "w" / "a").mkdir(parents=True)
        (self.home / "w" / "a" / "CLAUDE.md").write_text("x")
        (self.home / "outside").mkdir()
        (self.home / "outside" / "CLAUDE.md").write_text("y")
        self.assertEqual(md.all_files(), [(self.home / "w" / "a" / "CLAUDE.md").resolve()])


class Browser(Home):
    def test_only_web_urls(self):
        self.assertEqual(browser.checked(["https://claude.ai/x"]), ["https://claude.ai/x"])
        for bad in ("--renderer-cmd-prefix=/bin/sh", "file:///etc/passwd", "javascript:alert(1)"):
            with self.assertRaises(SystemExit), mock.patch("sys.stderr", io.StringIO()):
                browser.checked([bad])


if __name__ == "__main__":
    unittest.main()
