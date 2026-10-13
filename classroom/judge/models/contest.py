from django.conf import settings
from django.core.exceptions import ValidationError
from django.core.validators import MaxValueValidator, MinValueValidator, RegexValidator
from django.db import models, transaction
from django.db.models import CASCADE, Q
from django.urls import reverse
from django.utils import timezone
from django.utils.functional import cached_property
from django.utils.translation import gettext_lazy as _
from jsonfield import JSONField
from lupa import LuaRuntime
from moss import MOSS_LANG_C, MOSS_LANG_CC, MOSS_LANG_JAVA, MOSS_LANG_PYTHON

from judge import contest_format
from judge.models.problem import Problem
from judge.models.profile import Class, Organization, Profile
from judge.models.submission import Submission
from judge.ratings import rate_contest
from judge.storage import generate_seb_config_key, submission_storage
from judge.utils.seb import first_invalid_seb_key, parse_seb_keys
from judge.utils.unique_ids import next_prefixed_id

__all__ = [
    'Contest', 'ContestTag', 'ContestParticipation', 'ContestProblem', 'ContestSubmission', 'Rating',
    'contest_problem_display_name',
]


class MinValueOrNoneValidator(MinValueValidator):
    def compare(self, a, b):
        return a is not None and b is not None and super().compare(a, b)


def seb_contest_config_upload_to(instance, filename):
    """upload_to: opaque OSS key. Uploads only happen in edit mode, so the
    contest already has a pk to shard by."""
    return generate_seb_config_key('contest-%s' % (instance.pk or 0), filename)


class ContestTag(models.Model):
    color_validator = RegexValidator('^#(?:[A-Fa-f0-9]{3}){1,2}$', _('Invalid colour.'))

    name = models.CharField(max_length=20, verbose_name=_('tag name'), unique=True,
                            validators=[RegexValidator(r'^[a-z-]+$', message=_('Lowercase letters and hyphens only.'))])
    color = models.CharField(max_length=7, verbose_name=_('tag colour'), validators=[color_validator])
    description = models.TextField(verbose_name=_('tag description'), blank=True)

    def __str__(self):
        return self.name

    def get_absolute_url(self):
        return reverse('contest_tag', args=[self.name])

    @property
    def text_color(self, cache={}):
        if self.color not in cache:
            if len(self.color) == 4:
                r, g, b = [ord(bytes.fromhex(i * 2)) for i in self.color[1:]]
            else:
                r, g, b = [i for i in bytes.fromhex(self.color[1:])]
            cache[self.color] = '#000' if 299 * r + 587 * g + 144 * b > 140000 else '#fff'
        return cache[self.color]

    class Meta:
        verbose_name = _('contest tag')
        verbose_name_plural = _('contest tags')


class Contest(models.Model):
    SCOREBOARD_VISIBLE = 'V'
    SCOREBOARD_AFTER_CONTEST = 'C'
    SCOREBOARD_AFTER_PARTICIPATION = 'P'
    SCOREBOARD_HIDDEN = 'H'
    SCOREBOARD_VISIBILITY = (
        (SCOREBOARD_VISIBLE, _('Visible')),
        (SCOREBOARD_AFTER_CONTEST, _('Hidden for duration of contest')),
        (SCOREBOARD_AFTER_PARTICIPATION, _('Hidden for duration of participation')),
        (SCOREBOARD_HIDDEN, _('Hidden permanently')),
    )
    key = models.CharField(max_length=20, verbose_name=_('contest id'), unique=True,
                           validators=[RegexValidator('^[a-z0-9]+$', _('Contest id must be ^[a-z0-9]+$'))])
    name = models.CharField(max_length=100, verbose_name=_('contest name'), db_index=True)
    authors = models.ManyToManyField(Profile, verbose_name=_('authors'),
                                     help_text=_('These users will be able to edit the contest.'),
                                     related_name='authored_contests')
    curators = models.ManyToManyField(Profile, verbose_name=_('curators'),
                                      help_text=_('These users will be able to edit the contest, '
                                                  'but will not be listed as authors.'),
                                      related_name='curated_contests', blank=True)
    testers = models.ManyToManyField(Profile, verbose_name=_('testers'),
                                     help_text=_('These users will be able to view the contest, but not edit it.'),
                                     blank=True, related_name='tested_contests')
    tester_see_scoreboard = models.BooleanField(verbose_name=_('testers see scoreboard'), default=False,
                                                help_text=_('If testers can see the scoreboard.'))
    tester_see_submissions = models.BooleanField(verbose_name=_('testers see submissions'), default=False,
                                                 help_text=_('If testers can see in-contest submissions.'))
    spectators = models.ManyToManyField(Profile, verbose_name=_('spectators'),
                                        help_text=_('These users will be able to spectate the contest, '
                                                    'but not see the problems ahead of time.'),
                                        blank=True, related_name='spectated_contests')
    description = models.TextField(verbose_name=_('description'), blank=True)
    problems = models.ManyToManyField(Problem, verbose_name=_('problems'), through='ContestProblem')
    start_time = models.DateTimeField(verbose_name=_('start time'), db_index=True)
    end_time = models.DateTimeField(verbose_name=_('end time'), db_index=True)
    time_limit = models.DurationField(verbose_name=_('time limit'), blank=True, null=True)
    is_visible = models.BooleanField(verbose_name=_('publicly visible'), default=False,
                                     help_text=_('Should be set even for organization-private contests, where it '
                                                 'determines whether the contest is visible to members of the '
                                                 'specified organizations.'))
    is_rated = models.BooleanField(verbose_name=_('contest rated'), help_text=_('Whether this contest can be rated.'),
                                   default=False)
    view_contest_scoreboard = models.ManyToManyField(Profile, verbose_name=_('view contest scoreboard'), blank=True,
                                                     related_name='view_contest_scoreboard',
                                                     help_text=_('These users will be able to view the scoreboard.'))
    view_contest_submissions = models.ManyToManyField(Profile, verbose_name=_('can see contest submissions'),
                                                      blank=True, related_name='view_contest_submissions',
                                                      help_text=_('These users will be able '
                                                                  'to see in-contest submissions.'))
    # Model / Django admin default is visible (traditional contests). The
    # assignment editor defaults new homework to SCOREBOARD_HIDDEN instead.
    scoreboard_visibility = models.CharField(verbose_name=_('scoreboard visibility'), default=SCOREBOARD_VISIBLE,
                                             help_text=_('Scoreboard visibility through the duration of the contest.'),
                                             max_length=1, choices=SCOREBOARD_VISIBILITY)
    use_clarifications = models.BooleanField(verbose_name=_('no comments'),
                                             help_text=_('Use clarification system instead of comments.'),
                                             default=True)
    rating_floor = models.IntegerField(verbose_name=_('rating floor'),
                                       help_text=_('Do not rate users who have a lower rating.'), null=True, blank=True)
    rating_ceiling = models.IntegerField(verbose_name=_('rating ceiling'),
                                         help_text=_('Do not rate users who have a higher rating.'),
                                         null=True, blank=True)
    performance_ceiling_override = models.IntegerField(verbose_name=_('performance ceiling override'),
                                                       help_text=_('Overrides the performance_ceiling parameter '
                                                                   'used in computing ratings. '
                                                                   "Do not modify unless you know what you're doing!"),
                                                       null=True, blank=True)
    rate_all = models.BooleanField(verbose_name=_('rate all'),
                                   help_text=_('Rate users even if they make no submissions.'),
                                   default=False)
    rate_exclude = models.ManyToManyField(Profile, verbose_name=_('exclude from ratings'), blank=True,
                                          related_name='rate_exclude+')
    is_private = models.BooleanField(verbose_name=_('private to specific users'), default=False)
    private_contestants = models.ManyToManyField(Profile, blank=True, verbose_name=_('private contestants'),
                                                 help_text=_('If non-empty, only these users may see the contest.'),
                                                 related_name='private_contestants+')
    hide_problem_tags = models.BooleanField(verbose_name=_('hide problem tags'),
                                            help_text=_('Whether problem tags should be hidden by default.'),
                                            default=False)
    hide_problem_authors = models.BooleanField(verbose_name=_('hide problem authors'),
                                               help_text=_('Whether problem authors should be hidden by default.'),
                                               default=False)
    run_pretests_only = models.BooleanField(verbose_name=_('run pretests only'),
                                            help_text=_('Whether judges should grade pretests only, versus all '
                                                        'testcases. Commonly set during a contest, then unset '
                                                        'prior to rejudging user submissions when the contest ends.'),
                                            default=False)
    results_finalized = models.BooleanField(verbose_name=_('results finalized'),
                                            help_text=_('Set by the "Finalize results" action when an exam-style '
                                                        'contest ends: it reveals hidden problem results to students '
                                                        'and folds them into the scoreboard. Leave unset while the '
                                                        'contest is running.'),
                                            default=False)
    AI_GRADING_OFF = 'off'
    AI_GRADING_REALTIME = 'realtime'
    AI_GRADING_BATCH = 'batch'
    AI_GRADING_MODE_CHOICES = (
        (AI_GRADING_OFF, _('Off')),
        (AI_GRADING_REALTIME, _('On demand — student requests AI grading')),
        (AI_GRADING_BATCH, _('Batch — latest submits after the contest')),
    )
    ai_grading_mode = models.CharField(
        verbose_name=_('AI grading (legacy mode)'), max_length=16,
        choices=AI_GRADING_MODE_CHOICES, default=AI_GRADING_OFF,
        help_text=_('Legacy field kept for older assignments. Prefer '
                    '“allow student AI grading”. Synced from that checkbox on save: '
                    'on → on-demand, off → off. Teachers can always run AI from the '
                    'Grade page regardless of this value.'),
    )
    allow_student_ai_grading = models.BooleanField(
        verbose_name=_('allow student use AI grading'),
        default=False,
        help_text=_('When enabled, students can request AI grading on the submission '
                    'page (subject to quota). Teachers can always run AI grading from '
                    'the Grade page or submission page.'),
    )
    ai_grading_auto_delay_minutes = models.IntegerField(
        verbose_name=_('auto-grade delay after end (minutes)'),
        default=120,
        help_text=_('Minutes after the contest ends to re-grade each student’s latest '
                    'objective submissions (multiple choice, true/false, exact fill-blank). '
                    'Does not call the LLM. −1 = Off (no auto-run). 0 = run at end time. '
                    'Teachers can still use Batch grading on the Grade page anytime.'),
    )
    ai_grading_scheduled_task_id = models.CharField(
        verbose_name=_('scheduled batch AI task id'), max_length=255, blank=True, default='',
    )
    ai_grading_batch_started_at = models.DateTimeField(
        verbose_name=_('batch AI started'), null=True, blank=True,
    )
    ai_grading_batch_finished_at = models.DateTimeField(
        verbose_name=_('batch AI finished'), null=True, blank=True,
    )
    harvest_scheduled_task_id = models.CharField(
        verbose_name=_('scheduled auto-submit task id'), max_length=255, blank=True, default='',
        help_text=_('Celery task that auto-submits saved answers when this assignment ends. '
                    'Assignments with a personal time limit are scheduled per participation instead.'),
    )
    show_short_display = models.BooleanField(verbose_name=_('show short form settings display'),
                                             help_text=_('Whether to show a section containing contest settings '
                                                         'on the contest page or not.'),
                                             default=False)
    is_organization_private = models.BooleanField(verbose_name=_('private to organizations'), default=False)
    organizations = models.ManyToManyField(Organization, blank=True, verbose_name=_('organizations'),
                                           help_text=_('If non-empty, only these organizations may see the contest.'))
    limit_join_organizations = models.BooleanField(verbose_name=_('limit organizations that can join'), default=False)
    join_organizations = models.ManyToManyField(Organization, blank=True, verbose_name=_('join organizations'),
                                                help_text=_('If non-empty, only these organizations may join '
                                                            'the contest.'), related_name='join_only_contests')
    classes = models.ManyToManyField(Class, blank=True, verbose_name=_('classes'),
                                     help_text=_('If organization private, only these classes may see the contest.'))
    og_image = models.CharField(verbose_name=_('OpenGraph image'), default='', max_length=150, blank=True)
    logo_override_image = models.CharField(verbose_name=_('logo override image'), default='', max_length=150,
                                           blank=True,
                                           help_text=_('This image will replace the default site logo for users '
                                                       'inside the contest.'))
    tags = models.ManyToManyField(ContestTag, verbose_name=_('contest tags'), blank=True, related_name='contests')
    user_count = models.IntegerField(verbose_name=_('the amount of live participants'), default=0)
    summary = models.TextField(blank=True, verbose_name=_('contest summary'),
                               help_text=_('Plain-text, shown in meta description tag, e.g. for social media.'))
    access_code = models.CharField(verbose_name=_('access code'), blank=True, default='', max_length=255,
                                   help_text=_('An optional code to prompt contestants before they are allowed '
                                               'to join the contest. Leave it blank to disable.'))
    banned_users = models.ManyToManyField(Profile, verbose_name=_('personae non gratae'), blank=True,
                                          help_text=_('Bans the selected users from joining this contest.'))
    format_name = models.CharField(verbose_name=_('contest format'), default='default', max_length=32,
                                   choices=contest_format.choices(), help_text=_('The contest format module to use.'))
    format_config = JSONField(verbose_name=_('contest format configuration'), null=True, blank=True,
                              help_text=_('A JSON object to serve as the configuration for the chosen contest format '
                                          'module. Leave empty to use None. Exact format depends on the contest format '
                                          'selected.'))
    problem_label_script = models.TextField(verbose_name=_('contest problem label script'), blank=True,
                                            help_text=_('A custom Lua function to generate problem labels. Requires a '
                                                        'single function with an integer parameter, the zero-indexed '
                                                        'contest problem index, and returns a string, the label.'))
    locked_after = models.DateTimeField(verbose_name=_('contest lock'), null=True, blank=True,
                                        help_text=_('Prevent submissions from this contest '
                                                    'from being rejudged after this date.'))
    points_precision = models.IntegerField(verbose_name=_('precision points'), default=3,
                                           validators=[MinValueValidator(0), MaxValueValidator(10)],
                                           help_text=_('Number of digits to round points to.'))
    allow_virtual_participation = models.BooleanField(
        verbose_name=_('allow virtual join'),
        default=True,
        help_text=_('Allow users to start a virtual participation after the assignment ends.'),
    )
    # Spectating is deliberately separate from virtual participation: virtual
    # joins happen after the deadline, while spectating is an in-progress,
    # non-scoring participation.
    allow_spectate = models.BooleanField(
        verbose_name=_('allow spectate'),
        default=False,
        help_text=_('Allow students to spectate while the assignment is active. Spectator submissions do not count '
                    'toward statistics or rankings.'),
    )
    hide_sandbox_output = models.BooleanField(
        verbose_name=_('Hide IDE / Run Test output'),
        default=False,
        help_text=_('Hide sandbox stdout from students. The verdict, compile errors, and '
                    'runtime exceptions remain visible. This does not close the IDE, lock '
                    'problems, or change Submit.'),
    )
    allow_ai_hints = models.BooleanField(
        verbose_name=_('allow student use AI hints'),
        default=False,
        help_text=_('When this assignment is active, students can use AI Hint on every attached '
                    "problem. Each problem's own AI Hint setting is unchanged outside this assignment."),
    )
    seb_required = models.BooleanField(
        verbose_name=_('require Safe Exam Browser'),
        default=False,
        help_text=_('If enabled, non-staff participants can only access this contest from inside Safe Exam Browser.'),
    )
    seb_browser_exam_keys = models.TextField(
        verbose_name=_('allowed SEB Browser Exam Key hashes'),
        blank=True,
        default='',
        help_text=_('One 64-char hex hash per line. Copy from the SEB Configuration Tool — one per '
                    'supported platform/version. Lines may include trailing comments after the hash.'),
    )
    seb_config_keys = models.TextField(
        verbose_name=_('allowed SEB Config Key hashes'),
        blank=True,
        default='',
        help_text=_('One 64-char hex hash per line. Required for macOS clients (and any newer build '
                    'that sends only the Config Key).'),
    )
    seb_config_file = models.FileField(
        verbose_name=_('SEB config file (.seb)'),
        storage=submission_storage,
        upload_to=seb_contest_config_upload_to,
        max_length=255,
        blank=True,
        null=True,
        help_text=_('The .seb file students download or launch via sebs:// to enter this exam.'),
    )
    seb_quit_url = models.URLField(
        verbose_name=_('SEB quit URL'),
        blank=True,
        default='',
        help_text=_('Must match the Quit URL set inside the .seb file. Shown to students after final '
                    'submission so SEB auto-closes. Leave blank to disable.'),
    )
    seb_preset = models.ForeignKey(
        'judge.SEBConfigPreset',
        verbose_name=_('SEB config preset'),
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name='contests',
        help_text=_('Inherit the .seb file, key hashes and quit URL from a staff-published configuration. '
                    'Anything filled in above overrides the preset for this contest.'),
    )

    @classmethod
    def generate_unique_key(cls):
        """Next unused assignment/contest id in the clone/admin series ``c000000``."""
        return next_prefixed_id(
            cls.objects.values_list('key', flat=True),
            'c',
            fallback_prefix='contest',
        )

    @cached_property
    def format_class(self):
        return contest_format.formats[self.format_name]

    @cached_property
    def format(self):
        return self.format_class(self, self.format_config)

    @cached_property
    def get_label_for_problem(self):
        if not self.problem_label_script:
            return self.format.get_label_for_problem

        def DENY_ALL(obj, attr_name, is_setting):
            raise AttributeError()
        lua = LuaRuntime(attribute_filter=DENY_ALL, register_eval=False, register_builtins=False)
        return lua.eval(self.problem_label_script)

    def clean(self):
        # Django will complain if you didn't fill in start_time or end_time, so we don't have to.
        if self.start_time and self.end_time and self.start_time >= self.end_time:
            raise ValidationError(_('What is this? An assignment that ended before it starts?'))
        self.format_class.validate(self.format_config)

        try:
            # a contest should have at least one problem, with contest problem index 0
            # so test it to see if the script returns a valid label.
            label = self.get_label_for_problem(0)
        except Exception as e:
            raise ValidationError('Contest problem label script: %s' % e)
        else:
            if not isinstance(label, str):
                raise ValidationError('Contest problem label script: script should return a string.')

        if self.seb_required:
            seb_errors = {}
            # Validated against the EFFECTIVE keys: a contest that inherits a
            # preset needs nothing of its own, which is what lets an instructor
            # arm SEB in a single save instead of uploading a .seb by hand.
            if not self.seb_allowed_bek():
                seb_errors['seb_browser_exam_keys'] = _(
                    'At least one Browser Exam Key hash is required when Safe Exam Browser is enabled.')
            if not self.seb_allowed_config_keys():
                seb_errors['seb_config_keys'] = _(
                    'At least one Config Key hash is required when Safe Exam Browser is enabled '
                    '(the macOS SEB build only sends the Config Key).')
            # Only this contest's own fields are hex-checked; the preset
            # validates its own keys when staff save it.
            for field in ('seb_browser_exam_keys', 'seb_config_keys'):
                invalid = first_invalid_seb_key(getattr(self, field))
                if invalid is not None:
                    seb_errors[field] = _('"%s" is not a 64-character hex hash.') % invalid
            seb_errors.update(self._seb_mixed_source_errors())
            if seb_errors:
                raise ValidationError(seb_errors)

    def _seb_source_of(self, own_value, preset_attr):
        """Which record supplies a SEB setting: this contest, the preset, or nothing.

        Every SEB accessor is "own value, else preset" — a replace, never a union.
        """
        if own_value:
            return 'contest'
        if self.seb_preset_id and getattr(self.seb_preset, preset_attr):
            return 'preset'
        return None

    def _seb_mixed_source_errors(self):
        """Reject a .seb from one record with key hashes from another.

        The keys authenticate the exact file students receive, so drawing the two
        from different records locks out every platform whose key is wrong —
        silently, since a stale Config Key is invisible on Windows (which matches
        on the Browser Exam Key first) and fatal on macOS (which sends only the
        Config Key).
        """
        file_source = self._seb_source_of(self.seb_config_file, 'config_file')
        if file_source is None:
            return {}
        where = {'contest': _('this assignment'), 'preset': _('the shared configuration')}
        errors = {}
        for field, own, preset_attr, label in (
            ('seb_browser_exam_keys', self._parse_seb_keys(self.seb_browser_exam_keys),
             'browser_exam_keys', _('Browser Exam Key')),
            ('seb_config_keys', self._parse_seb_keys(self.seb_config_keys),
             'config_keys', _('Config Key')),
        ):
            key_source = self._seb_source_of(own, preset_attr)
            if key_source is not None and key_source != file_source:
                errors[field] = _(
                    'The .seb file comes from %(file_from)s but the %(label)s hashes come from '
                    '%(keys_from)s. The hashes authenticate the exact file students receive, so this '
                    'combination locks them out — on macOS silently, since it authenticates with the '
                    'Config Key alone. Use the file and the hashes from the same place.',
                ) % {'file_from': where[file_source], 'label': label, 'keys_from': where[key_source]}
        return errors

    def is_in_contest(self, user):
        if user.is_authenticated:
            profile = user.profile
            return profile and profile.current_contest is not None and profile.current_contest.contest == self
        return False

    def can_see_own_scoreboard(self, user):
        if self.can_see_full_scoreboard(user):
            return True
        if not self.started:
            return False
        if not self.show_scoreboard and not self.is_in_contest(user) and not self.has_completed_contest(user):
            return False
        return True

    def can_see_full_scoreboard(self, user):
        if self.show_scoreboard:
            return True
        if not user.is_authenticated:
            return False
        if user.has_perm('judge.see_private_contest') or user.has_perm('judge.edit_all_contest'):
            return True
        if user.profile.id in self.editor_ids:
            return True
        if self.tester_see_scoreboard and user.profile.id in self.tester_ids:
            return True
        if self.started and user.profile.id in self.spectator_ids:
            return True
        if self.view_contest_scoreboard.filter(id=user.profile.id).exists():
            return True
        if self.scoreboard_visibility == self.SCOREBOARD_AFTER_PARTICIPATION and self.has_completed_contest(user):
            return True
        return False

    def has_completed_contest(self, user):
        if user.is_authenticated:
            participation = self.users.filter(virtual=ContestParticipation.LIVE, user=user.profile).first()
            if participation and participation.ended:
                return True
        return False

    @cached_property
    def show_scoreboard(self):
        if not self.started:
            return False
        if (self.scoreboard_visibility in (self.SCOREBOARD_AFTER_CONTEST, self.SCOREBOARD_AFTER_PARTICIPATION) and
                not self.ended):
            return False
        return self.scoreboard_visibility != self.SCOREBOARD_HIDDEN

    @property
    def contest_window_length(self):
        return self.end_time - self.start_time

    @cached_property
    def _now(self):
        # This ensures that all methods talk about the same now.
        return timezone.now()

    @cached_property
    def started(self):
        return self.start_time <= self._now

    @property
    def time_before_start(self):
        if self.start_time >= self._now:
            return self.start_time - self._now
        else:
            return None

    @property
    def time_before_end(self):
        if self.end_time >= self._now:
            return self.end_time - self._now
        else:
            return None

    @cached_property
    def performance_ceiling(self):
        if self.performance_ceiling_override is not None:
            return self.performance_ceiling_override
        elif self.rating_ceiling:
            return self.rating_ceiling + settings.DMOJ_CONTEST_PERF_CEILING_INCREMENT
        else:
            return None

    @cached_property
    def ended(self):
        return self.end_time < self._now

    @cached_property
    def author_ids(self):
        return Contest.authors.through.objects.filter(contest=self).values_list('profile_id', flat=True)

    @cached_property
    def editor_ids(self):
        return self.author_ids.union(
            Contest.curators.through.objects.filter(contest=self).values_list('profile_id', flat=True))

    @cached_property
    def tester_ids(self):
        return Contest.testers.through.objects.filter(contest=self).values_list('profile_id', flat=True)

    @cached_property
    def spectator_ids(self):
        return Contest.spectators.through.objects.filter(contest=self).values_list('profile_id', flat=True)

    def __str__(self):
        return self.name

    def get_absolute_url(self):
        return reverse('contest_view', args=(self.key,))

    def update_user_count(self):
        self.user_count = self.users.filter(virtual=0).count()
        self.save()

    update_user_count.alters_data = True

    class Inaccessible(Exception):
        pass

    class PrivateContest(Exception):
        pass

    def access_check(self, user):
        # Do unauthenticated check here so we can skip authentication checks later on.
        if not user.is_authenticated:
            # Unauthenticated users can only see visible, non-private contests
            if not self.is_visible:
                raise self.Inaccessible()
            if self.is_private or self.is_organization_private:
                raise self.PrivateContest()
            return

        # If the user can view or edit all contests
        if user.has_perm('judge.see_private_contest') or user.has_perm('judge.edit_all_contest'):
            return

        # User is organizer or curator for contest
        if user.profile.id in self.editor_ids:
            return

        # User is tester for contest
        if user.profile.id in self.tester_ids:
            return

        # User is spectator for contest
        if user.profile.id in self.spectator_ids:
            return

        # Org admins can always see org-private contests
        if self.is_viewable_by_org_admin(user):
            return

        # Course teachers can view org-private assignments in their course
        if self.is_organization_private and self.is_viewable_by_course_teacher(user):
            return

        # Contest is not publicly visible
        if not self.is_visible:
            raise self.Inaccessible()

        # Contest is not private
        if not self.is_private and not self.is_organization_private:
            return

        if self.view_contest_scoreboard.filter(id=user.profile.id).exists():
            return

        # Check organization/class membership for students
        if self.is_organization_private:
            # If classes are specified, check class membership
            if self.classes.exists():
                in_class = self.classes.filter(id__in=user.profile.classes.all()).exists()
                if not self.is_private and in_class:
                    return
                if self.is_private:
                    in_users = self.private_contestants.filter(id=user.profile.id).exists()
                    if in_class and in_users:
                        return
                raise self.PrivateContest()
            else:
                # No classes specified - check org membership
                in_org = self.organizations.filter(id__in=user.profile.organizations.all()).exists()
                if not self.is_private and in_org:
                    return
                if self.is_private:
                    in_users = self.private_contestants.filter(id=user.profile.id).exists()
                    if in_org and in_users:
                        return
                raise self.PrivateContest()

        if self.is_private and not self.is_organization_private:
            in_users = self.private_contestants.filter(id=user.profile.id).exists()
            if in_users:
                return
            raise self.PrivateContest()

    # Assumes the user can access, to avoid the cost again
    def is_live_joinable_by(self, user):
        if not self.started:
            return False

        if not user.is_authenticated:
            return False

        # Can be populated using annotate for performance in list views.
        editor_or_tester = getattr(self, 'editor_or_tester', None)
        if editor_or_tester is None:
            editor_or_tester = user.profile.id in self.editor_ids or user.profile.id in self.tester_ids

        if editor_or_tester:
            return False

        completed_contest = getattr(self, 'completed_contest', None)
        if completed_contest is None:
            completed_contest = self.has_completed_contest(user)

        if completed_contest:
            return False

        if self.limit_join_organizations:
            return self.join_organizations.filter(id__in=user.profile.organizations.all()).exists()
        return True

    # Also skips access check
    def is_spectatable_by(self, user):
        if not user.is_authenticated:
            return False

        if user.profile.id in self.editor_ids or user.profile.id in self.tester_ids:
            return True

        # Spectating is opt-in for each assignment. Virtual participation uses
        # a separate switch because it is only available after the deadline.
        if not self.allow_spectate:
            return False

        if self.limit_join_organizations:
            return self.join_organizations.filter(id__in=user.profile.organizations.all()).exists()
        return True

    def is_accessible_by(self, user):
        try:
            self.access_check(user)
        except (self.Inaccessible, self.PrivateContest):
            return False
        else:
            return True

    def is_editable_by(self, user):
        # Grade / analytics / in-contest staff tools: edit_all_contest, or being
        # listed as author/curator. This is *not* Django admin's change
        # permission — Teachers often have no ``edit_own_contest``, and the
        # grade page still admits them via membership. Add / Edit / Clone /
        # Delete on the frontend use the admin-aligned helpers below.
        if user.has_perm('judge.edit_all_contest'):
            return True

        # Being listed as an author/curator IS the grant for grading. Sensitive
        # fields inside the assignment editor are still gated by their own
        # permissions via ContestCreate._restricted_fields.
        if user.is_authenticated and user.profile.id in self.editor_ids:
            return True

        return False

    @classmethod
    def can_be_added_by(cls, user):
        """Match ContestAdmin.has_add_permission (Django default: ``add_contest``)."""
        return bool(user.is_authenticated and user.has_perm('judge.add_contest'))

    def is_changeable_by(self, user):
        """Match ContestAdmin.has_change_permission for this contest.

        Admin requires ``edit_own_contest`` first, then ``is_editable_by``
        (``edit_all_contest`` or author/curator).
        """
        if not user.is_authenticated or not user.has_perm('judge.edit_own_contest'):
            return False
        return self.is_editable_by(user)

    def is_viewable_by_org_admin(self, user):
        """Check if user is org admin of any organization this contest belongs to."""
        if not user.is_authenticated:
            return False
        if not self.is_organization_private:
            return False
        return self.organizations.filter(admins=user.profile).exists()

    def linked_organization_ids(self):
        org_ids = set(self.organizations.values_list('id', flat=True))
        org_ids.update(self.classes.values_list('organization_id', flat=True))
        return org_ids

    def is_viewable_by_course_teacher(self, user):
        """Course admins, instructors, and class instructors for linked courses."""
        if not user.is_authenticated:
            return False
        org_ids = self.linked_organization_ids()
        if not org_ids:
            return False
        profile = user.profile
        if Organization.objects.filter(id__in=org_ids, instructors=profile).exists():
            return True
        if Organization.objects.filter(id__in=org_ids, admins=profile).exists():
            return True
        return Class.objects.filter(organization_id__in=org_ids, admins=profile).exists()

    def is_general_course_assignment(self):
        """Public assignment with no course/class scope (Assignments list "General course")."""
        if not self.is_visible or self.is_private or self.is_organization_private:
            return False
        return not self.linked_organization_ids()

    def is_cloneable_by_site_teacher(self, user):
        if not user.is_authenticated or not self.is_general_course_assignment():
            return False
        if user.has_perm('judge.add_organization'):
            return True
        from judge.models.notification import user_in_instructors_group
        return user_in_instructors_group(user)

    def is_cloneable_by(self, user):
        """Match the Django ``clone_contest`` permission.

        ContestAdmin has no Clone action; this is the permission that appears
        on the Contest model's auth list and that original DMOJ's clone view
        required. Source-contest access is still enforced by ContestMixin.
        """
        return bool(user.is_authenticated and user.has_perm('judge.clone_contest'))

    def is_deletable_by(self, user):
        """Match Django admin contest delete.

        ``has_delete_permission`` is ``delete_contest``; opening the object in
        admin also requires change permission (``edit_own_contest`` plus
        ``is_editable_by``). Admin still disables *batch* delete via
        ``NoBatchDeleteMixin``; the frontend list may bulk-delete but each
        row is re-checked here.
        """
        if not user.is_authenticated or not user.has_perm('judge.delete_contest'):
            return False
        return self.is_changeable_by(user)

    _parse_seb_keys = staticmethod(parse_seb_keys)

    # Every SEB accessor below resolves this contest's own value first and falls
    # back to the linked preset, so a contest can inherit a staff-published
    # configuration wholesale yet still override any single part of it.
    def seb_allowed_bek(self):
        return self._parse_seb_keys(self.seb_browser_exam_keys) or \
            (self.seb_preset.allowed_bek() if self.seb_preset_id else [])

    def seb_allowed_config_keys(self):
        return self._parse_seb_keys(self.seb_config_keys) or \
            (self.seb_preset.allowed_config_keys() if self.seb_preset_id else [])

    @property
    def seb_effective_config_file(self):
        if self.seb_config_file:
            return self.seb_config_file
        return self.seb_preset.config_file if self.seb_preset_id else None

    @property
    def seb_effective_quit_url(self):
        if self.seb_quit_url:
            return self.seb_quit_url
        return self.seb_preset.quit_url if self.seb_preset_id else ''

    def user_bypasses_seb(self, user):
        if not user.is_authenticated:
            return False
        if user.has_perm('judge.edit_all_contest'):
            return True
        pid = user.profile.id
        return (pid in self.editor_ids or pid in self.tester_ids or
                pid in self.spectator_ids or self.is_viewable_by_org_admin(user))

    @classmethod
    def get_visible_contests(cls, user):
        if not user.is_authenticated:
            return cls.objects.filter(is_visible=True, is_organization_private=False, is_private=False) \
                              .defer('description').distinct()

        queryset = cls.objects.defer('description')
        if not (user.has_perm('judge.see_private_contest') or user.has_perm('judge.edit_all_contest')):
            # For org-private contests:
            # - If classes are set, only class members can see
            # - If no classes, org members can see
            org_only_check = Q(is_organization_private=True, classes__isnull=True,
                               organizations__in=user.profile.organizations.all())
            class_check = Q(is_organization_private=True,
                            classes__in=user.profile.classes.all())
            org_check = org_only_check | class_check
            q = Q(is_visible=True)
            q &= (
                Q(view_contest_scoreboard=user.profile) |
                Q(is_organization_private=False, is_private=False) |
                Q(is_organization_private=False, is_private=True, private_contestants=user.profile) |
                (Q(is_organization_private=True, is_private=False) & org_check) |
                (Q(is_organization_private=True, is_private=True, private_contestants=user.profile) & org_check)
            )

            q |= Q(authors=user.profile)
            q |= Q(curators=user.profile)
            q |= Q(testers=user.profile)
            q |= Q(spectators=user.profile)
            # Org admins can see org-private contests
            q |= Q(is_organization_private=True, organizations__admins=user.profile)
            teaching_orgs = Organization.objects.filter(
                Q(instructors=user.profile) | Q(admins=user.profile) | Q(class__admins=user.profile),
            ).distinct()
            q |= Q(is_organization_private=True, organizations__in=teaching_orgs)
            q |= Q(is_organization_private=True, classes__organization__in=teaching_orgs)
            queryset = queryset.filter(q)
        return queryset.distinct()

    def rate(self):
        with transaction.atomic():
            Rating.objects.filter(contest__end_time__range=(self.end_time, self._now)).delete()
            for contest in Contest.objects.filter(
                is_rated=True, end_time__range=(self.end_time, self._now),
            ).order_by('end_time'):
                rate_contest(contest)

    class Meta:
        permissions = (
            ('see_private_contest', _('See private contests')),
            ('edit_own_contest', _('Edit own contests')),
            ('edit_all_contest', _('Edit all contests')),
            ('clone_contest', _('Clone contest')),
            ('moss_contest', _('MOSS contest')),
            ('contest_rating', _('Rate contests')),
            ('contest_access_code', _('Contest access codes')),
            ('create_private_contest', _('Create private contests')),
            ('change_contest_visibility', _('Change contest visibility')),
            ('contest_problem_label', _('Edit contest problem label script')),
            ('lock_contest', _('Change lock status of contest')),
            ('override_performance_ceiling', _('Override performance ceiling of contest')),
            ('contest_seb_requirement', _('Configure Safe Exam Browser requirement on contests')),
        )
        verbose_name = _('contest')
        verbose_name_plural = _('contests')


class ContestParticipation(models.Model):
    LIVE = 0
    SPECTATE = -1

    contest = models.ForeignKey(Contest, verbose_name=_('associated contest'), related_name='users', on_delete=CASCADE)
    user = models.ForeignKey(Profile, verbose_name=_('user'), related_name='contest_history', on_delete=CASCADE)
    real_start = models.DateTimeField(verbose_name=_('start time'), default=timezone.now, db_column='start')
    score = models.FloatField(verbose_name=_('score'), default=0, db_index=True)
    cumtime = models.PositiveIntegerField(verbose_name=_('cumulative time'), default=0)
    is_disqualified = models.BooleanField(verbose_name=_('is disqualified'), default=False,
                                          help_text=_('Whether this participation is disqualified.'))
    tiebreaker = models.FloatField(verbose_name=_('tie-breaking field'), default=0.0)
    virtual = models.IntegerField(verbose_name=_('virtual participation id'), default=LIVE,
                                  help_text=_('0 means non-virtual, otherwise the n-th virtual participation.'))
    format_data = JSONField(verbose_name=_('contest format specific data'), null=True, blank=True)
    harvest_scheduled_task_id = models.CharField(
        verbose_name=_('scheduled auto-submit task id'), max_length=255, blank=True, default='',
        help_text=_('Celery task that auto-submits this student’s saved answers when their own '
                    'deadline arrives. Used only when the assignment has a personal time limit.'),
    )

    def recompute_results(self):
        with transaction.atomic():
            self.contest.format.update_participation(self)
            if self.is_disqualified:
                self.score = -9999
                self.cumtime = 0
                self.tiebreaker = 0
                self.save(update_fields=['score', 'cumtime', 'tiebreaker'])
    recompute_results.alters_data = True

    def set_disqualified(self, disqualified):
        self.is_disqualified = disqualified
        self.recompute_results()
        if self.contest.is_rated and self.contest.ratings.exists():
            self.contest.rate()
        if self.is_disqualified:
            if self.user.current_contest == self:
                self.user.remove_contest()
            self.contest.banned_users.add(self.user)
        else:
            self.contest.banned_users.remove(self.user)
    set_disqualified.alters_data = True

    @property
    def live(self):
        return self.virtual == self.LIVE

    @property
    def spectate(self):
        return self.virtual == self.SPECTATE

    @cached_property
    def start(self):
        contest = self.contest
        return contest.start_time if contest.time_limit is None and (self.live or self.spectate) else self.real_start

    @cached_property
    def end_time(self):
        contest = self.contest
        if self.spectate:
            return contest.end_time
        if self.virtual:
            if contest.time_limit:
                return self.real_start + contest.time_limit
            else:
                return self.real_start + (contest.end_time - contest.start_time)
        return contest.end_time if contest.time_limit is None else \
            min(self.real_start + contest.time_limit, contest.end_time)

    @cached_property
    def _now(self):
        # This ensures that all methods talk about the same now.
        return timezone.now()

    @property
    def ended(self):
        return self.end_time is not None and self.end_time < self._now

    @property
    def time_remaining(self):
        end = self.end_time
        if end is not None and end >= self._now:
            return end - self._now

    def __str__(self):
        if self.spectate:
            return _('%(user)s spectating in %(contest)s') % {'user': self.user.username, 'contest': self.contest.name}
        if self.virtual:
            return _('%(user)s in %(contest)s, v%(id)d') % {
                'user': self.user.username, 'contest': self.contest.name, 'id': self.virtual,
            }
        return _('%(user)s in %(contest)s') % {'user': self.user.username, 'contest': self.contest.name}

    class Meta:
        verbose_name = _('contest participation')
        verbose_name_plural = _('contest participations')

        unique_together = ('contest', 'user', 'virtual')


class ContestProblem(models.Model):
    problem = models.ForeignKey(Problem, verbose_name=_('problem'), related_name='contests', on_delete=CASCADE)
    contest = models.ForeignKey(Contest, verbose_name=_('contest'), related_name='contest_problems', on_delete=CASCADE)
    points = models.IntegerField(verbose_name=_('points'))
    partial = models.BooleanField(default=True, verbose_name=_('partial'))
    is_pretested = models.BooleanField(default=False, verbose_name=_('is pretested'))
    order = models.PositiveIntegerField(db_index=True, verbose_name=_('order'))
    output_prefix_override = models.IntegerField(verbose_name=_('output prefix length override'),
                                                 default=0, null=True, blank=True)
    max_submissions = models.IntegerField(verbose_name=_('max submissions'),
                                          help_text=_('Maximum number of submissions for this problem, '
                                                      'or leave blank for no limit.'),
                                          default=None, null=True, blank=True,
                                          validators=[MinValueOrNoneValidator(1, _('Why include a problem you '
                                                                                   "can't submit to?"))])
    hide_results = models.BooleanField(verbose_name=_('hide results in contest'),
                                       help_text=_("Whether to hide this problem's results from students until the "
                                                   'contest is finalized. Leave unset to use the default for the '
                                                   'question type (hidden for multiple-choice, shown for code). Only '
                                                   'applies to exam-style contests.'),
                                       default=None, null=True, blank=True)
    display_name = models.CharField(max_length=100, blank=True, default='',
                                    verbose_name=_('display name'))

    @property
    def results_hidden(self):
        """Whether this problem's results are hidden during the contest (override, else type default)."""
        if self.hide_results is not None:
            return self.hide_results
        from judge.question_types import get_grader
        return get_grader(self.problem.submission_type).hide_results_in_contest

    @property
    def score_uses_latest(self):
        """Whether scoring uses the latest (vs best) submission for this problem (type policy)."""
        from judge.question_types import get_grader
        return get_grader(self.problem.submission_type).contest_score_uses_latest

    class Meta:
        unique_together = ('problem', 'contest')
        verbose_name = _('contest problem')
        verbose_name_plural = _('contest problems')
        ordering = ('order',)


def contest_problem_display_name(cp, language_code=None) -> str:
    """Assignment-local title for a contest problem.

    A non-empty ``display_name`` wins over the Problem's i18n translation and
    canonical name. Empty string means "use the original Problem name".
    """
    if cp.display_name:
        return cp.display_name
    if language_code:
        return cp.problem.translated_name(language_code)
    return cp.problem.name


class ContestSubmission(models.Model):
    submission = models.OneToOneField(Submission, verbose_name=_('submission'),
                                      related_name='contest', on_delete=CASCADE)
    problem = models.ForeignKey(ContestProblem, verbose_name=_('problem'), on_delete=CASCADE,
                                related_name='submissions', related_query_name='submission')
    participation = models.ForeignKey(ContestParticipation, verbose_name=_('participation'), on_delete=CASCADE,
                                      related_name='submissions', related_query_name='submission')
    points = models.FloatField(default=0.0, verbose_name=_('points'))
    is_pretest = models.BooleanField(verbose_name=_('is pretested'),
                                     help_text=_('Whether this submission was ran only on pretests.'),
                                     default=False)

    class Meta:
        verbose_name = _('contest submission')
        verbose_name_plural = _('contest submissions')


class Rating(models.Model):
    user = models.ForeignKey(Profile, verbose_name=_('user'), related_name='ratings', on_delete=CASCADE)
    contest = models.ForeignKey(Contest, verbose_name=_('contest'), related_name='ratings', on_delete=CASCADE)
    participation = models.OneToOneField(ContestParticipation, verbose_name=_('participation'),
                                         related_name='rating', on_delete=CASCADE)
    rank = models.IntegerField(verbose_name=_('rank'))
    rating = models.IntegerField(verbose_name=_('rating'))
    mean = models.FloatField(verbose_name=_('raw rating'))
    performance = models.FloatField(verbose_name=_('contest performance'))
    last_rated = models.DateTimeField(db_index=True, verbose_name=_('last rated'))

    class Meta:
        unique_together = ('user', 'contest')
        verbose_name = _('contest rating')
        verbose_name_plural = _('contest ratings')


class ContestMoss(models.Model):
    LANG_MAPPING = [
        ('C', MOSS_LANG_C),
        ('C++', MOSS_LANG_CC),
        ('Java', MOSS_LANG_JAVA),
        ('Python', MOSS_LANG_PYTHON),
    ]

    contest = models.ForeignKey(Contest, verbose_name=_('contest'), related_name='moss', on_delete=CASCADE)
    problem = models.ForeignKey(Problem, verbose_name=_('problem'), related_name='moss', on_delete=CASCADE)
    language = models.CharField(max_length=10)
    submission_count = models.PositiveIntegerField(default=0)
    url = models.URLField(null=True, blank=True)

    class Meta:
        unique_together = ('contest', 'problem', 'language')
        verbose_name = _('contest moss result')
        verbose_name_plural = _('contest moss results')
