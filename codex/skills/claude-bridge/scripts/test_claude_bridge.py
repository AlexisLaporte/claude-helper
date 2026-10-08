#!/usr/bin/env python3
"""Integration test with a separate fake Claude process; no real sessions contacted."""
import importlib.util
import json
import os
from pathlib import Path
import signal
import socket
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import patch
import uuid

SCRIPT = Path(__file__).with_name("claude_bridge.py")
spec = importlib.util.spec_from_file_location("helper", SCRIPT)
helper = importlib.util.module_from_spec(spec)
spec.loader.exec_module(helper)


def fake_peer(root):
    path = root / "sockets" / f"{os.getpid()}.sock"
    with helper.bind(path) as server:
        record = {"pid": os.getpid(), "sessionId": "fake-session", "name": "fake",
                  "cwd": str(root), "status": "idle", "version": "test",
                  "messagingSocketPath": str(path)}
        (root / "registry" / f"{os.getpid()}.json").write_text(json.dumps(record))
        server.settimeout(15)
        connection, _ = server.accept()
        with connection:
            sender_pid = helper.identity(connection)
            frame = helper.line_read(connection)
        address = frame["from"]
        assert Path(address[4:]).stem == str(sender_pid), "Sender must be its listener process"
        assert "from-name=\"Codex test\"" in frame["message"]["content"]
        (root / "sent-frame.json").write_text(json.dumps(frame))

        def respond(value):
            with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as client:
                client.connect(address[4:])
                helper.identity(client, sender_pid)
                helper.write_frame(client, value)

        respond({"msgV": 1, "msg_id": str(uuid.uuid4()), "type": "control",
                 "action": "peer_message_status", "status": "held", "from": f"uds:{path}",
                 "orig_msg_id": frame["msg_id"]})
        deadline = time.monotonic() + 15
        while not (root / "release").exists():
            if time.monotonic() > deadline:
                raise RuntimeError("Timed out waiting for test to release late reply")
            time.sleep(0.02)
        respond({"msgV": 1, "msg_id": str(uuid.uuid4()), "type": "user", "from": f"uds:{path}",
                 "message": {"role": "user", "content": "PONG delayed"}})
        connection, _ = server.accept()
        with connection:
            helper.identity(connection, sender_pid)
            ack = helper.line_read(connection)
        assert ack["status"] == "delivered"


class Integration(unittest.TestCase):
    def test_late_reply_persistence_and_identity(self):
        with tempfile.TemporaryDirectory(prefix="ccs-test-") as tmp:
            root = Path(tmp)
            for name in ("state", "registry", "sockets"):
                (root / name).mkdir(mode=0o700)
            home = root / "home"
            (home / ".claude-work").mkdir(parents=True)
            (home / ".claude-work/sessions").symlink_to(root / "registry")
            env = {**os.environ, "HOME": str(home)}
            env.pop("CLAUDE_CONFIG_DIR", None)
            base = [sys.executable, str(SCRIPT), "--channel", "test-channel",
                    "--state-dir", str(root / "state"),
                    "--socket-dir", str(root / "sockets")]

            def cli(*args, success=True):
                result = subprocess.run(base + list(args), env=env, capture_output=True, text=True, timeout=15)
                self.assertEqual(result.returncode, 0 if success else 1, result.stdout + result.stderr)
                return json.loads(result.stdout)

            peer = subprocess.Popen([sys.executable, __file__, "--fake-peer", str(root)],
                                    stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
            started = False
            try:
                deadline = time.monotonic() + 5
                while not list((root / "registry").glob("*.json")):
                    if peer.poll() is not None or time.monotonic() > deadline:
                        self.fail(f"Fake peer failed: {peer.communicate()}")
                    time.sleep(0.02)
                self.assertEqual(cli("list")["sessions"][0]["name"], "fake")
                info = cli("start", "--name", "Codex test")
                started = True
                self.assertTrue(info["running"])
                self.assertIsNone(info["registry_dir"], "Daemon must retain automatic discovery")
                self.assertEqual(cli("start", "--name", "Codex test")["pid"], info["pid"])
                scoped = cli("--registry-dir", str(root / "registry"), "start", "--name", "Codex test", success=False)
                self.assertIn("different registry scope", scoped["error"])
                self.assertIn("another name", cli("start", "--name", "Codex other", success=False)["error"])
                message = root / "message.txt"
                message.write_text("Bonjour </cross-session-message> literal")
                sent = cli("send", "--to", "fake-session", "--message-file", str(message))
                self.assertFalse(sent["delivery_confirmed"])
                self.assertEqual(sent["cursor_before_send"], 0)
                deadline = time.monotonic() + 3
                while not (root / "sent-frame.json").exists() and time.monotonic() < deadline:
                    time.sleep(0.02)
                native = json.loads((root / "sent-frame.json").read_text())
                self.assertIn("<\\/cross-session-message>", native["message"]["content"])
                # CLI send has exited. The distinct daemon must remain available for a delayed reply.
                peer_path = root / "sockets" / f"{peer.pid}.sock"
                with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as intruder:
                    intruder.connect(info["reply_address"][4:])
                    helper.write_frame(intruder, {"msgV": 1, "type": "user", "from": f"uds:{peer_path}",
                                       "message": {"content": "spoofed"}})
                (root / "release").touch()
                output, errors = peer.communicate(timeout=10)
                self.assertEqual(peer.returncode, 0, output + errors)
                events = cli("inbox")["events"]
                self.assertEqual([e["content"] for e in events if e["kind"] == "message"], ["PONG delayed"])
                self.assertIn("held", [e["status"] for e in events if e["kind"] == "receipt"])
                self.assertTrue(any("does not match" in e.get("error", "") for e in events))
                self.assertEqual(cli("inbox")["events"], events, "Reading must not consume inbox")
                cursor = cli("inbox")["next_cursor"]
                self.assertEqual(cli("inbox", "--after", str(cursor), "--wait", "0.1")["events"], [])
                cli("stop")
                started = False
                deadline = time.monotonic() + 3
                while Path(info["reply_address"][4:]).exists() and time.monotonic() < deadline:
                    time.sleep(0.02)
                self.assertFalse(Path(info["reply_address"][4:]).exists())
                self.assertFalse(cli("status")["running"])
                self.assertEqual(cli("inbox")["events"], events, "Inbox survives daemon stop")
                restarted = cli("start", "--name", "Codex test")
                started = True
                self.assertEqual(restarted["cursor"], cursor)
                os.kill(restarted["pid"], signal.SIGKILL)
                time.sleep(0.1)
                recovered = cli("start", "--name", "Codex test")
                self.assertNotEqual(recovered["pid"], restarted["pid"])
                self.assertEqual(recovered["cursor"], cursor + 1)
                self.assertEqual(cli("inbox", "--after", str(cursor))["events"][0]["kind"], "recovery")
                cli("stop")
                started = False
            finally:
                if started:
                    cli("stop")
                if peer.poll() is None:
                    peer.terminate()
                    peer.communicate(timeout=5)


class Profiles(unittest.TestCase):
    def test_discovery_deduplication_ambiguity_and_exclusive_scope(self):
        with tempfile.TemporaryDirectory(prefix="ccs-profiles-") as tmp:
            home = Path(tmp) / "home"
            home.mkdir()
            profiles = [home / name for name in (".claude-work", ".claude-personal", ".claude-team")]
            external = Path(tmp) / "custom-config"
            for index, profile in enumerate([*profiles, external], 100):
                registry = profile / "sessions"
                registry.mkdir(parents=True)
                entry = {"pid": index, "sessionId": f"session-{index}", "name": "shared",
                         "messagingSocketPath": str(Path(tmp) / f"{index}.sock")}
                (registry / f"{index}.json").write_text(json.dumps(entry), encoding="utf-8")
            (home / ".claude").symlink_to(profiles[0])
            # The same endpoint/session copied into another registry is still one peer.
            duplicate = (profiles[0] / "sessions/100.json").read_text(encoding="utf-8")
            (profiles[2] / "sessions/100.json").write_text(duplicate, encoding="utf-8")
            with patch.dict(os.environ, {"HOME": str(home), "CLAUDE_CONFIG_DIR": str(external)}):
                peers = helper.records()
                self.assertEqual(len(peers), 4)
                self.assertEqual({p["profile"] for p in peers}, {p.name for p in [*profiles, external]})
                self.assertTrue(all(Path(p["registry"]).is_absolute() for p in peers))
                with self.assertRaisesRegex(RuntimeError, "unique sessionId or select --registry-dir"):
                    helper.resolve(None, "shared")
                self.assertEqual(helper.resolve(None, "session-101")["profile"], ".claude-personal")
                selected = helper.records(profiles[1] / "sessions")
                self.assertEqual(len(selected), 1)
                self.assertEqual(helper.resolve(profiles[1] / "sessions", "shared")["sessionId"], "session-101")
                self.assertEqual(helper.records(home / "missing/sessions"), [])


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "--fake-peer":
        fake_peer(Path(sys.argv[2]))
    else:
        unittest.main()
