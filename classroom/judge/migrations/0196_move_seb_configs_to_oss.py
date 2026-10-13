# Move already-uploaded contest .seb files off MEDIA_ROOT and into the object store.
#
# 0195 repointed Contest.seb_config_file at submission_storage. Rows written before
# that still hold MEDIA_ROOT-relative names ("seb_configs/exam.seb"), which the new
# storage cannot resolve — every such exam would 404 its own config. Copy the bytes
# across and rewrite the name.
#
# The MEDIA_ROOT originals are deliberately left in place: this migration is not
# reversible, so they are the only way back if the object store rejects a file.

import logging
import os
import re

from django.conf import settings
from django.core.files import File
from django.db import migrations

from judge.storage import submission_storage

logger = logging.getLogger('judge.storage')


def _seb_config_key(scope, original_name):
    # Frozen copy of the removed judge.storage.generate_seb_config_key (see 0195).
    import importlib
    return importlib.import_module('judge.migrations.0195_seb_config_oss_storage')._seb_config_key(
        scope, original_name)


# Keys minted by generate_seb_config_key: seb_configs/YYYY/MM/<scope>/<uuid>.seb
NEW_KEY_RE = re.compile(r'^seb_configs/\d{4}/\d{2}/')


def move_seb_configs_to_oss(apps, schema_editor):
    Contest = apps.get_model('judge', 'Contest')
    storage = submission_storage()

    rows = (Contest.objects.exclude(seb_config_file='').exclude(seb_config_file=None)
            .values_list('pk', 'key', 'seb_config_file'))
    for pk, contest_key, name in rows:
        if not name or NEW_KEY_RE.match(name):
            continue
        source = os.path.join(settings.MEDIA_ROOT, name)
        if not os.path.exists(source):
            logger.warning('contest %s: .seb config %s is missing from MEDIA_ROOT; leaving the '
                           'row untouched so it can be re-uploaded', contest_key, name)
            continue
        try:
            with open(source, 'rb') as fh:
                stored = storage.save(_seb_config_key('contest-%d' % pk, name), File(fh))
        except Exception:
            # One unreadable file must not block the whole deploy; the row keeps
            # its old name and an admin can re-upload through the editor.
            logger.exception('contest %s: could not copy .seb config %s to object storage',
                             contest_key, name)
            continue
        Contest.objects.filter(pk=pk).update(seb_config_file=stored)


def noop_reverse(apps, schema_editor):
    pass


class Migration(migrations.Migration):

    dependencies = [
        ('judge', '0195_seb_config_oss_storage'),
    ]

    operations = [
        migrations.RunPython(move_seb_configs_to_oss, noop_reverse),
    ]
