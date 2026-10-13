import logging

import paramiko
from django.conf import settings
from django.core.exceptions import ImproperlyConfigured
from storages.backends.sftpstorage import SFTPStorage
from storages.utils import is_seekable

logger = logging.getLogger('judge.storage')

#: Failures that can mean "the cached session died", so one reconnect is worth trying.
#: FileNotFoundError is deliberately handled before these: a missing object is an
#: answer, not a broken connection.
RECONNECT_ERRORS = (paramiko.SSHException, EOFError, OSError)


def host_key_name(host, port):
    """known_hosts lookup key, in the same form paramiko's connect() uses."""
    return host if port == 22 else f'[{host}]:{port}'


class BoxSFTPStorage(SFTPStorage):
    """Backup submission-file store on Box's SFTP service.

    Box constraints baked in here:
      - password auth only, so keys/agent lookups are disabled (they waste round-trips
        and Box rejects them);
      - never set file_mode/dir_mode (Box rejects chmod);
      - the host key must already be pinned in DMOJ_BOX_KNOWN_HOSTS (see _connect).
    """

    def __init__(self, **kwargs):
        kwargs.setdefault('host', settings.DMOJ_BOX_SFTP_HOST)
        kwargs.setdefault('root_path', settings.DMOJ_BOX_SFTP_ROOT)
        kwargs.setdefault('known_host_file', settings.DMOJ_BOX_KNOWN_HOSTS or None)
        kwargs.setdefault('params', {
            'port': settings.DMOJ_BOX_SFTP_PORT,
            'username': settings.DMOJ_BOX_SFTP_USER,
            'password': settings.DMOJ_BOX_SFTP_PASSWORD,
            'timeout': 30,
            'banner_timeout': 30,
            'auth_timeout': 30,
            'look_for_keys': False,
            'allow_agent': False,
        })
        super().__init__(**kwargs)

    def _connect(self):
        """Connect with the host key pinned.

        Replaces SFTPStorage._connect, which installs paramiko's AutoAddPolicy
        unconditionally: a host *absent* from known_hosts is then trusted on first
        connect, so anyone able to answer for sftp.services.box.com collects the
        backup stream and our credentials. Here a missing entry is a hard error and
        RejectPolicy refuses anything unpinned. Box needs neither interactive nor
        key/agent auth, so none of the upstream fallbacks are reimplemented.
        """
        key_name = host_key_name(self.host, self.params.get('port', 22))
        if not self.known_host_file:
            raise ImproperlyConfigured(
                f'DMOJ_BOX_KNOWN_HOSTS is empty; refusing to open an unpinned SFTP '
                f'connection to {key_name}.')

        ssh = paramiko.SSHClient()
        ssh.load_host_keys(self.known_host_file)  # raises if the file is unreadable
        if not ssh.get_host_keys().lookup(key_name):
            raise ImproperlyConfigured(
                f'{self.known_host_file} contains no host key for {key_name}; populate it '
                f"with: ssh-keyscan -p {self.params.get('port', 22)} {self.host}")
        ssh.set_missing_host_key_policy(paramiko.RejectPolicy())

        ssh.connect(self.host, **self.params)
        self._ssh = ssh
        if ssh.get_transport():
            self._sftp = ssh.open_sftp()

    # -- stale-session recovery ----------------------------------------------------
    #
    # submission_storage() caches one instance per process, and Box drops idle
    # sessions. The upstream `sftp` property only reconnects when the transport is
    # *known* dead, so a half-open session looks fine and fails on first use. Every
    # remote call therefore gets exactly one reconnect-and-retry; a genuine outage
    # still surfaces to the mirror task's retry policy or to ResilientStorage.

    def _drop_session(self):
        try:
            self.close()
        except Exception:
            logger.debug('error closing stale Box SFTP session', exc_info=True)
        self._ssh = self._sftp = None

    def _retry_once(self, call, *args, **kwargs):
        try:
            return call(*args, **kwargs)
        except FileNotFoundError:
            raise
        except RECONNECT_ERRORS as e:
            logger.warning('Box SFTP call failed (%s); reconnecting and retrying once',
                           type(e).__name__)
            self._drop_session()
            return call(*args, **kwargs)

    def open_stream(self, name):
        """Raw paramiko SFTPFile for a read — a true file-like object FileResponse can
        stream in chunks without buffering the whole file in memory."""
        return self._retry_once(lambda: self.sftp.open(self._remote_path(name), 'rb'))

    def _read(self, name):
        return self._retry_once(super()._read, name)

    def _save(self, name, content):
        if not is_seekable(content):
            # Replaying the upload would resend from wherever the first attempt
            # stopped, so let the caller's retry deal with it instead.
            return super()._save(name, content)
        return self._retry_once(super()._save, name, content)

    def exists(self, name):
        return self._retry_once(super().exists, name)

    def size(self, name):
        return self._retry_once(super().size, name)

    def delete(self, name):
        # Upstream swallows every OSError here, which turns a dead session into a
        # silent no-op that leaves a partial backup copy in place. Keep the
        # missing-file tolerance, surface everything else.
        def remove():
            try:
                self.sftp.remove(self._remote_path(name))
            except FileNotFoundError:
                pass

        self._retry_once(remove)
