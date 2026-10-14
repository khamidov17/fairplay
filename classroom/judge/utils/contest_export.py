"""Paper-exam export of an assignment (``/contest/<key>/export-pdf/`` and ``/export-word/``).

The assignment's statements are rendered to HTML and converted by pandoc (lualatex for PDF).
Problem text is instructor-controlled, so the conversion is hardened:

* only editors of the assignment may export (``can_export_contest``); the statements of an
  upcoming exam must not leak to students;
* titles and the assignment description are HTML-escaped before they reach pandoc;
* pandoc runs the ``pandoc_export_filter.lua`` filter, which neutralises TeX math / raw TeX that uses
  file, Lua or definition primitives and drops every image the server did not resolve to a local
  file (so pandoc never fetches a URL or reads a path from the statement); PDF runs also use
  ``--sandbox``;
* lualatex runs with shell escape off and kpathsea ``openin_any=p`` / ``openout_any=p``, so even a
  TeX construct that slipped past the filter cannot \\input a file outside pandoc's temporary
  directory.
"""
import html
import mimetypes
import os
import posixpath
import re
import shutil
import subprocess
import tempfile
from urllib.parse import unquote, urlsplit

import markdown
from django.conf import settings
from django.utils import timezone
from markdown_katex import KatexExtension

from judge.models import contest_problem_display_name

FILTER_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'pandoc_export_filter.lua')
PANDOC_TIMEOUT_SECONDS = 180
MAX_EXPORT_IMAGE_BYTES = 10 * 1024 * 1024
EXPORT_IMAGE_EXTENSIONS = {'.png', '.jpg', '.jpeg', '.gif'}
INPUT_FORMAT = 'html+tex_math_dollars+tex_math_single_backslash'

_IMG_SRC_RE = re.compile(r'<img\b[^>]*?\bsrc\s*=\s*(?:"([^"]*)"|\'([^\']*)\'|([^\s>]+))', re.IGNORECASE)
_EDITOR_FILE_RE = re.compile(r'^/widgets/files/([0-9a-fA-F-]{32,36})/?$')


class ContestExportError(Exception):
    pass


def can_export_contest(user, contest):
    """Statements may be exported only by the assignment's editors (authors, curators, edit_all)."""
    return contest.is_editable_by(user)


def _render_description(description):
    return markdown.markdown(
        re.sub(r'~([^~]+?)~', r'$\1$', description or ''),
        extensions=[
            'markdown.extensions.extra',
            'markdown.extensions.codehilite',
            'markdown.extensions.tables',
            'markdown.extensions.toc',
            'markdown.extensions.nl2br',
            'markdown.extensions.sane_lists',
            KatexExtension(),
        ],
        output_format='html5',
    )


def _format_time(value):
    # The active time zone is the exporting teacher's (TimezoneMiddleware).
    return timezone.localtime(value).strftime('%Y-%m-%d %H:%M %Z') if value else ''


def _assignment_points(cp):
    points = getattr(cp, 'points', None)
    return cp.problem.points if points is None else points


def _format_points(value):
    return ('%g' % value) if value is not None else ''


def _render_choices(problem):
    """The answer choices of a multiple-choice question (never which one is correct)."""
    from judge.models import ProblemSubmissionType
    if getattr(problem, 'submission_type', None) != ProblemSubmissionType.MCQ:
        return ''
    options = list(problem.mcq_options.order_by('order'))
    if not options:
        return ''
    config = getattr(problem, 'mcq_config', None)
    hint = '<p><em>Select all that apply.</em></p>\n' if config is not None and config.multiple_select else ''
    items = ''.join(f'<li>{_render_description(option.text)}</li>\n' for option in options)
    return f'<div class="problem-choices">\n{hint}<ol type="A">\n{items}</ol>\n</div>\n'


def build_contest_export_html(contest, contest_problems):
    """Return the HTML document that pandoc turns into the paper exam."""
    contest_problems = list(contest_problems)
    count = len(contest_problems)
    # Points as set on the assignment, which may differ from the problem's own points.
    total_score = sum(_assignment_points(cp) for cp in contest_problems)
    esc = html.escape
    parts = [
        '<!DOCTYPE html>\n<html>\n<head>\n<meta charset="UTF-8">\n'
        f'<title>{esc(contest.name)}</title>\n</head>\n<body>\n'
        '<div class="page cover-page">\n<div class="contest-info">\n'
        f'<div><span class="bold">Start Time: </span>{esc(_format_time(contest.start_time))}</div>\n'
        f'<div><span class="bold">End Time: </span>{esc(_format_time(contest.end_time))}</div>\n'
        f'<div><span class="bold">Total Problems: </span>{count}</div>\n'
        '<div style="margin-top: 10mm;"><span class="bold">Student Name: </span>____________________</div>\n'
        '<div><span class="bold">Student NetID: </span>____________________</div>\n'
        '</div>\n<div class="instructions">\n<ul>\n'
        f'<li>Total Score: {esc(_format_points(total_score))}</li>\n'
        f'<li>Pages: {count + 1}</li>\n'
        f'{esc(contest.description or "")}\n'
        '</ul>\n'
        f'<div class="text-right italic">—— {timezone.now().strftime("%m/%d/%Y")} ——</div>\n'
        '</div>\n</div>\n',
    ]
    for page_num, cp in enumerate(contest_problems, 1):
        points = esc(_format_points(_assignment_points(cp)))
        last = 'last-page' if page_num == count else ''
        header = (
            f'<div class="page-header"><div class="problem-info">Problem {page_num} / {count} <br>'
            f'<span class="problem-points">{points} - points</span></div></div>\n'
        )
        footer = (
            f'<div class="page-footer"><div class="text-left">Problem {page_num}</div>'
            f'<div class="text-right">Points: {points}</div></div>\n'
        )
        parts.append(
            f'<!-- Problem {page_num} -->\n<div class="page {last}">\n{header}'
            '<div class="problem-content">\n'
            f'<div class="problem-title">Problem {page_num}: {esc(contest_problem_display_name(cp))}</div>\n'
            f'<div class="problem-description">\n{_render_description(cp.problem.description)}\n</div>\n'
            f'{_render_choices(cp.problem)}'
            f'</div>\n{footer}</div>\n',
        )
        parts.append(
            f'<!-- Problem {page_num} -->\n<div class="page {last}">\n{header}'
            '<div class="answer-area">\n'
            '<div class="answer-header">Answer Area (write down your answer in this area)</div>\n'
            '<div class="answer-box"></div>\n</div>\n'
            f'{footer}'
            '<div class="student-info">\n'
            '<div class="info-field">Student Name: ________________</div>\n'
            '<div class="info-field">Student NetID: ________________</div>\n'
            '<div class="info-field">Score For this Problem: ________________</div>\n'
            '</div>\n</div>\n',
        )
    parts.append('</body>\n</html>\n')
    return ''.join(parts)


def _read_media_image(path):
    media_url = urlsplit(settings.MEDIA_URL or '').path
    if not media_url.strip('/') or not path.startswith(media_url):
        return None
    relative = posixpath.normpath(path[len(media_url):]).lstrip('/')
    if not relative or relative.startswith('..') or relative.split('/')[0] == 'problem_import_test_data':
        return None
    root = os.path.realpath(settings.MEDIA_ROOT)
    full = os.path.realpath(os.path.join(root, *relative.split('/')))
    if os.path.commonpath((full, root)) != root or not os.path.isfile(full):
        return None
    if os.path.getsize(full) > MAX_EXPORT_IMAGE_BYTES:
        return None
    with open(full, 'rb') as f:
        return f.read()


def _read_editor_attachment_image(path):
    match = _EDITOR_FILE_RE.match(path)
    if not match:
        return None, ''
    from judge.models import EditorAttachment
    try:
        attachment = EditorAttachment.objects.get(uuid=match.group(1))
    except (EditorAttachment.DoesNotExist, ValueError):
        return None, ''
    if attachment.size > MAX_EXPORT_IMAGE_BYTES or not attachment.file:
        return None, ''
    with attachment.file.open('rb') as f:
        return f.read(MAX_EXPORT_IMAGE_BYTES + 1), attachment.original_name


def _resolve_image(src):
    """Return ``(bytes, extension)`` for an image this site stores itself, else ``(None, '')``."""
    try:
        parts = urlsplit(html.unescape(src).strip())
    except ValueError:
        return None, ''
    if parts.scheme not in ('', 'http', 'https'):
        return None, ''
    path = unquote(parts.path)
    content = _read_media_image(path)
    name = path
    if content is None:
        content, name = _read_editor_attachment_image(path)
    if not content or len(content) > MAX_EXPORT_IMAGE_BYTES:
        return None, ''
    extension = os.path.splitext(name)[1].lower()
    if extension not in EXPORT_IMAGE_EXTENSIONS:
        return None, ''
    return content, extension


def _write_image_map(html_content, workdir):
    """Copy every resolvable site image into ``workdir`` and write the filter's image map."""
    lines = []
    seen = set()
    for match in _IMG_SRC_RE.finditer(html_content):
        raw_src = next(group for group in match.groups() if group is not None)
        # Pandoc hands the filter the attribute value with entities decoded.
        src = html.unescape(raw_src)
        if src in seen or '\t' in src or '\n' in src:
            continue
        seen.add(src)
        content, extension = _resolve_image(src)
        if content is None:
            continue
        local_path = os.path.join(workdir, 'export-image-%d%s' % (len(lines) + 1, extension))
        with open(local_path, 'wb') as f:
            f.write(content)
        mime = mimetypes.types_map.get(extension, 'application/octet-stream')
        lines.append('%s\t%s\t%s' % (src, local_path, mime))
    map_path = os.path.join(workdir, 'image-map.tsv')
    with open(map_path, 'w', encoding='utf-8') as f:
        f.write('\n'.join(lines) + ('\n' if lines else ''))
    return map_path


def pandoc_environment(workdir):
    env = {key: value for key, value in os.environ.items() if not key.lower().startswith(('openin_', 'openout_'))}
    env.update({
        # kpathsea: TeX may read and write only below its working/output directory.
        'openin_any': 'p',
        'openout_any': 'p',
        'shell_escape': 'f',
        'TMPDIR': workdir,
    })
    return env


def pandoc_command(output_format, output_path, image_map_path):
    try:
        import pypandoc
        pandoc = pypandoc.get_pandoc_path()
    except Exception:
        pandoc = shutil.which('pandoc') or 'pandoc'
    args = [
        pandoc, '-f', INPUT_FORMAT, '-t', output_format, '-o', output_path,
        '--lua-filter', FILTER_PATH,
        '-M', 'fairplay-image-map=%s' % image_map_path,
        '--standalone', '--wrap=preserve', '--highlight-style=pygments',
    ]
    if output_format == 'pdf':
        args += [
            # No reading of files or URLs by pandoc itself. (pandoc 3.1's docx writer cannot find its
            # own data files under --sandbox; for docx the filter alone keeps pandoc from fetching,
            # because every image it would load is either pre-resolved or replaced by its alt text.)
            '--sandbox',
            '--pdf-engine=lualatex',
            '--pdf-engine-opt=-no-shell-escape',
            '-V', 'geometry:margin=1in',
            '-V', 'fontsize=11pt',
            '-V', 'linestretch=1.4',
            '-V', 'classoption=oneside',
        ]
    else:
        args += ['--columns=80']
    return args


def convert_contest_html(html_content, output_format):
    """Run the hardened pandoc conversion; return the output bytes or raise ContestExportError."""
    if output_format not in ('pdf', 'docx'):
        raise ValueError(output_format)
    workdir = tempfile.mkdtemp(prefix='contest-export-')
    try:
        output_path = os.path.join(workdir, 'export.%s' % output_format)
        image_map_path = _write_image_map(html_content, workdir)
        try:
            result = subprocess.run(
                pandoc_command(output_format, output_path, image_map_path),
                input=html_content.encode('utf-8'),
                stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                cwd=workdir, env=pandoc_environment(workdir),
                timeout=PANDOC_TIMEOUT_SECONDS,
            )
        except (OSError, subprocess.TimeoutExpired) as error:
            raise ContestExportError(str(error)) from error
        if result.returncode != 0 or not os.path.exists(output_path):
            raise ContestExportError(result.stderr.decode('utf-8', 'replace')[-2000:] or 'pandoc failed')
        with open(output_path, 'rb') as f:
            return f.read()
    finally:
        shutil.rmtree(workdir, ignore_errors=True)
