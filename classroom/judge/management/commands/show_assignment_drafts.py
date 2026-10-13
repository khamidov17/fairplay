"""Print assignment drafts in a form a teacher can read without the Drafts page.

``judge_assignmentdraft.content`` stores MCQ option row ids and file answers live
in object storage, so a raw SQL dump is not enough. This command joins those
pieces and can copy uploaded files onto disk.
"""
import re
from pathlib import Path

from django.core.management.base import BaseCommand, CommandError

from judge.models import AssignmentDraft, Contest, ContestParticipation
from judge.utils.assignment_draft import format_draft_preview

_UNSAFE_CHARS = re.compile(r'[^\w.\-]+', re.UNICODE)


def safe_export_leaf(value, fallback='file'):
    """Keep only the last path component so a stored filename cannot leave save_dir."""
    raw = str(value or '').replace('\\', '/').replace('\x00', '')
    leaf = raw.rsplit('/', 1)[-1]
    if leaf in ('', '.', '..'):
        return fallback
    cleaned = _UNSAFE_CHARS.sub('_', leaf).strip('._')
    return (cleaned or fallback)[:180]


def export_path_under(save_dir, user, problem_code, original_name):
    """Build ``save_dir / student_problem_filename`` and refuse to leave ``save_dir``."""
    save_root = Path(save_dir).expanduser().resolve()
    filename = '%s_%s_%s' % (
        safe_export_leaf(user, 'student'),
        safe_export_leaf(problem_code, 'problem'),
        safe_export_leaf(original_name, 'upload'),
    )
    dest = (save_root / filename).resolve()

    if not dest.is_relative_to(save_root):
        raise CommandError('Refusing to write outside %s' % save_root)
    return dest


class Command(BaseCommand):
    help = 'Show saved assignment drafts with choice text and original filenames.'

    def add_arguments(self, parser):
        parser.add_argument('contest_key', help='Assignment key, e.g. c000018')
        parser.add_argument('--user', dest='username', default='',
                            help='Limit to this username.')
        parser.add_argument('--save-files', dest='save_files', default='',
                            help='Directory to copy file-upload drafts into.')

    def handle(self, *args, **options):
        contest = Contest.objects.filter(key=options['contest_key']).first()
        if contest is None:
            raise CommandError('No assignment with key %s' % options['contest_key'])
        drafts = (
            AssignmentDraft.objects
            .filter(
                participation__contest=contest,
                participation__virtual=ContestParticipation.LIVE,
            )
            .select_related('participation__user__user', 'problem', 'language')
            .prefetch_related('upload', 'problem__mcq_options')
            .order_by('participation__user__user__username', 'problem__code')
        )
        username = (options['username'] or '').strip()
        if username:
            drafts = drafts.filter(participation__user__user__username=username)

        save_dir = Path(options['save_files']).expanduser() if options['save_files'] else None
        if save_dir is not None:
            save_dir.mkdir(parents=True, exist_ok=True)

        count = 0
        for draft in drafts:
            upload = getattr(draft, 'upload', None)
            if not draft.has_content() and upload is None:
                continue
            count += 1
            user = draft.participation.user.username
            self.stdout.write('---')
            self.stdout.write('student: %s' % user)
            self.stdout.write('problem: %s (%s)' % (draft.problem.code, draft.problem.name))
            self.stdout.write('type: %s' % draft.problem.submission_type)
            self.stdout.write('saved: %s' % draft.updated_on)
            if draft.language_id:
                self.stdout.write('language: %s' % draft.language.key)
            try:
                self.stdout.write(format_draft_preview(draft))
            except Exception as exc:
                self.stdout.write('unreadable: %s' % exc)
            if save_dir is not None and upload is not None and upload.file:
                dest = export_path_under(save_dir, user, draft.problem.code, upload.original_name)
                with upload.file.open('rb') as src, dest.open('wb') as out:
                    while True:
                        chunk = src.read(65536)
                        if not chunk:
                            break
                        out.write(chunk)
                self.stdout.write('copied: %s' % dest)
        if count == 0:
            self.stdout.write('No saved drafts.')
        else:
            self.stdout.write('---')
            self.stdout.write('%s draft(s)' % count)
