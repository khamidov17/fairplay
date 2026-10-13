import gzip
import io
import os
import tempfile
from unittest import mock

from django.core.management.base import CommandError
from django.test import SimpleTestCase, override_settings

from judge.management.commands.dump_database import Command


DATABASE = {
    'ENGINE': 'django.db.backends.mysql',
    'NAME': 'dmoj',
    'USER': 'backup-user',
    'PASSWORD': 'do-not-log-this',
    'HOST': 'host.docker.internal',
    'PORT': '3306',
}


class FakeProcess:
    def __init__(self, return_code, payload, error, calls, command, stdout, stderr, env):
        self.return_code = return_code
        self.stdout = io.BytesIO(payload)
        stderr.write(error)
        calls.append((command, env))

    def wait(self):
        return self.return_code


class DumpDatabaseCommandTest(SimpleTestCase):
    def test_build_command_uses_connection_without_password_argument(self):
        with mock.patch('judge.management.commands.dump_database.shutil.which',
                        side_effect=['/usr/bin/mariadb-dump']):
            command = Command.build_command(DATABASE)

        self.assertIn('--single-transaction', command)
        self.assertIn('--host=host.docker.internal', command)
        self.assertIn('--port=3306', command)
        self.assertIn('--user=backup-user', command)
        self.assertEqual(command[-2:], ['--databases', 'dmoj'])
        self.assertNotIn(DATABASE['PASSWORD'], ' '.join(command))

    def test_build_command_supports_unix_socket(self):
        database = dict(DATABASE, HOST='/run/mysqld/mysqld.sock', PORT='')
        with mock.patch('judge.management.commands.dump_database.shutil.which',
                        return_value='/usr/bin/mariadb-dump'):
            command = Command.build_command(database)

        self.assertIn('--socket=/run/mysqld/mysqld.sock', command)
        self.assertFalse(any(argument.startswith('--host=') for argument in command))

    @override_settings(DATABASES={'default': DATABASE})
    def test_handle_compresses_dump_and_passes_password_in_environment(self):
        calls = []

        def popen(command, stdout, stderr, env):
            return FakeProcess(0, b'CREATE TABLE example ();\n', b'', calls,
                               command, stdout, stderr, env)

        with tempfile.TemporaryDirectory() as directory:
            output = os.path.join(directory, 'database.sql.gz')
            with mock.patch('judge.management.commands.dump_database.shutil.which',
                            return_value='/usr/bin/mariadb-dump'), \
                    mock.patch('judge.management.commands.dump_database.subprocess.Popen',
                               side_effect=popen):
                Command().handle(output=output)

            with gzip.open(output, 'rb') as dump:
                self.assertEqual(dump.read(), b'CREATE TABLE example ();\n')

        self.assertEqual(calls[0][1]['MYSQL_PWD'], DATABASE['PASSWORD'])
        self.assertNotIn(DATABASE['PASSWORD'], ' '.join(calls[0][0]))

    @override_settings(DATABASES={'default': DATABASE})
    def test_handle_removes_partial_output_when_dump_fails(self):
        def popen(command, stdout, stderr, env):
            return FakeProcess(2, b'partial sql', b'access denied', [],
                               command, stdout, stderr, env)

        with tempfile.TemporaryDirectory() as directory:
            output = os.path.join(directory, 'database.sql.gz')
            with mock.patch('judge.management.commands.dump_database.shutil.which',
                            return_value='/usr/bin/mariadb-dump'), \
                    mock.patch('judge.management.commands.dump_database.subprocess.Popen',
                               side_effect=popen):
                with self.assertRaisesMessage(CommandError, 'database dump failed: access denied'):
                    Command().handle(output=output)

            self.assertEqual(os.listdir(directory), [])

    def test_missing_client_is_an_operator_error(self):
        with mock.patch('judge.management.commands.dump_database.shutil.which',
                        return_value=None):
            with self.assertRaisesMessage(CommandError, 'mariadb-dump or mysqldump is required'):
                Command.build_command(DATABASE)
