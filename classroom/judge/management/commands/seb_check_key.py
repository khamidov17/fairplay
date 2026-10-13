"""
Check a candidate Safe Exam Browser key against a hash observed in the logs.

When a student is blocked, ``judge.seb`` logs the URL the server hashed and the
hash the client sent, e.g.::

    SEB check failed for /contest/c000018: Config Key hash mismatch ...
    SEB sent 803f37e5023b...; this server hashed the URL
    'https://oj.dukekunshan.edu.cn/contest/c000018' ...

SEB sends ``SHA256(absolute_url + key)``, so a candidate key can be tested
against that pair offline -- no student, no exam, no redeploy. Use it to confirm
a key copied from the SEB Configuration Tool before pasting it into a contest.

```
python manage.py seb_check_key \
    --url 'https://oj.dukekunshan.edu.cn/contest/c000018' \
    --hash 803f37e5023b \
    --key 23bcadedd398... --key <candidate-from-macos>
```

``--hash`` accepts the truncated value straight from the log; it is compared as
a prefix. Pass ``--contest <key>`` instead of ``--key`` to test every key already
configured on a contest (and its preset).
"""

from django.core.management.base import BaseCommand, CommandError

from judge.models import Contest
from judge.utils.seb import SEB_KEY_RE, _sha256_hex, seb_url_variants


class Command(BaseCommand):
    help = 'Test candidate SEB keys against a URL and hash observed in the logs.'

    def add_arguments(self, parser):
        parser.add_argument('--url', required=True,
                            help='The absolute URL from the log line, quoted exactly.')
        parser.add_argument('--hash', required=True,
                            help='The hash SEB sent. The truncated log value is fine.')
        parser.add_argument('--key', action='append', default=[],
                            help='A candidate key hash. Repeat to test several.')
        parser.add_argument('--contest',
                            help='Test every key already configured on this contest and its preset.')

    def handle(self, *args, **options):
        url = options['url']
        received = options['hash'].strip().lower().rstrip('.…')
        candidates = [(k.strip().lower(), 'given') for k in options['key'] if k.strip()]

        if options['contest']:
            try:
                contest = Contest.objects.get(key=options['contest'])
            except Contest.DoesNotExist:
                raise CommandError('No contest with key %r.' % options['contest'])
            # Say which record each key actually lives on: the accessors fall back
            # to the preset, and pasting a fix onto the wrong record fixes nothing.
            own_bek = contest._parse_seb_keys(contest.seb_browser_exam_keys)
            own_cfg = contest._parse_seb_keys(contest.seb_config_keys)
            preset = contest.seb_preset if contest.seb_preset_id else None
            where = 'contest' if own_bek else ('preset %r' % preset.name if preset else 'contest')
            candidates += [(k, '%s BEK' % where) for k in contest.seb_allowed_bek()]
            where = 'contest' if own_cfg else ('preset %r' % preset.name if preset else 'contest')
            candidates += [(k, '%s Config Key' % where) for k in contest.seb_allowed_config_keys()]
            if preset is not None:
                self.stdout.write('Contest %s uses preset %r; keys below are labelled with the record '
                                  'that holds them.\n' % (contest.key, preset.name))

        if not candidates:
            raise CommandError('Pass at least one --key, or --contest to pull the configured ones.')

        self.stdout.write('URL hashed : %r' % url)
        self.stdout.write('SEB sent   : %s' % received)
        self.stdout.write('')

        matched = False
        usable = []
        for key, source in candidates:
            if not SEB_KEY_RE.fullmatch(key):
                self.stdout.write(self.style.WARNING(
                    '  %-24s %s  -- not a 64-char hex hash, SEB would never match it' % (source, key)))
                continue
            usable.append((key, source))
            digest = _sha256_hex(url, key)
            if digest.startswith(received):
                matched = True
                self.stdout.write(self.style.SUCCESS('  %-24s %s -> %s  MATCH' % (source, key, digest[:12])))
            else:
                self.stdout.write('  %-24s %s -> %s' % (source, key, digest[:12]))

        if matched:
            self.stdout.write('')
            self.stdout.write(self.style.SUCCESS(
                'A candidate matches. Paste that key into the contest (or its preset) on its own line; '
                'existing lines can stay, since any one of them is accepted.'))
            return

        # No key matches the URL as given. Before concluding the keys are stale,
        # rule out the other cause: the client hashing a different URL.
        self.stdout.write('')
        self.stdout.write('No key matches that URL. Sweeping URL variants:')
        variant_hit = None
        variants = seb_url_variants(url)
        for variant in variants:
            for key, source in usable:
                if _sha256_hex(variant, key).startswith(received):
                    variant_hit = (variant, key, source)
                    break
            if variant_hit:
                break

        if variant_hit:
            variant, key, source = variant_hit
            self.stdout.write('')
            self.stdout.write(self.style.SUCCESS(
                '  MATCH on %r using the %s.' % (variant, source)))
            self.stdout.write(self.style.SUCCESS(
                'The key is correct -- the server is hashing the wrong URL. Compare it against the URL '
                'above and fix the request reconstruction (SECURE_PROXY_SSL_HEADER for a scheme '
                'difference, USE_X_FORWARDED_HOST or the proxy Host header for a host difference, or a '
                'redirect if only the trailing slash differs). Do not change the keys.'))
            return

        self.stdout.write('  ...%d variants tried, none match.' % len(variants))
        self.stdout.write('')
        self.stdout.write(self.style.ERROR(
            'No key matches under any URL variant, so this is genuinely a key the server does not have. '
            'Read the key off the SEB Configuration Tool on the platform that is failing, with the exact '
            '.seb the student launched loaded -- and confirm that client is really loading your .seb '
            'rather than a configuration previously installed on it as "configuring a client".'))
