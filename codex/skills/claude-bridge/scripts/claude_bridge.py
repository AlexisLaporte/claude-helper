#!/usr/bin/env python3
"""Persistent on-demand Codex peer for Claude Code's Linux Unix socket protocol."""
import argparse
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import select
import signal
import socket
import stat
import struct
import subprocess
import sys
import time
import uuid

from notifications import InboxNotifier

LIMIT = 1024 * 1024


def emit(value):
    print(json.dumps(value, ensure_ascii=False), flush=True)


def private_dir(path):
    path.mkdir(parents=True, exist_ok=True, mode=0o700)
    info = path.lstat()
    if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid():
        raise RuntimeError(f"Not an owned directory: {path}")
    path.chmod(0o700)


def identity(connection, expected_pid=None):
    pid, uid, _ = struct.unpack("3i", connection.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, 12))
    if uid != os.getuid() or (expected_pid is not None and pid != expected_pid):
        raise RuntimeError(f"Unexpected peer identity: pid={pid}, uid={uid}")
    return pid


def check_socket(path):
    info = path.lstat()
    if not stat.S_ISSOCK(info.st_mode) or info.st_uid != os.getuid():
        raise RuntimeError(f"Not an owned socket: {path}")


def line_read(connection):
    data = b""
    while b"\n" not in data:
        chunk = connection.recv(65536)
        if not chunk:
            raise RuntimeError("Connection closed before a complete JSON line")
        data += chunk
        if len(data) > LIMIT:
            raise RuntimeError("Frame exceeds 1 MiB")
    return json.loads(data.split(b"\n", 1)[0])


def write_frame(connection, frame):
    data = (json.dumps(frame, ensure_ascii=False) + "\n").encode()
    if len(data) > LIMIT:
        raise RuntimeError("Frame exceeds 1 MiB")
    connection.sendall(data)


def registry_paths(explicit=None):
    if explicit is not None:
        return [Path(explicit).expanduser().resolve()]
    home = Path.home()
    candidates = [home / ".claude" / "sessions"]
    candidates += [path / "sessions" for path in sorted(home.iterdir())
                   if path.name.startswith(".claude-") and path.is_dir()]
    if os.environ.get("CLAUDE_CONFIG_DIR"):
        candidates.append(Path(os.environ["CLAUDE_CONFIG_DIR"]).expanduser() / "sessions")
    return list(dict.fromkeys(path.resolve() for path in candidates))


def records(registry=None):
    result, seen = [], set()
    for directory in registry_paths(registry):
        try:
            paths = sorted(directory.iterdir())
        except FileNotFoundError:
            continue
        for path in paths:
            if not re.fullmatch(r"[1-9][0-9]*\.json", path.name):
                continue
            entry = json.loads(path.read_text(encoding="utf-8"))
            if entry.get("pid") != int(path.stem):
                raise RuntimeError(f"Registry PID mismatch: {path}")
            endpoint = entry.get("messagingSocketPath")
            key = (str(Path(endpoint).resolve()) if isinstance(endpoint, str) else None,
                   entry.get("sessionId"))
            if key[0] is not None and key in seen:
                continue
            seen.add(key)
            result.append({**{key: entry.get(key) for key in (
                "pid", "sessionId", "name", "cwd", "status", "version", "messagingSocketPath")},
                "profile": directory.parent.name, "registry": str(directory)})
    return result


def resolve(registry, target):
    available = records(registry)
    matches = [item for item in available if target == item["sessionId"]]
    if not matches:
        matches = [item for item in available if target == item["name"]]
    if len(matches) != 1:
        raise RuntimeError(f"Expected one registered session for {target!r}; found {len(matches)}. "
                           "Use a unique sessionId or select --registry-dir explicitly.")
    peer = matches[0]
    path = peer["messagingSocketPath"]
    if not isinstance(path, str) or not Path(path).is_absolute():
        raise RuntimeError("Session has no absolute messaging socket")
    return peer


def event_list(folder, after=0):
    path = folder / "inbox.jsonl"
    if not path.exists():
        return [], 0
    events, cursor = [], 0
    with path.open(encoding="utf-8") as stream:
        for line in stream:
            if not line.endswith("\n"):
                break  # A concurrent append is not yet a complete event.
            value = json.loads(line)
            cursor = value["cursor"]
            if cursor > after:
                events.append(value)
    return events, cursor


def rpc(folder, request):
    path = folder / "control.sock"
    check_socket(path)
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as connection:
        connection.settimeout(10)
        connection.connect(str(path))
        identity(connection)
        write_frame(connection, request)
        response = line_read(connection)
    if "error" in response:
        raise RuntimeError(response["error"])
    return response


def bind(path):
    server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    server.bind(str(path))
    path.chmod(0o600)
    server.listen(16)
    return server


class Daemon:
    def __init__(self, args, folder):
        self.args, self.folder = args, folder
        events, self.cursor = event_list(folder)
        self.running, self.peers, self.outstanding = True, {}, set()
        self.contacts = set()
        for event in events:
            if event.get("kind") == "sent":
                self.remember_contact(event.get("to", {}))
        self.path = args.socket_dir / f"{os.getpid()}.sock"
        self.address = f"uds:{self.path}"
        self.notifier = InboxNotifier(folder, getattr(args, "notify_thread", None),
                                      args.channel, events, self.append)

    def remember_contact(self, peer):
        session, registry = peer.get("sessionId"), peer.get("registry")
        if isinstance(session, str) and session and isinstance(registry, str) and registry:
            self.contacts.add((str(Path(registry).resolve()), session))

    def refresh_peer(self, path, pid):
        # Only a successful authenticated send grants a stable identity permission.
        if not Path(path).is_absolute():
            return False
        for registry in sorted({registry for registry, _ in self.contacts}):
            matches = [peer for peer in records(registry)
                       if (registry, peer["sessionId"]) in self.contacts
                       and peer["messagingSocketPath"] == path and peer["pid"] == pid]
            if len(matches) != 1:
                continue
            check_socket(Path(path))
            self.peers[path] = pid
            return True
        return False

    def append(self, kind, **fields):
        self.cursor += 1
        event = {"cursor": self.cursor, "time": time.time(), "kind": kind, **fields}
        with (self.folder / "inbox.jsonl").open("a", encoding="utf-8") as stream:
            stream.write(json.dumps(event, ensure_ascii=False) + "\n")
            stream.flush()
            os.fsync(stream.fileno())
        return event

    def transmit(self, path, pid, frame):
        check_socket(Path(path))
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as connection:
            connection.settimeout(5)
            connection.connect(path)
            identity(connection, pid)
            write_frame(connection, frame)
            connection.shutdown(socket.SHUT_WR)

    def status(self):
        return {"running": True, "pid": os.getpid(), "name": self.args.name,
                "channel": self.args.channel, "reply_address": self.address,
                "registry_dir": str(self.args.registry_dir) if self.args.registry_dir else None,
                "notify_thread": self.notifier.thread,
                "cursor": self.cursor, "inbox": str(self.folder / "inbox.jsonl")}

    def command(self, request):
        action = request.get("action")
        if action == "status":
            return self.status()
        if action == "ack_notification":
            cursor = request.get("cursor")
            if type(cursor) is not int or not 0 <= cursor <= self.cursor:
                raise RuntimeError("Invalid inbox read cursor")
            notification = request.get("notification")
            if type(notification) is not int or notification <= 0:
                raise RuntimeError("Invalid notification cursor")
            return {"acknowledged": self.notifier.acknowledge(notification, cursor)}
        if action == "stop":
            self.running = False
            return {"stopping": True, "cursor": self.cursor}
        if action != "send":
            raise RuntimeError("Unknown control action")
        message = request.get("message")
        if not isinstance(message, str) or not message.strip():
            raise RuntimeError("Message must be nonempty text")
        peer = resolve(request.get("registry_dir") or self.args.registry_dir, request["to"])
        path, pid = peer["messagingSocketPath"], peer["pid"]
        cursor, msg_id = self.cursor, str(uuid.uuid4())
        body = (message + "\n\n[Message from Codex. To answer me, use "
                f"SendMessage with to=\"{self.address}\"; a normal reply stays local.]")
        body = re.sub(r"</(?=cross-session-message(?:[>\s/]|$))", r"<\\/", body, flags=re.I)
        frame = {"msgV": 1, "msg_id": msg_id, "type": "user", "priority": "next",
                 "from": self.address, "session_id": peer["sessionId"],
                 "message": {"role": "user", "content":
                     f'<cross-session-message from="{self.address}" from-name="{self.args.name}">\n'
                     f"{body}\n</cross-session-message>"}}
        self.transmit(path, pid, frame)
        self.peers[path] = pid
        self.remember_contact(peer)
        self.outstanding.add(msg_id)
        self.append("sent", msg_id=msg_id, to=peer, message=message)
        return {"sent": True, "delivery_confirmed": False, "msg_id": msg_id,
                "cursor_before_send": cursor, "reply_address": self.address}

    def receive(self, connection):
        pid = identity(connection)
        frame = line_read(connection)
        if not isinstance(frame, dict):
            raise RuntimeError("Native frame must be an object")
        if frame.get("type") == "auth":
            raise RuntimeError("Unexpected auth preamble: this peer has no published token")
        address = frame.get("from", "")
        if not isinstance(address, str) or not address.startswith("uds:"):
            raise RuntimeError("Native frame lacks a uds sender address")
        path = address[4:]
        if self.peers.get(path) != pid and not self.refresh_peer(path, pid):
            raise RuntimeError(f"Sender address does not match a contacted peer (pid={pid})")
        if frame.get("msgV") != 1:
            raise RuntimeError("Unsupported native protocol version")
        if frame.get("type") == "control":
            if frame.get("action") != "peer_message_status":
                raise RuntimeError("Unsupported native control action")
            if frame.get("orig_msg_id") not in self.outstanding:
                raise RuntimeError("Uncorrelated delivery receipt")
            status = frame.get("status")
            if status not in ("delivered", "held", "denied", "expired", "refused", "dropped"):
                raise RuntimeError("Unknown delivery status")
            self.append("receipt", status=status, frame=frame)
            return
        if frame.get("type") != "user":
            raise RuntimeError("Unsupported native frame type")
        content = frame.get("message", {}).get("content")
        if not isinstance(content, str) or not content:
            raise RuntimeError("Missing native message text")
        event = self.append("message", from_address=address, content=content, frame=frame)
        self.notifier.consider(event)
        self.transmit(path, pid, {"msgV": 1, "msg_id": str(uuid.uuid4()), "type": "control",
                      "action": "peer_message_status", "status": "delivered",
                      "orig_msg_id": frame.get("msg_id"), "from": self.address})

    def run(self):
        os.umask(0o077)
        private_dir(self.args.socket_dir)
        lock = (self.folder / "daemon.lock").open("a")
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        paths, servers = [], []
        try:
            for path in (self.path, self.folder / "control.sock"):
                server = bind(path)
                servers.append(server)
                info = path.lstat()
                paths.append((path, info.st_dev, info.st_ino))
            native, control = servers
            (self.folder / "daemon.json").write_text(json.dumps(self.status()), encoding="utf-8")
            signal.signal(signal.SIGTERM, lambda *_: setattr(self, "running", False))
            signal.signal(signal.SIGINT, lambda *_: setattr(self, "running", False))
            self.notifier.resume()
            while self.running:
                for server in select.select(servers, [], [], 0.5)[0]:
                    connection, _ = server.accept()
                    with connection:
                        connection.settimeout(5)
                        try:
                            identity(connection)
                            if server is native:
                                self.receive(connection)
                            else:
                                write_frame(connection, self.command(line_read(connection)))
                        except Exception as error:
                            self.append("error", error=str(error), source="native" if server is native else "control")
                            if server is control:
                                write_frame(connection, {"error": str(error)})
        finally:
            for server in servers:
                server.close()
            for path, device, inode in paths:
                if path.exists():
                    info = path.lstat()
                    if (info.st_dev, info.st_ino) == (device, inode):
                        path.unlink()
            lock.close()


def recover_stale_control(folder):
    with (folder / "daemon.lock").open("a") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise RuntimeError("Daemon lock is held; refusing to remove its control socket") from None
        path = folder / "control.sock"
        check_socket(path)
        before = path.lstat()
        current = path.lstat()
        if (before.st_dev, before.st_ino) != (current.st_dev, current.st_ino):
            raise RuntimeError("Control socket changed during recovery")
        path.unlink()
        event = {"cursor": event_list(folder)[1] + 1, "time": time.time(),
                 "kind": "recovery", "detail": "Removed stale private control socket after daemon exit"}
        with (folder / "inbox.jsonl").open("a", encoding="utf-8") as stream:
            stream.write(json.dumps(event) + "\n")
            stream.flush()
            os.fsync(stream.fileno())


def start(args, folder):
    with (folder / "start.lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        if (folder / "control.sock").exists():
            try:
                result = rpc(folder, {"action": "status"})
            except ConnectionRefusedError:
                recover_stale_control(folder)
            else:
                if result["name"] != args.name:
                    raise RuntimeError("Channel already running under another name")
                scope = str(args.registry_dir) if args.registry_dir else None
                if "registry_dir" not in result or result["registry_dir"] != scope:
                    raise RuntimeError("Running daemon has a different registry scope or older code; stop/start it")
                if result.get("notify_thread") != args.notify_thread:
                    raise RuntimeError("Running daemon has another notification target; stop/start it")
                return result
        command = [sys.executable, str(Path(__file__).resolve()), "--channel", args.channel,
                   "--state-dir", str(args.state_dir), "--socket-dir", str(args.socket_dir)]
        if args.registry_dir is not None:
            command += ["--registry-dir", str(args.registry_dir)]
        command += ["_daemon", "--name", args.name]
        if args.notify_thread:
            command += ["--notify-thread", args.notify_thread]
        with (folder / "daemon.log").open("a", encoding="utf-8") as log:
            child = subprocess.Popen(command, stdin=subprocess.DEVNULL, stdout=log, stderr=log,
                                     start_new_session=True, close_fds=True)
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            if child.poll() is not None:
                raise RuntimeError(f"Daemon failed; inspect {folder / 'daemon.log'}")
            if (folder / "control.sock").exists():
                return rpc(folder, {"action": "status"})
            time.sleep(0.05)
        child.terminate()
        raise RuntimeError("Daemon startup timed out")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--channel")
    parser.add_argument("--state-dir", type=Path, default=Path.home() / ".local/state/codex-claude")
    parser.add_argument("--registry-dir", type=Path,
                        help="Use only this sessions directory; default discovers all local Claude profiles")
    runtime = Path(os.environ.get("XDG_RUNTIME_DIR", f"/run/user/{os.getuid()}"))
    parser.add_argument("--socket-dir", type=Path, default=runtime / "cc-socks")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("list")
    for command in ("start", "_daemon"):
        listener = sub.add_parser(command)
        listener.add_argument("--name", required=True)
        listener.add_argument("--notify-thread", type=lambda v: str(uuid.UUID(v)),
                              help="Opt in to coalesced Codex queue notifications for this UUID")
    sender = sub.add_parser("send")
    sender.add_argument("--to", required=True)
    sender.add_argument("--message-file", type=Path, required=True)
    inbox = sub.add_parser("inbox")
    inbox.add_argument("--after", type=int, default=0)
    inbox.add_argument("--wait", type=float, default=0)
    inbox.add_argument("--ack-notification", type=int,
                       help="Only when processing the queued notice: acknowledge its message cursor")
    sub.add_parser("status")
    sub.add_parser("stop")
    args = parser.parse_args()
    if args.registry_dir is not None:
        args.registry_dir = args.registry_dir.expanduser().resolve()
    if args.command == "list":
        emit({"sessions": records(args.registry_dir), "note": "Registry presence is not proof of reachability"})
        return
    if not args.channel or len(args.channel) > 128:
        parser.error("--channel is required, at most 128 characters")
    folder = args.state_dir / hashlib.sha256(args.channel.encode()).hexdigest()[:16]
    if args.command in ("start", "_daemon"):
        if not re.fullmatch(r"Codex[ A-Za-z0-9_.-]{1,70}", args.name):
            parser.error("--name must start with Codex and use ASCII letters, numbers, spaces, . _ -")
        os.umask(0o077)
        private_dir(args.state_dir)
        private_dir(folder)
        if args.command == "_daemon":
            Daemon(args, folder).run()
        else:
            emit(start(args, folder))
    elif args.command == "inbox":
        if args.ack_notification is not None and args.ack_notification <= 0:
            parser.error("--ack-notification must be a positive cursor")
        if args.after < 0 or not 0 <= args.wait <= 45:
            parser.error("--after must be nonnegative and --wait between 0 and 45")
        deadline = time.monotonic() + args.wait
        while True:
            events, cursor = event_list(folder, args.after)
            if events or time.monotonic() >= deadline:
                emit({"events": events, "next_cursor": cursor})
                if args.ack_notification is not None:
                    try:
                        rpc(folder, {"action": "ack_notification", "cursor": cursor,
                                     "notification": args.ack_notification})
                    except Exception as error:
                        print(f"Inbox read; notification acknowledgement failed: {error}", file=sys.stderr)
                break
            time.sleep(min(0.2, max(0, deadline - time.monotonic())))
    elif args.command == "status" and not (folder / "control.sock").exists():
        emit({"running": False, "channel": args.channel, "cursor": event_list(folder)[1]})
    elif args.command == "send":
        emit(rpc(folder, {"action": "send", "to": args.to,
                          "registry_dir": str(args.registry_dir.resolve()) if args.registry_dir else None,
                          "message": args.message_file.read_text(encoding="utf-8")}))
    else:
        emit(rpc(folder, {"action": args.command}))


if __name__ == "__main__":
    try:
        main()
    except Exception as error:
        emit({"error": str(error)})
        sys.exit(1)
