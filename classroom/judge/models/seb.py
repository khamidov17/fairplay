from django.core.cache import cache
from django.core.exceptions import ValidationError
from django.db import models, transaction
from django.utils import timezone
from django.utils.translation import gettext_lazy as _

from judge.models.profile import Profile
from judge.storage import generate_seb_config_key, submission_storage
from judge.utils.seb import first_invalid_seb_key, parse_seb_keys

__all__ = ['SEBConfigPreset']

# Cached id of the active site default, so every contest page render does not
# hit the database. Invalidated by the signals in judge/signals.py.
DEFAULT_PRESET_CACHE_KEY = 'seb_default_preset_id'
DEFAULT_PRESET_CACHE_TIMEOUT = 86400
# Sentinel stored in the cache when no default is configured, so "no default"
# is cached too instead of falling through to a query on every request.
_NO_DEFAULT = -1


def seb_preset_config_upload_to(instance, filename):
    """upload_to: opaque OSS key (the human-facing label stays on ``name``)."""
    return generate_seb_config_key('presets', filename)


class SEBConfigPreset(models.Model):
    """A ready-made Safe Exam Browser configuration published by site staff.

    Authoring a ``.seb`` file and transcribing its two key hashes is far too
    much work to ask of every instructor, so an administrator uploads one
    blessed configuration here and marks it the site default. Instructors then
    tick a single checkbox in the assignment editor and their contest points at
    this row (``Contest.seb_preset``), inheriting its file, keys and quit URL.

    Contests reference this row live rather than copying it, so fixing the
    default propagates everywhere. That is also why :meth:`clean` refuses edits
    while an exam using the preset is in progress: changing the file changes its
    Config Key, which would lock out every student mid-exam.
    """

    name = models.CharField(
        verbose_name=_('name'),
        max_length=100,
        unique=True,
        help_text=_('Shown to instructors when they pick this configuration, e.g. "Standard exam lockdown".'),
    )
    description = models.TextField(
        verbose_name=_('description'),
        blank=True,
        default='',
        help_text=_('What this configuration locks down. Shown to instructors under the name.'),
    )
    config_file = models.FileField(
        verbose_name=_('SEB config file (.seb)'),
        storage=submission_storage,
        upload_to=seb_preset_config_upload_to,
        max_length=255,
        help_text=_("Built in the SEB Configuration Tool. Set the Start URL to this site's home page so one "
                    'file serves every exam.'),
    )
    browser_exam_keys = models.TextField(
        verbose_name=_('Browser Exam Key hashes'),
        blank=True,
        default='',
        help_text=_('One 64-char hex hash per line, copied from the SEB Configuration Tool — one per '
                    'supported platform/version. Lines may include trailing comments after the hash.'),
    )
    config_keys = models.TextField(
        verbose_name=_('Config Key hashes'),
        blank=True,
        default='',
        help_text=_('One 64-char hex hash per line. Required for macOS clients (and any newer build that '
                    'sends only the Config Key).'),
    )
    quit_url = models.URLField(
        verbose_name=_('quit URL'),
        blank=True,
        default='',
        help_text=_('Must match the Quit URL set inside the .seb file. Students land here after their final '
                    'submission so SEB auto-closes. Leave blank to disable.'),
    )
    is_default = models.BooleanField(
        verbose_name=_('site default'),
        default=False,
        help_text=_('The configuration instructors get from the "use the default configuration" checkbox. '
                    'Marking one default un-marks any other.'),
    )
    is_active = models.BooleanField(
        verbose_name=_('active'),
        default=True,
        help_text=_('Uncheck to retire this configuration. Contests already using it keep working; it just '
                    'stops being offered as the default.'),
    )
    updated_by = models.ForeignKey(
        Profile,
        verbose_name=_('last updated by'),
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name='+',
    )
    updated_on = models.DateTimeField(verbose_name=_('last updated'), auto_now=True)

    class Meta:
        verbose_name = _('SEB config preset')
        verbose_name_plural = _('SEB config presets')
        ordering = ['-is_default', 'name']

    def __str__(self):
        return self.name

    def allowed_bek(self):
        return parse_seb_keys(self.browser_exam_keys)

    def allowed_config_keys(self):
        return parse_seb_keys(self.config_keys)

    # Fields whose change invalidates the keys students' SEB clients are already
    # sending, or the URL their client is configured to quit to.
    _LOCKOUT_FIELDS = ('config_file', 'browser_exam_keys', 'config_keys', 'quit_url')

    def live_seb_contest_keys(self, limit=5):
        """Keys of contests using this preset that students are sitting right now."""
        if self.pk is None:
            return []
        now = timezone.now()
        return list(self.contests.filter(seb_required=True, start_time__lte=now, end_time__gt=now)
                        .values_list('key', flat=True)[:limit])

    def clean(self):
        errors = {}
        if not self.allowed_bek():
            errors['browser_exam_keys'] = _('At least one Browser Exam Key hash is required.')
        if not self.allowed_config_keys():
            errors['config_keys'] = _('At least one Config Key hash is required '
                                      '(the macOS SEB build only sends the Config Key).')
        for field in ('browser_exam_keys', 'config_keys'):
            invalid = first_invalid_seb_key(getattr(self, field))
            if invalid is not None:
                errors[field] = _('"%s" is not a 64-character hex hash.') % invalid
        if errors:
            raise ValidationError(errors)

        # Editing the file or keys re-keys every contest pointing here. Doing
        # that during a live exam would 403 every student already inside SEB.
        if self.pk is not None:
            try:
                stored = self.__class__.objects.get(pk=self.pk)
            except self.__class__.DoesNotExist:
                stored = None
            if stored is not None:
                changed = [f for f in self._LOCKOUT_FIELDS
                           if str(getattr(stored, f) or '') != str(getattr(self, f) or '')]
                if changed:
                    live = self.live_seb_contest_keys()
                    if live:
                        raise ValidationError(
                            _('An exam using this configuration is in progress (%(keys)s). Changing the '
                              'file or keys now would lock those students out. Duplicate this preset and '
                              'edit the copy instead.') % {'keys': ', '.join(live)},
                        )

    def save(self, *args, **kwargs):
        with transaction.atomic():
            super().save(*args, **kwargs)
            # Exactly one site default: marking this one un-marks the rest.
            if self.is_default:
                self.__class__.objects.filter(is_default=True).exclude(pk=self.pk).update(is_default=False)

    @classmethod
    def get_default(cls):
        """The active site-default preset, or ``None`` if staff haven't set one."""
        preset_id = cache.get(DEFAULT_PRESET_CACHE_KEY)
        if preset_id == _NO_DEFAULT:
            return None
        if preset_id is not None:
            preset = cls.objects.filter(pk=preset_id, is_default=True, is_active=True).first()
            if preset is not None:
                return preset
        preset = cls.objects.filter(is_default=True, is_active=True).first()
        cache.set(DEFAULT_PRESET_CACHE_KEY, preset.pk if preset else _NO_DEFAULT, DEFAULT_PRESET_CACHE_TIMEOUT)
        return preset
