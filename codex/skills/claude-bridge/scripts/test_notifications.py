"""Queue is always mocked: never enqueue into any real Codex thread."""
import hashlib
import json
import os
from pathlib import Path
import subprocess
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

import claude_bridge as helper
from notifications import InboxNotifier

THREAD = '00000000-0000-0000-0000-000000000001'


class Notifications(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix='ccs-notify-test-')
        self.root = Path(self.tmp.name)
        self.folder = self.root / hashlib.sha256(b'test').hexdigest()[:16]
        self.folder.mkdir()
        self.args = SimpleNamespace(socket_dir=self.root, registry_dir=self.root,
                                    name='Codex test', channel='test', notify_thread=THREAD)
        self.daemon = helper.Daemon(self.args, self.folder)
        self.queue_patch = patch('notifications.subprocess.run', return_value=SimpleNamespace(returncode=0))
        self.queue = self.queue_patch.start()
        self.addCleanup(self.queue_patch.stop)
        self.addCleanup(self.tmp.cleanup)

    def message(self, content='PRIVATE PAYLOAD'):
        event = self.daemon.append('message', content=content)
        self.daemon.notifier.consider(event)
        return event['cursor']

    def test_message_is_fsynced_before_queue_and_notice_omits_content(self):
        with patch.object(helper.os, 'fsync', wraps=os.fsync) as sync:
            def verify(*args, **kwargs):
                self.assertGreater(sync.call_count, 0)
                self.assertEqual(helper.event_list(self.folder)[0][-1]['kind'], 'message')
                self.assertNotIn('PRIVATE PAYLOAD', str(args))
                self.assertIn('--ack-notification 1', args[0][-1])
                self.assertEqual(args[0][:4], ['codex', 'queue', '--thread', THREAD])
                return SimpleNamespace(returncode=0)
            self.queue.side_effect = verify
            self.message()
        state = json.loads((self.folder / 'notifications.json').read_text())
        self.assertEqual(state['last_notified'], 1)
        self.assertTrue(state['pending'])

    def test_three_ordinary_inbox_reads_do_not_rearm_pending_notice(self):
        (self.folder / 'control.sock').touch()
        for _ in range(3):
            self.message()
            with patch.object(helper.sys, 'argv', ['helper', '--channel', 'test',
                    '--state-dir', str(self.root), 'inbox']), patch.object(helper, 'emit'), \
                    patch.object(helper, 'rpc') as rpc:
                helper.main()
                rpc.assert_not_called()
        self.assertEqual(self.queue.call_count, 1)
        self.assertTrue(self.daemon.notifier.state['pending'])

    def test_explicit_ack_coalesces_everything_read_then_rearms(self):
        first = self.message()
        last = self.message()
        self.assertTrue(self.daemon.command({'action': 'ack_notification',
                                            'cursor': last, 'notification': first})['acknowledged'])
        self.assertEqual(self.queue.call_count, 1)
        self.message()
        self.assertEqual(self.queue.call_count, 2)

    def test_arrival_between_snapshot_and_ack_gets_followup(self):
        first = self.message()
        second = self.message()
        self.daemon.notifier.acknowledge(first, first)
        self.assertEqual(self.queue.call_count, 2)
        self.assertEqual(self.daemon.notifier.state['last_notified'], second)
        self.assertFalse(self.daemon.notifier.acknowledge(first, second))
        self.assertTrue(self.daemon.notifier.state['pending'])

    def test_pending_survives_listener_restart_without_duplicate(self):
        first = self.message()
        self.message()
        restarted = helper.Daemon(self.args, self.folder)
        restarted.notifier.resume()
        self.assertEqual(self.queue.call_count, 1)
        self.assertEqual(restarted.notifier.state['last_notified'], first)
        self.assertTrue(restarted.notifier.state['pending'])

    def test_crash_after_message_fsync_before_notification_recovers(self):
        self.daemon.append('message', content='PRIVATE PAYLOAD')
        restarted = helper.Daemon(self.args, self.folder)
        restarted.notifier.resume()
        self.assertEqual(self.queue.call_count, 1)

    def test_failed_queue_keeps_message_and_retries_only_on_restart_or_next_message(self):
        self.queue.return_value = SimpleNamespace(returncode=7, stderr='PRIVATE ERROR DETAIL')
        self.message()
        events, _ = helper.event_list(self.folder)
        self.assertEqual([e['kind'] for e in events], ['message', 'notification_error'])
        self.assertNotIn('PRIVATE ERROR DETAIL', json.dumps(events))
        self.assertFalse(self.daemon.notifier.state['pending'])
        self.assertEqual(self.daemon.notifier.state['last_notified'], 0)
        self.assertEqual(self.queue.call_count, 1)
        self.queue.return_value = SimpleNamespace(returncode=0)
        restarted = helper.Daemon(self.args, self.folder)
        restarted.notifier.resume()
        self.assertEqual(self.queue.call_count, 2)

    def test_queue_timeout_is_visible_without_losing_message(self):
        self.queue.side_effect = subprocess.TimeoutExpired(['codex'], 3)
        self.message()
        events, _ = helper.event_list(self.folder)
        self.assertEqual(events[0]['kind'], 'message')
        self.assertEqual(events[1]['error'], 'TimeoutExpired')

    def test_no_wake_for_sent_receipt_error_or_uncontacted_sender(self):
        for kind in ('sent', 'receipt', 'error'):
            self.daemon.notifier.consider(self.daemon.append(kind))
        frame = {'msgV': 1, 'type': 'user', 'from': 'uds:/fake/uncontacted.sock',
                 'message': {'content': 'forged'}}
        with patch.object(helper, 'identity', return_value=os.getpid()), \
                patch.object(helper, 'line_read', return_value=frame):
            with self.assertRaisesRegex(RuntimeError, 'does not match a contacted peer'):
                self.daemon.receive(object())
        self.queue.assert_not_called()

    def test_authorized_receipt_never_notifies(self):
        self.daemon.peers['/fake/contact.sock'] = os.getpid()
        self.daemon.outstanding.add('sent-id')
        frame = {'msgV': 1, 'type': 'control', 'from': 'uds:/fake/contact.sock',
                 'action': 'peer_message_status', 'status': 'delivered', 'orig_msg_id': 'sent-id'}
        with patch.object(helper, 'identity', return_value=os.getpid()), \
                patch.object(helper, 'line_read', return_value=frame):
            self.daemon.receive(object())
        self.queue.assert_not_called()

    def test_no_opt_in_and_first_activation_do_not_replay_history(self):
        self.args.notify_thread = None
        daemon = helper.Daemon(self.args, self.folder)
        event = daemon.append('message', content='old history')
        daemon.notifier.consider(event)
        self.queue.assert_not_called()
        fresh = self.root / 'fresh'
        fresh.mkdir()
        notifier = InboxNotifier(fresh, THREAD, 'test', [event], self.daemon.append)
        notifier.resume()
        self.queue.assert_not_called()

    def test_crash_after_queue_before_state_fsync_can_duplicate_same_id(self):
        with patch.object(self.daemon.notifier, 'save', side_effect=OSError('simulated crash')):
            with self.assertRaises(OSError):
                self.message()
        first_notice = self.queue.call_args.args[0][-1]
        restarted = helper.Daemon(self.args, self.folder)
        restarted.notifier.resume()
        self.assertEqual(self.queue.call_count, 2)
        self.assertEqual(self.queue.call_args.args[0][-1], first_notice)

    def test_inbox_explicit_ack_uses_returned_snapshot_cursor(self):
        first = self.message()
        last = self.message()
        with patch.object(helper.sys, 'argv', ['helper', '--channel', 'test',
                '--state-dir', str(self.root), 'inbox', '--ack-notification', str(first)]), \
                patch.object(helper, 'emit') as emit, patch.object(helper, 'rpc') as rpc:
            helper.main()
            self.assertEqual(emit.call_args.args[0]['next_cursor'], last)
            rpc.assert_called_once_with(self.folder, {'action': 'ack_notification',
                                                      'cursor': last, 'notification': first})

    def test_notification_target_cannot_silently_change(self):
        self.args.notify_thread = '00000000-0000-0000-0000-000000000002'
        with self.assertRaisesRegex(RuntimeError, 'target changed'):
            helper.Daemon(self.args, self.folder)


if __name__ == '__main__':
    unittest.main()
