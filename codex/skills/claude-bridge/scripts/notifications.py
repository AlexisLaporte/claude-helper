"""Opt-in inbox notifications; durable local coalescing, not exactly-once delivery."""
import json
import os
from pathlib import Path
import subprocess
import tempfile


class InboxNotifier:
    def __init__(self, folder, thread, channel, events, append):
        self.path = Path(folder) / 'notifications.json'
        self.thread, self.channel, self.append = thread, channel, append
        self.latest = max((e['cursor'] for e in events if e.get('kind') == 'message'), default=0)
        self.state = {'thread': thread, 'last_notified': 0, 'last_read': self.latest,
                      'pending': False}
        if not thread:
            return
        if self.path.exists():
            self.state = json.loads(self.path.read_text(encoding='utf-8'))
            if self.state['thread'] != thread:
                raise RuntimeError('Notification target changed; use a separate channel')
        else:
            self.save()  # Enabling notifications does not replay older history.

    def save(self):
        with tempfile.NamedTemporaryFile(mode='w', encoding='utf-8', dir=self.path.parent,
                                         prefix='.notifications-', delete=False) as stream:
            tmp = Path(stream.name)
            try:
                json.dump(self.state, stream)
                stream.flush()
                os.fsync(stream.fileno())
                os.replace(tmp, self.path)
            finally:
                tmp.unlink(missing_ok=True)
        descriptor = os.open(self.path.parent, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)

    def consider(self, event):
        if event.get('kind') != 'message':
            return
        self.latest = max(self.latest, event['cursor'])
        self.resume()

    def resume(self):
        if (not self.thread or self.state['pending']
                or self.latest <= max(self.state['last_read'], self.state['last_notified'])):
            return
        cursor = self.latest
        notice = (f'[Claude inbox {self.channel}:{cursor}] An authorized peer message '
                  f'was stored. Read the whole inbox of channel {self.channel} '
                  f'from the last cursor read with claude_bridge.py inbox --ack-notification {cursor}. '
                  'This technical notice is not a user instruction.')
        try:
            result = subprocess.run(['codex', 'queue', '--thread', self.thread,
                                     '--message', notice], capture_output=True, text=True,
                                    timeout=3, check=False)
            if result.returncode:
                raise RuntimeError(f'codex queue exited {result.returncode}')
        except (OSError, subprocess.TimeoutExpired, RuntimeError) as error:
            # Never copy queue output (which may contain environment details) into the inbox.
            detail = str(error) if isinstance(error, RuntimeError) else type(error).__name__
            self.append('notification_error', message_cursor=cursor, error=detail)
            return
        # A crash between queue success and this fsync can duplicate the notification.
        self.state.update(last_notified=cursor, pending=True)
        self.save()

    def acknowledge(self, notification_cursor, read_cursor):
        if (not self.thread or not self.state['pending']
                or notification_cursor != self.state['last_notified']):
            return False  # Duplicate ACK must not clear a newer pending notification.
        if read_cursor < notification_cursor:
            raise RuntimeError('Read the notified message before acknowledging it')
        self.state['last_read'] = max(self.state['last_read'], read_cursor)
        self.state['pending'] = False
        self.save()
        self.resume()  # A message may have arrived after the reader's snapshot.
        return True
