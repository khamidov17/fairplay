import gzip
import os
import shutil
import subprocess
import tempfile

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError


class Command(BaseCommand):
    help = 'Write a transactionally consistent, gzip-compressed MySQL database dump.'

    def add_arguments(self, parser):
        parser.add_argument('--output', required=True, help='Destination .sql.gz path')

    @staticmethod
    def build_command(database):
        client = shutil.which('mariadb-dump') or shutil.which('mysqldump')
        if client is None:
            raise CommandError('mariadb-dump or mysqldump is required')

        engine = database.get('ENGINE', '')
        if 'mysql' not in engine:
            raise CommandError('dump_database only supports a MySQL/MariaDB database')

        command = [
            client,
            '--single-transaction',
            '--quick',
            '--skip-lock-tables',
            '--hex-blob',
            '--default-character-set=utf8mb4',
        ]
        host = str(database.get('HOST') or '')
        if host.startswith('/'):
            command.append('--socket=' + host)
        elif host:
            command.append('--host=' + host)
        if database.get('PORT'):
            command.append('--port=' + str(database['PORT']))
        if database.get('USER'):
            command.append('--user=' + str(database['USER']))
        command.extend(['--databases', str(database['NAME'])])
        return command

    def handle(self, *args, **options):
        output = os.path.abspath(options['output'])
        parent = os.path.dirname(output)
        if not os.path.isdir(parent):
            raise CommandError('output directory does not exist: ' + parent)

        database = settings.DATABASES['default']
        command = self.build_command(database)
        environment = os.environ.copy()
        environment['MYSQL_PWD'] = str(database.get('PASSWORD') or '')

        fd, temporary = tempfile.mkstemp(prefix='.database-', suffix='.sql.gz', dir=parent)
        os.close(fd)
        try:
            with tempfile.TemporaryFile() as error_file, gzip.open(temporary, 'wb') as compressed:
                process = subprocess.Popen(
                    command,
                    stdout=subprocess.PIPE,
                    stderr=error_file,
                    env=environment,
                )
                try:
                    shutil.copyfileobj(process.stdout, compressed, length=1024 * 1024)
                finally:
                    process.stdout.close()
                return_code = process.wait()
                if return_code:
                    error_file.seek(0)
                    detail = error_file.read().decode('utf-8', errors='replace').strip()
                    raise CommandError('database dump failed: ' + (detail or f'exit {return_code}'))
            os.replace(temporary, output)
        except BaseException:
            try:
                os.unlink(temporary)
            except FileNotFoundError:
                pass
            raise

        self.stdout.write(self.style.SUCCESS('database dump written to ' + output))
