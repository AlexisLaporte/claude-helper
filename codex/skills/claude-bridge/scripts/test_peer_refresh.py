"""Real Unix peer credentials and disposable peers only; no live sessions."""
import importlib.util
import json
import multiprocessing
import os
from pathlib import Path
import socket
import tempfile
from types import SimpleNamespace
import unittest

spec = importlib.util.spec_from_file_location('helper', Path(__file__).with_name('claude_bridge.py'))
helper = importlib.util.module_from_spec(spec)
spec.loader.exec_module(helper)


def peer_process(root, registry, session, listener, contacted, pipe, claimed=None, wrong_pid=False):
    path = Path(root) / f'{os.getpid()}.sock'
    pid = os.getpid() + 100000 if wrong_pid else os.getpid()
    entry = {'pid': pid, 'sessionId': session, 'name': 'fake',
             'messagingSocketPath': str(path)}
    record = Path(registry) / f'{pid}.json'
    with helper.bind(path) as server:
        server.settimeout(5)
        record.write_text(json.dumps(entry))
        pipe.send(os.getpid())
        if contacted:
            connection, _ = server.accept()
            with connection:
                helper.line_read(connection)
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as connection:
            connection.connect(listener)
            helper.write_frame(connection, {'msgV': 1, 'msg_id': 'test-reply', 'type': 'user',
                'from': f'uds:{claimed or path}', 'session_id': 'original',
                'message': {'content': f'reply {os.getpid()}'}})
        connection, _ = server.accept()
        with connection:
            ack = helper.line_read(connection)
            pipe.send(ack['status'])
    record.unlink()
    path.unlink()


class PeerRefresh(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix='ccs-peer-test-')
        self.root = Path(self.tmp.name)
        self.registry = self.root / 'registry'
        self.registry.mkdir()
        self.folder = self.root / 'state'
        self.folder.mkdir()
        self.args = SimpleNamespace(socket_dir=self.root, registry_dir=self.registry,
                                    name='test', channel='test')
        self.daemon = helper.Daemon(self.args, self.folder)
        self.listener = helper.bind(self.daemon.path)
        self.listener.settimeout(5)
        self.processes = []
        self.pipes = []

    def tearDown(self):
        for process in self.processes:
            if process.is_alive():
                process.terminate()
            process.join(5)
        for pipe in self.pipes:
            pipe.close()
        self.listener.close()
        self.tmp.cleanup()

    def start_peer(self, session='original', contacted=False, registry=None, **kwargs):
        parent, child = multiprocessing.Pipe()
        process = multiprocessing.Process(target=peer_process, args=(
            str(self.root), str(registry or self.registry), session,
            str(self.daemon.path), contacted, child), kwargs=kwargs)
        process.start()
        child.close()
        self.processes.append(process)
        self.pipes.append(parent)
        self.assertTrue(parent.poll(5), 'peer failed to initialize')
        pid = parent.recv()
        return process, parent, pid

    def receive(self):
        connection, _ = self.listener.accept()
        with connection:
            self.daemon.receive(connection)

    def contact_original(self):
        process, pipe, pid = self.start_peer(contacted=True)
        self.daemon.command({'action': 'send', 'to': 'original', 'message': 'test'})
        self.receive()
        self.assertTrue(pipe.poll(5))
        self.assertEqual(pipe.recv(), 'delivered')
        process.join(5)
        self.assertEqual(process.exitcode, 0)
        return pid

    def assert_rejected(self, **kwargs):
        before = helper.event_list(self.folder)[1]
        self.start_peer(**kwargs)
        with self.assertRaisesRegex(RuntimeError, 'does not match a contacted peer'):
            self.receive()
        self.assertEqual(helper.event_list(self.folder)[1], before)

    def test_restarted_contact_new_pid_and_socket(self):
        old_pid = self.contact_original()
        process, pipe, new_pid = self.start_peer()
        self.assertNotEqual(old_pid, new_pid)
        self.receive()
        self.assertTrue(pipe.poll(5))
        self.assertEqual(pipe.recv(), 'delivered')
        process.join(5)
        self.assertEqual(process.exitcode, 0)
        self.assertEqual(self.daemon.peers[str(self.root / f'{new_pid}.sock')], new_pid)

    def test_listener_restart_restores_only_successful_sent_identity(self):
        self.contact_original()
        self.daemon = helper.Daemon(self.args, self.folder)
        self.assertFalse(self.daemon.peers)
        process, pipe, _ = self.start_peer()
        self.receive()
        self.assertTrue(pipe.poll(5))
        self.assertEqual(pipe.recv(), 'delivered')
        process.join(5)
        self.assertEqual(process.exitcode, 0)

    def test_uncontacted_identity_even_with_forged_frame_session_id(self):
        self.contact_original()
        self.assert_rejected(session='uncontacted')

    def test_matching_session_in_different_registry_is_not_authorized(self):
        self.contact_original()
        other = self.root / 'other-registry'
        other.mkdir()
        self.assert_rejected(registry=other)

    def test_claimed_socket_must_match_registered_endpoint(self):
        self.contact_original()
        self.assert_rejected(claimed=str(self.root / 'other.sock'))

    def test_registered_pid_must_match_real_incoming_credentials(self):
        self.contact_original()
        self.assert_rejected(wrong_pid=True)

    def test_no_successful_send_grants_no_contact(self):
        self.assert_rejected()

    def test_regular_file_cannot_be_refreshed_as_socket(self):
        self.contact_original()
        fake = self.root / 'fake.sock'
        fake.write_text('not a socket')
        record = self.registry / f'{os.getpid()}.json'
        record.write_text(json.dumps({'pid': os.getpid(), 'sessionId': 'original',
                                     'messagingSocketPath': str(fake)}))
        with self.assertRaisesRegex(RuntimeError, 'Not an owned socket'):
            self.daemon.refresh_peer(str(fake), os.getpid())


if __name__ == '__main__':
    unittest.main()
