"""Review-first Excel import for code, multiple-choice, and fill-blank problems."""
import errno
import io
import json
import logging
import mimetypes
import os
import re
import shutil
import uuid
import zipfile
from urllib.parse import urlencode
from xml.etree import ElementTree as ET

from django.contrib.auth.mixins import LoginRequiredMixin
from django.core.exceptions import PermissionDenied
from django.core.files.base import ContentFile
from django.core.files.storage import default_storage
from django.db import IntegrityError, transaction
from django.http import Http404, HttpResponse, JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils import timezone
from django.utils.translation import gettext as _
from django.views import View

from judge import llm
from judge.llm import LLMError
from judge.models import Language, Problem, ProblemBlank, ProblemData, ProblemFillBlankConfig, \
    ProblemImportDraft, \
    ProblemImportImage, ProblemImportItem, ProblemMCQConfig, ProblemMCQOption, ProblemShortAnswerConfig, \
    ProblemSubmissionType, ProblemTestCase, ProblemType, Solution, problem_data_storage
from judge.utils.excel_embedded_documents import image_placeholder, read_xlsx_rows_with_embedded_files, \
    store_docx_image
from judge.utils.problem_create import apply_simple_problem_defaults, default_problem_type, log_problem_admin_entry
from judge.utils.problem_data import ProblemDataCompiler
from judge.utils.safe_json import script_json
from judge.utils.simple_xlsx import validate_xlsx_upload
from judge.views.problem_create_v2 import ProblemCreateV2View, assignable_organizations


logger = logging.getLogger('judge.problem_import')

QUESTION_TYPE_MAP = {
    'code': ProblemSubmissionType.CODE,
    'c': ProblemSubmissionType.CODE,
    'multiple_choice': ProblemSubmissionType.MCQ,
    'multiple choice': ProblemSubmissionType.MCQ,
    'mcq': ProblemSubmissionType.MCQ,
    'm': ProblemSubmissionType.MCQ,
    'fill_blank': ProblemSubmissionType.FILL_BLANK,
    'fill in the blank': ProblemSubmissionType.FILL_BLANK,
    'fill-in-the-blank': ProblemSubmissionType.FILL_BLANK,
    'b': ProblemSubmissionType.FILL_BLANK,
    'programming': ProblemSubmissionType.CODE,
    'programming_problem': ProblemSubmissionType.CODE,
    'code_problem': ProblemSubmissionType.CODE,
    'coding': ProblemSubmissionType.CODE,
    'coding_problem': ProblemSubmissionType.CODE,
    'algorithm': ProblemSubmissionType.CODE,
    'algorithmic': ProblemSubmissionType.CODE,
    'choice': ProblemSubmissionType.MCQ,
    'single_choice': ProblemSubmissionType.MCQ,
    'multiple_choice_question': ProblemSubmissionType.MCQ,
    'multiple_choice_problem': ProblemSubmissionType.MCQ,
    'single_choice_question': ProblemSubmissionType.MCQ,
    'multiple_selection': ProblemSubmissionType.MCQ,
    'single_selection': ProblemSubmissionType.MCQ,
    'fill_blank_question': ProblemSubmissionType.FILL_BLANK,
    'fill_in_blank': ProblemSubmissionType.FILL_BLANK,
    'fill_in_blanks': ProblemSubmissionType.FILL_BLANK,
    'blank_filling': ProblemSubmissionType.FILL_BLANK,
    'cloze': ProblemSubmissionType.FILL_BLANK,
    'short_answer': ProblemSubmissionType.SHORT_ANSWER,
    's': ProblemSubmissionType.SHORT_ANSWER,
    'short answer': ProblemSubmissionType.SHORT_ANSWER,
    'short_response': ProblemSubmissionType.SHORT_ANSWER,
    'short response': ProblemSubmissionType.SHORT_ANSWER,
    'open_response': ProblemSubmissionType.SHORT_ANSWER,
    'open response': ProblemSubmissionType.SHORT_ANSWER,
    'essay': ProblemSubmissionType.SHORT_ANSWER,
    'true_false': ProblemSubmissionType.MCQ,
    'true false': ProblemSubmissionType.MCQ,
    'truefalse': ProblemSubmissionType.MCQ,
    'tf': ProblemSubmissionType.MCQ,
    'boolean': ProblemSubmissionType.MCQ,
}

BLANK_SIGN_RE = re.compile(r'\[\[blank\]\]', re.IGNORECASE)
MCQ_OPTION_LINE_RE = re.compile(r'^\s*([A-Z])\s*[.)]\s*(\S(?:.*\S)?)\s*$')
HEADER_ALIASES = {
    'explanation': 'solution',
    'problem_type': 'question_type',
    'tag': 'tags',
    'title': 'name',
    'problem_name': 'name',
    'problem_title': 'name',
    'question': 'name',
    'question_name': 'name',
    'question_title': 'name',
    'type': 'question_type',
    'question_category': 'question_type',
    'question_kind': 'question_type',
    'content': 'description',
    'problem_description': 'description',
    'question_description': 'description',
    'question_text': 'description',
    'statement': 'description',
    'answer_key': 'answer',
    'correct_answer': 'answer',
    'ai_rubric': 'rubric',
    'grading_rubric': 'rubric',
    'grading_table_json': 'grading_table',
}
DIFFICULTY_VALUES = {'0': 'easy', '1': 'medium', '2': 'hard'}
IMPORT_COLUMNS = {
    'name', 'question_type', 'description', 'answer', 'solution', 'tags', 'difficulty', 'organizations',
    'allowed_languages', 'test_data', 'options', 'correct_options', 'blanks', 'rubric', 'grading_table',
}


def _require_creator(request):
    if not request.user.has_perm('judge.create_private_problem'):
        raise PermissionDenied


def _draft_for(request, draft_id):
    _require_creator(request)
    return get_object_or_404(ProblemImportDraft, pk=draft_id, owner=request.user.profile)


def _draft_image_url(draft_id, token):
    # XLSX parsing uses UUID hex strings; the URL converter requires the canonical form.
    return reverse('problem_import_image', args=(draft_id, uuid.UUID(str(token))))


def _store_draft_images(item, documents):
    """Persist validated figures with the draft, never in public media storage."""
    images = [image for document in documents for image in document.images]
    if not images:
        return
    for image in images:
        ProblemImportImage.objects.create(
            item=item, token=image.token, filename=image.filename, content=image.content,
        )
        item.description = item.description.replace(
            image_placeholder(image.token), _draft_image_url(item.draft_id, image.token),
        )
    item.save(update_fields=['description', 'updated_at'])


def _promote_draft_images(item, description):
    """Replace still-used private preview URLs with permanent Markdown image URLs."""
    stored_names = []
    try:
        for image in item.images.all():
            preview_url = _draft_image_url(item.draft_id, image.token)
            if preview_url not in description:
                continue
            stored_name, public_url = store_docx_image(image.filename, bytes(image.content))
            stored_names.append(stored_name)
            description = description.replace(preview_url, public_url)
    except Exception:
        _delete_stored_images(stored_names)
        raise
    return description, stored_names


def _delete_stored_images(names):
    """Best-effort cleanup if a transaction cannot create its problem."""
    for name in names:
        try:
            default_storage.delete(name)
        except OSError:
            pass


def _normalise_type(value):
    key = re.sub(r'[^a-z0-9]+', '_', str(value or '').strip().casefold()).strip('_')
    try:
        return QUESTION_TYPE_MAP[key]
    except KeyError:
        if any(token in key for token in ('code', 'coding', 'programming', 'algorithm')):
            return ProblemSubmissionType.CODE
        if any(token in key for token in ('choice', 'select', 'selection', 'multiple_answer', 'single_answer')):
            return ProblemSubmissionType.MCQ
        if any(token in key for token in ('blank', 'cloze', 'fill')):
            return ProblemSubmissionType.FILL_BLANK
        if any(token in key for token in ('short_answer', 'short_response', 'open_response', 'essay')):
            return ProblemSubmissionType.SHORT_ANSWER
        raise ValueError('question_type must be code, multiple_choice, fill_blank, or short_answer.')


def _is_true_false_type(value):
    key = re.sub(r'[^a-z0-9]+', '_', str(value or '').strip().casefold()).strip('_')
    return key in ('true_false', 'truefalse', 'tf', 'boolean')


def _import_question_type(value):
    """Keep an unrecognized row editable instead of rejecting the whole workbook."""
    try:
        return _normalise_type(value), ''
    except ValueError:
        return ProblemSubmissionType.CODE, _(
            'Question type "%(value)s" was not recognized. It is shown as Code; select the correct type before '
            'creating the problem.',
        ) % {'value': value or _('empty')}


def _import_problem_name(value, row_number):
    name = str(value or '').strip()
    if name:
        return name, False
    return 'Missing name problem {}'.format(row_number), True


def _review_message(item):
    messages = []
    if item.name_needs_review:
        messages.append(_('Replace the generated missing-name placeholder before creating this problem.'))
    if item.type_needs_review:
        messages.append(_('Select or confirm the question type before creating this problem.'))
    if item.data_needs_review:
        messages.append(_('Review the test-data loading warning before creating this problem.'))
    if item.config_needs_review:
        messages.append(_('Review and confirm the unrecognized imported configuration before creating this problem.'))
    return '\n'.join(messages)


def _value(row, headers, name):
    index = headers.get(name)
    return str(row[index]).strip() if index is not None and index < len(row) and row[index] is not None else ''


def _header_name(value):
    """Normalize the annotated headings used by the teacher-facing workbook."""
    name = re.sub(r'\s+', ' ', str(value or '').replace('*', '').strip().lower())
    name = re.split(r'[（(]', name, maxsplit=1)[0].strip().replace(' ', '_')
    return HEADER_ALIASES.get(name, name)


def _serialize_fill_blanks(blanks):
    """Store labeled blanks in a reviewable text format without a schema migration."""
    blocks = []
    for index, blank in enumerate(blanks, start=1):
        answers = [str(answer).strip() for answer in blank.get('answers', []) if str(answer).strip()]
        if not answers:
            continue
        lines = ['[blank {}]'.format(index)]
        label = str(blank.get('label', '')).strip()
        if label:
            lines.append('label: {}'.format(label))
        lines.append('answers:')
        lines.extend(answers)
        blocks.append('\n'.join(lines))
    return '\n---\n'.join(blocks)


def _serialize_editor_testcases(testcases):
    """Convert the unified editor's inline cases back to the Excel text representation."""
    lines = []
    for index, testcase in enumerate(testcases, start=1):
        if not isinstance(testcase, dict):
            continue
        input_data = str(testcase.get('input') or '')
        output_data = str(testcase.get('output') or '')
        if not input_data and not output_data:
            continue
        lines.append('input data {}: {}'.format(index, input_data))
        lines.append('output data {}: {}'.format(index, output_data))
    return '\n'.join(lines)


def _safe_json(value):
    """Serialize inline editor data without allowing it to escape a script tag."""
    return script_json(value)


def _editor_data_for_item(item):
    """Return an import item in the exact shape consumed by create_v2's loadEditData."""
    data = {
        'name': item.name,
        'description': item.description,
        'points': 10,
        'is_public': True,
        'partial': False,
        'ai': False,
        'ai_hints_enabled': False,
        'organizations': [],
        'group_id': '',
        'difficulty': DIFFICULTY_VALUES.get(item.difficulty, item.difficulty) or 'medium',
        'problemTypes': _split_lines(item.tags),
        'solution': {'is_public': False, 'publish_on': '', 'content': item.solution, 'authors': []},
        'authors': [],
        'curators': [],
        'testers': [],
        'banned_users': [],
        'classes': [],
        'language_limits': [],
        'templates': [],
        'clarifications': [],
        'translations': [],
    }
    if item.submission_type == ProblemSubmissionType.CODE:
        data.update({
            'type': 'programming',
            'time_limit': 1,
            'memory_limit': 256,
            'languages': _preview_language_keys(item),
            'checker': '',
            'testcases': _editor_testcases_for_item(item),
        })
    elif item.submission_type == ProblemSubmissionType.MCQ:
        if item.is_true_false:
            data.update({'type': 'truefalse', 'tfAnswer': 'true' if item.answer == '1' else 'false'})
        else:
            options = _preview_options(item)
            data.update({
                'type': 'multiple' if sum(option['is_correct'] for option in options) > 1 else 'single',
                'options': [{'text': option['text'], 'correct': option['is_correct']} for option in options],
            })
    elif item.submission_type == ProblemSubmissionType.FILL_BLANK:
        data.update({
            'type': 'blank',
            'blanks': ['\n'.join(answers) for _label, answers in _preview_blanks(item)],
            'rubric': item.rubric,
            'gradingTable': _grading_table_value(item.grading_table),
            'ai': False,
            'trimWhitespace': True,
            'caseSensitive': False,
        })
    else:
        data.update({
            'type': 'short',
            'shortAnswer': item.answer,
            'rubric': item.rubric,
            'gradingTable': _grading_table_value(item.grading_table),
            'ai': False,
        })
    # Upload parsing may prefill only organizations; preserve the complete editor shape.
    data.update(item.editor_data or {})
    return data


def _grading_table_value(value):
    try:
        parsed = json.loads(value) if value else []
    except (TypeError, ValueError):
        return []
    return parsed if isinstance(parsed, list) else []


def _editor_testcases_for_item(item):
    try:
        if item.test_data_zip:
            _payload, cases = _read_test_data_zip(item)
            return [
                {
                    'input': input_data.decode('utf-8', 'replace'),
                    'output': output_data.decode('utf-8', 'replace'),
                    'input_file': input_name,
                    'output_file': output_name,
                    'points': 1,
                    'pretest': False,
                }
                for input_name, input_data, output_name, output_data in cases
            ]
        return [
            {'input': input_data, 'output': output_data, 'points': 1, 'pretest': False}
            for _input_name, input_data, _output_name, output_data in _parse_text_test_data(item.test_data)
        ]
    except ValueError:
        return []


def _parse_fill_blanks(value):
    """Return (label, accepted_answers) pairs from the preview's blank format.

    Legacy blocks that contain only answer lines are still accepted and have no label.
    """
    result = []
    for raw_block in re.split(r'\n\s*---\s*\n', value or ''):
        lines = [line.strip() for line in raw_block.splitlines() if line.strip()]
        if not lines:
            continue
        if not re.match(r'^\[blank(?:\s+\d+)?\]$', lines[0], re.IGNORECASE):
            result.append(('', '\n'.join(lines)))
            continue
        label = ''
        answers = []
        reading_answers = False
        for line in lines[1:]:
            if not reading_answers and line.lower().startswith('label:'):
                label = line.split(':', 1)[1].strip()
            elif line.lower().startswith('answers:'):
                reading_answers = True
                first_answer = line.split(':', 1)[1].strip()
                if first_answer:
                    answers.append(first_answer)
            elif reading_answers:
                answers.append(line)
            else:
                raise ValueError('Each formatted blank needs an "answers:" line.')
        if not answers:
            raise ValueError('Each blank needs at least one accepted answer.')
        result.append((label, '\n'.join(answers)))
    return result


def _remove_multiple_choice_option_block(description, option_block):
    """Remove only an exact LLM-identified source block; never rewrite the prompt."""
    if not option_block or option_block not in description:
        return description
    start = description.index(option_block)
    before = description[:start]
    after = description[start + len(option_block):]
    # The option block occupies complete lines, so keep its removal from adding a blank paragraph.
    if before.endswith('\n') and after.startswith('\n'):
        after = after[1:]
    result = before + after
    result = re.sub(r'[ \t]+\n', '\n', result)
    result = re.sub(r'\n{3,}', '\n\n', result)
    return result.strip()


def _extract_labeled_multiple_choice_options(description):
    """Extract only a complete, explicitly labeled multiple-choice option block."""
    lines = (description or '').splitlines()
    for start, line in enumerate(lines):
        first = MCQ_OPTION_LINE_RE.match(line)
        if first is None or first.group(1) != 'A':
            continue
        options = [first.group(2)]
        end = start + 1
        while end < len(lines):
            match = MCQ_OPTION_LINE_RE.match(lines[end])
            expected_label = chr(ord('A') + len(options))
            if match is None or match.group(1) != expected_label:
                break
            options.append(match.group(2))
            end += 1
        if len(options) >= 2:
            return options, '\n'.join(lines[start:end])
    return [], ''


def _fill_blanks_from_signs(description, answer):
    """Map each [[blank]] in a prompt to one pipe-separated answer cell value."""
    sign_count = len(BLANK_SIGN_RE.findall(description or ''))
    if not sign_count:
        return description, []
    answers = [part.strip() for part in (answer or '').split('|')]
    if len(answers) != sign_count or any(not value for value in answers):
        raise ValueError('Fill-in-the-blank answers must use one value per [[blank]], separated by |.')
    counter = iter(range(1, sign_count + 1))
    cleaned_description = BLANK_SIGN_RE.sub(
        lambda _match: '**[Blank {}]**'.format(next(counter)), description)
    return cleaned_description, [('Blank {}'.format(index), value) for index, value in enumerate(answers, start=1)]


def _auto_fill_blank_config(existing_blanks, description, answer):
    """Build the editable blank configuration from every description source."""
    if existing_blanks:
        return existing_blanks
    try:
        _description, signed_blanks = _fill_blanks_from_signs(description, answer)
    except ValueError:
        signed_blanks = []
    if signed_blanks:
        return _serialize_fill_blanks([
            {'label': label, 'answers': [accepted]}
            for label, accepted in signed_blanks
        ])
    return answer


def _multiple_choice_answer_indices(answer, options):
    """Resolve Excel answer values without asking the LLM to infer correctness."""
    result = []
    for token in _split_lines(answer):
        value = token.strip().strip('()[]').rstrip('.)').strip()
        if value.isdigit():
            index = int(value)
        elif re.fullmatch(r'[A-Za-z]', value):
            index = ord(value.upper()) - ord('A') + 1
        else:
            matches = [index for index, option in enumerate(options, start=1)
                       if option.strip().casefold() == value.casefold()]
            if len(matches) != 1:
                raise ValueError(
                    'MCQ answer "{}" must be an option letter, number, or exact option text.'.format(token),
                )
            index = matches[0]
        if index < 1 or index > len(options):
            raise ValueError('MCQ answer "{}" does not match an option.'.format(token))
        if index not in result:
            result.append(index)
    if not result:
        raise ValueError('Multiple-choice questions need an answer in the answer column.')
    return result


class ProblemImportUploadView(LoginRequiredMixin, View):
    """Convert a workbook into an editable, server-side preview."""
    def post(self, request):
        _require_creator(request)
        uploaded = request.FILES.get('excel_file')
        if uploaded is None or not (uploaded.name or '').lower().endswith('.xlsx'):
            return JsonResponse({'ok': False, 'message': _('Please choose an .xlsx Excel file.')}, status=400)
        try:
            validate_xlsx_upload(uploaded)
            rows, embedded = read_xlsx_rows_with_embedded_files(uploaded)
        except zipfile.BadZipFile:
            return JsonResponse({'ok': False, 'message': _('The Excel file is invalid.')}, status=400)
        except (ET.ParseError, KeyError, ValueError) as error:
            return JsonResponse({'ok': False, 'message': str(error)}, status=400)

        if not rows:
            return JsonResponse({'ok': False, 'message': _('The Excel file is empty.')}, status=400)
        headers = {_header_name(value): index for index, value in enumerate(rows[0])}
        missing = [name for name in ('name', 'question_type') if name not in headers]
        if missing:
            return JsonResponse({'ok': False, 'message': _('Missing required columns: %(columns)s.') % {
                'columns': ', '.join(missing),
            }}, status=400)

        files_by_row = {}
        for document in embedded:
            files_by_row.setdefault(document.row_number, []).append(document)
        try:
            with transaction.atomic():
                draft = ProblemImportDraft.objects.create(owner=request.user.profile)
                count = 0
                for row_number, row in enumerate(rows[1:], start=2):
                    source_name = _value(row, headers, 'name')
                    unrecognized_configurations = [
                        {'column': str(header).strip(), 'value': str(row[index]).strip()}
                        for index, header in enumerate(rows[0])
                        if _header_name(header) not in IMPORT_COLUMNS
                        and index < len(row) and row[index] is not None and str(row[index]).strip()
                    ]
                    organization_ids, organization_issues = _resolve_import_organizations(
                        request,
                        _value(row, headers, 'organizations'),
                    )
                    unrecognized_configurations.extend(organization_issues)
                    name, name_needs_review = _import_problem_name(source_name, row_number)
                    type_value = _value(row, headers, 'question_type')
                    description = _value(row, headers, 'description')
                    attachments = files_by_row.get(row_number, [])
                    description_files = [document for document in attachments if document.text]
                    zip_files = [document for document in attachments if document.filename.lower().endswith('.zip')]
                    if not any((source_name, type_value, description, attachments)):
                        continue
                    question_type, type_error = _import_question_type(type_value)
                    is_true_false = _is_true_false_type(type_value)
                    review_messages = [message for message in (
                        type_error,
                        _('Replace the generated missing-name placeholder before creating this problem.')
                        if name_needs_review else '',
                    ) if message]
                    if unrecognized_configurations:
                        review_messages.append(
                            _(
                                'Review and confirm the unrecognized imported configuration before creating this '
                                'problem.',
                            ),
                        )
                    parts = [description] if description else []
                    parts.extend(document.text for document in description_files)
                    if not parts:
                        raise ValueError(
                            'Row {} must contain description text or an embedded DOCX/Markdown file.'.format(
                                row_number),
                        )
                    if zip_files and question_type != ProblemSubmissionType.CODE:
                        raise ValueError(
                            'Row {} has an embedded ZIP, but only code problems can use test data ZIPs.'.format(
                                row_number),
                        )
                    if len(zip_files) > 1:
                        raise ValueError('Row {} has more than one embedded test data ZIP.'.format(row_number))
                    test_data_zip = None
                    data_load_error = ''
                    if zip_files:
                        zip_attachment = zip_files[0]
                        # Keep a failed archive reviewable instead of rejecting the whole workbook.
                        try:
                            _parse_zip_test_data(ContentFile(zip_attachment.content, name=zip_attachment.filename))
                        except ValueError as error:
                            data_load_error = str(error)
                        test_data_zip = ContentFile(zip_attachment.content, name=zip_attachment.filename)
                    item_description = '\n\n'.join(parts)
                    answer = _value(row, headers, 'answer')
                    if is_true_false:
                        normalized_answer = answer.casefold()
                        if normalized_answer in ('true', 't', 'yes', '1'):
                            answer = '1'
                        elif normalized_answer in ('false', 'f', 'no', '2'):
                            answer = '2'
                    blanks = _value(row, headers, 'blanks')
                    if question_type == ProblemSubmissionType.FILL_BLANK:
                        blanks = _auto_fill_blank_config(blanks, item_description, answer)
                    options = _value(row, headers, 'options')
                    correct_options = _value(row, headers, 'correct_options')
                    if is_true_false and not options:
                        options = '{}\n{}'.format(_('True'), _('False'))
                    if question_type == ProblemSubmissionType.MCQ and not options:
                        extracted_options, option_block = _extract_labeled_multiple_choice_options(item_description)
                        if extracted_options:
                            cleaned_description = _remove_multiple_choice_option_block(item_description, option_block)
                            if cleaned_description:
                                item_description = cleaned_description
                            options = '\n'.join(extracted_options)
                            if not correct_options:
                                try:
                                    correct_options = '\n'.join(str(index) for index in _multiple_choice_answer_indices(
                                        answer, extracted_options))
                                except ValueError:
                                    # Keep the answer cell editable when it does not yet match the extracted options.
                                    correct_options = ''
                    test_data = _value(row, headers, 'test_data')
                    if question_type == ProblemSubmissionType.CODE and test_data and not data_load_error:
                        try:
                            _parse_text_test_data(test_data)
                        except ValueError as error:
                            data_load_error = str(error)
                    item = ProblemImportItem.objects.create(
                        draft=draft,
                        row_number=row_number,
                        name=name,
                        submission_type=question_type,
                        description=item_description,
                        answer=answer,
                        solution=_value(row, headers, 'solution'),
                        tags=_serialize_import_tags(_value(row, headers, 'tags')),
                        difficulty=_value(row, headers, 'difficulty'),
                        allowed_languages=_value(row, headers, 'allowed_languages'),
                        test_data=test_data,
                        options=options,
                        correct_options=correct_options,
                        # A fill-blank row can use its answer cell when no separate blanks column exists.
                        blanks=blanks or (answer if question_type == ProblemSubmissionType.FILL_BLANK else ''),
                        rubric=_value(row, headers, 'rubric'),
                        grading_table=_value(row, headers, 'grading_table'),
                        is_true_false=is_true_false,
                        data_needs_review=bool(data_load_error),
                        data_load_error=data_load_error,
                        config_needs_review=bool(unrecognized_configurations),
                        unrecognized_configurations=unrecognized_configurations,
                        editor_data={'organizations': organization_ids} if organization_ids else {},
                        test_data_zip=test_data_zip,
                        source_label=', '.join(document.filename for document in attachments),
                        formatting_error='\n'.join(review_messages),
                        type_needs_review=bool(type_error),
                        name_needs_review=name_needs_review,
                    )
                    if _refresh_configuration_review(item):
                        item.formatting_error = _review_message(item)
                        item.save(update_fields=[
                            'config_needs_review', 'unrecognized_configurations', 'formatting_error', 'updated_at',
                        ])
                    _store_draft_images(item, description_files)
                    count += 1
        except ValueError as error:
            return JsonResponse({'ok': False, 'message': str(error)}, status=400)
        if not count:
            draft.delete()
            return JsonResponse(
                {'ok': False, 'message': _('The Excel file does not contain any problem rows.')}, status=400,
            )
        preview_url = reverse('problem_import_preview', args=(draft.pk,))
        if request.headers.get('x-requested-with') == 'XMLHttpRequest':
            return JsonResponse({'ok': True, 'preview_url': preview_url})
        return redirect(preview_url)


class ProblemImportPreviewView(LoginRequiredMixin, View):
    template_name = 'problem/import_preview.html'

    def get(self, request, draft):
        draft = _draft_for(request, draft)
        items = list(draft.items.select_related('created_problem'))
        selected_item = None
        selected_id = request.GET.get('item')
        if selected_id:
            try:
                selected_item = next(item for item in items if item.pk == int(selected_id))
            except (StopIteration, TypeError, ValueError):
                selected_item = None
        if selected_item is None and items:
            selected_item = items[0]
        languages = list(Language.objects.order_by('name', 'key'))
        for item in items:
            if not item.data_needs_review:
                data_load_error = _test_data_load_error(item)
                if data_load_error:
                    item.data_needs_review = True
                    item.data_load_error = data_load_error
                    item.formatting_error = _review_message(item)
                    item.save(update_fields=['data_needs_review', 'data_load_error', 'formatting_error', 'updated_at'])
            if _refresh_configuration_review(item):
                item.formatting_error = _review_message(item)
                item.save(update_fields=[
                    'config_needs_review', 'unrecognized_configurations', 'formatting_error', 'updated_at',
                ])
            item.preview_options = _preview_options(item)
            item.preview_blanks = _preview_blanks(item)
            item.preview_language_keys = _preview_language_keys(item)
            item.preview_type_label, item.preview_type_class = _preview_type_presentation(item)
        return render(request, self.template_name, {
            'draft': draft,
            'items': items,
            'selected_item': selected_item,
            'type_choices': ProblemImportItem.TYPE_CHOICES,
            'languages': languages,
            'image_upload_url': reverse('martor_image_uploader'),
            'file_upload_url': reverse('editor_file_uploader'),
            'title': _('Excel import preview'),
            'content_title': _('Excel import preview'),
            'feedback': request.GET.get('feedback', ''),
            'feedback_error': request.GET.get('feedback_error') == '1',
        })


class ProblemImportItemEditorView(ProblemCreateV2View):
    """Render the unified problem editor against one import-draft item."""
    def get(self, request, draft, item, *args, **kwargs):
        draft = _draft_for(request, draft)
        item = get_object_or_404(draft.items, pk=item)
        context = self.get_context(request)
        context.update({
            'title': _('Excel import preview'),
            'edit_data_json': _safe_json(_editor_data_for_item(item)),
            'save_url': reverse('problem_import_item_action', args=(draft.pk, item.pk, 'editor')),
            'import_format_url': reverse('problem_import_item_action', args=(draft.pk, item.pk, 'format')),
            'import_create_url': reverse('problem_import_item_action', args=(draft.pk, item.pk, 'create')),
            'import_editor_mode': True,
            'import_item_id': item.pk,
            'import_type_needs_review': item.type_needs_review,
            'import_name_needs_review': item.name_needs_review,
            'import_data_needs_review': item.data_needs_review,
            'import_data_load_error': item.data_load_error,
            'import_config_needs_review': item.config_needs_review,
            'import_unrecognized_configurations': item.unrecognized_configurations,
            'edit_code': '',
            'edit_detail_url': '',
        })
        return render(request, self.template_name, context)

    def post(self, request, draft, item, *args, **kwargs):
        # The editor URL is also the draft-save URL.  Do not inherit create_v2.post(),
        # which would create a real problem instead of updating the import draft.
        return ProblemImportItemActionView().post(request, draft, item, 'editor')


class ProblemImportImageView(LoginRequiredMixin, View):
    """Serve an unconfirmed DOCX figure only to the owner of its import draft."""
    def get(self, request, draft, token):
        draft = _draft_for(request, draft)
        image = get_object_or_404(ProblemImportImage, item__draft=draft, token=token)
        content_type, _encoding = mimetypes.guess_type(image.filename)
        return HttpResponse(bytes(image.content), content_type=content_type or 'application/octet-stream')


class ProblemImportItemActionView(LoginRequiredMixin, View):
    """Save, format, or create one preview item without exposing draft ownership."""
    def post(self, request, draft, item, action):
        draft = _draft_for(request, draft)
        item = get_object_or_404(draft.items, pk=item)
        if action not in ('save', 'format', 'create', 'editor'):
            raise Http404()
        if action == 'editor':
            try:
                payload, zip_upload = self._parse_editor_request(request)
                self._save_editor_item(item, payload, zip_upload)
            except ValueError as error:
                return self._error_response(request, draft, item, str(error))
            return JsonResponse({'ok': True, 'message': _('Draft saved.'), 'item': _item_payload(item)})
        try:
            self._save_item(item, request.POST, request.FILES)
        except ValueError as error:
            return self._error_response(request, draft, item, str(error))
        if action == 'save':
            return self._success_response(request, draft, item, _('Draft saved.'), _item_payload(item))
        if action == 'format':
            if not llm.is_configured():
                return self._error_response(request, draft, item, _('LiteLLM is not configured.'), status=503)
            try:
                payload = self._format(item)
            except (LLMError, ValueError) as error:
                return self._error_response(request, draft, item, str(error))
            return self._success_response(request, draft, item, _('Question formatted.'), payload)
        try:
            _validate_problem_import_item(item)
        except ValueError as error:
            return self._error_response(request, draft, item, str(error))
        created_data_directories = []
        completed = False
        try:
            with transaction.atomic():
                problem = _create_problem(request, item, created_data_directories=created_data_directories)
                item.created_problem = problem
                item.save(update_fields=['created_problem', 'updated_at'])
            completed = True
        except ValueError as error:
            return self._error_response(request, draft, item, str(error))
        except OSError as error:
            if error.errno not in (errno.EACCES, errno.ENOENT, errno.EROFS):
                raise
            return self._error_response(
                request, draft, item,
                _('Cannot write code-problem test data. An administrator must create and grant write access to '
                  '%(path)s for the web service.') % {'path': '/mnt/problems'},
            )
        finally:
            if not completed:
                _delete_problem_data_directories(created_data_directories)
        return self._success_response(
            request, draft, item, _('Created %(name)s.') % {'name': problem.name},
            edit_url=_edit_url(problem),
        )

    @staticmethod
    def _wants_json(request):
        """Keep JSON for API calls while allowing normal browser form submissions."""
        return request.headers.get('x-requested-with') == 'XMLHttpRequest' or request.POST.get('native_submit') != '1'

    def _preview_redirect(self, draft, item, message, error=False):
        query = urlencode({'feedback': message, 'feedback_error': '1' if error else '0'})
        return '{}?{}#import-item-{}'.format(
            reverse('problem_import_preview', args=(draft.pk,)), query, item.pk)

    def _success_response(self, request, draft, item, message, payload=None, edit_url=None):
        if not self._wants_json(request):
            return redirect(self._preview_redirect(draft, item, message))
        response = {'ok': True, 'message': message}
        if payload is not None:
            response['item'] = _item_payload(item)
        if edit_url:
            response['edit_url'] = edit_url
        return JsonResponse(response)

    def _error_response(self, request, draft, item, message, status=400):
        item.formatting_error = message
        item.save(update_fields=['formatting_error', 'updated_at'])
        if not self._wants_json(request):
            return redirect(self._preview_redirect(draft, item, message, error=True))
        return JsonResponse({'ok': False, 'message': message}, status=status)

    def _save_item(self, item, data, files):
        previous_type = item.submission_type
        previous_name = item.name
        try:
            item.submission_type = _normalise_type(data.get('question_type', item.submission_type))
        except ValueError as error:
            raise ValueError(str(error))
        for field in (
            'name', 'description', 'answer', 'solution', 'tags', 'difficulty', 'allowed_languages', 'test_data',
            'options',
            'correct_options', 'blanks', 'rubric', 'grading_table',
        ):
            if field in data:
                setattr(item, field, (data.get(field, '') or '').strip())
        item.tags = _serialize_import_tags(item.tags)
        if files.get('test_data_zip'):
            raise ValueError('Test data ZIP files must be embedded as an Excel object.')
        if (
            item.type_needs_review and item.submission_type == previous_type
            and data.get('confirm_question_type') != '1'
        ):
            # Background draft saves must not silently accept an unknown imported type.
            pass
        else:
            item.type_needs_review = False
        if item.name_needs_review and item.name != previous_name:
            item.name_needs_review = False
        _refresh_configuration_review(
            item,
            confirm=data.get('confirm_unrecognized_configurations') == '1',
        )
        item.formatting_error = _review_message(item)
        item.save()

    @staticmethod
    def _parse_editor_request(request):
        content_type = (request.content_type or '').split(';')[0].strip().lower()
        if content_type == 'multipart/form-data':
            raw = request.POST.get('payload')
            zip_upload = request.FILES.get('testcase_zip')
        else:
            raw = request.body.decode('utf-8')
            zip_upload = None
        try:
            payload = json.loads(raw or '')
        except (TypeError, ValueError, UnicodeDecodeError) as error:
            raise ValueError(_('Invalid editor draft data.')) from error
        if not isinstance(payload, dict):
            raise ValueError(_('Invalid editor draft data.'))
        return payload, zip_upload

    def _save_editor_item(self, item, payload, zip_upload):
        editor_type = payload.get('type')
        type_map = {
            'programming': ProblemSubmissionType.CODE,
            'single': ProblemSubmissionType.MCQ,
            'multiple': ProblemSubmissionType.MCQ,
            'truefalse': ProblemSubmissionType.MCQ,
            'blank': ProblemSubmissionType.FILL_BLANK,
            'short': ProblemSubmissionType.SHORT_ANSWER,
        }
        if editor_type not in type_map:
            raise ValueError(_('Select a supported question type.'))
        raw_name = payload.get('name') or ''
        raw_description = payload.get('description') or ''
        solution = payload.get('solution') or {}
        if not isinstance(raw_name, str) or not isinstance(raw_description, str) or not isinstance(solution, dict):
            raise ValueError(_('Invalid editor draft data.'))
        name = raw_name.strip()
        description = raw_description.strip()
        if not name or not description:
            raise ValueError(_('A name and description are required.'))

        item.submission_type = type_map[editor_type]
        item.is_true_false = editor_type == 'truefalse'
        item.name = name
        item.description = description
        item.solution = str(solution.get('content') or '').strip()
        problem_types = payload.get('problemTypes') or []
        languages = payload.get('languages') or []
        if not isinstance(problem_types, list) or not isinstance(languages, list):
            raise ValueError(_('Invalid editor draft data.'))
        item.tags = _serialize_import_tags(problem_types)
        item.difficulty = str(payload.get('difficulty') or '').strip()
        item.allowed_languages = '|'.join(
            str(key).strip() for key in languages if str(key).strip()
        )

        if editor_type in ('single', 'multiple'):
            options = payload.get('options') or []
            if not isinstance(options, list):
                raise ValueError(_('Choices must be a list.'))
            texts = [str(option.get('text') or '').strip() for option in options if isinstance(option, dict)]
            item.options = '\n'.join(text for text in texts if text)
            correct = [str(index) for index, option in enumerate(options, start=1)
                       if isinstance(option, dict) and option.get('correct') and str(option.get('text') or '').strip()]
            item.answer = '|'.join(correct)
            item.correct_options = '\n'.join(correct)
        elif editor_type == 'truefalse':
            item.options = 'True\nFalse'
            item.answer = '1' if payload.get('tfAnswer', 'true') == 'true' else '2'
        elif editor_type == 'blank':
            blanks = payload.get('blanks') or []
            if not isinstance(blanks, list):
                raise ValueError(_('Blank answers must be a list.'))
            blank_blocks = []
            for answer in blanks:
                if isinstance(answer, dict):
                    text = answer.get('answers') or answer.get('text') or ''
                else:
                    text = answer
                if isinstance(text, (list, tuple)):
                    text = '\n'.join(str(part) for part in text if part is not None and str(part).strip())
                text = str(text or '').strip()
                if text:
                    blank_blocks.append({'label': '', 'answers': _split_lines(text.replace('|', '\n'))})
            item.blanks = _serialize_fill_blanks(blank_blocks)
            item.rubric = (payload.get('rubric') or '').strip()
            item.grading_table = json.dumps(payload.get('gradingTable')) if payload.get('gradingTable') else ''
        elif editor_type == 'short':
            item.answer = (payload.get('shortAnswer') or '').strip()
            item.rubric = (payload.get('rubric') or '').strip()
            item.grading_table = json.dumps(payload.get('gradingTable')) if payload.get('gradingTable') else ''
        elif editor_type == 'programming':
            testcases = payload.get('testcases') or []
            if not isinstance(testcases, list):
                raise ValueError(_('Test cases must be a list.'))
            existing_data_error = item.data_load_error or _test_data_load_error(item)
            if existing_data_error and not testcases and zip_upload is None and payload.get('confirm_data_load') != '1':
                # The editor cannot render unreadable data. Keep it until the teacher explicitly confirms removal.
                item.data_load_error = existing_data_error
                item.data_needs_review = True
            else:
                item.test_data = _serialize_editor_testcases(testcases)
                item.data_load_error = ''
                item.data_needs_review = False
            if zip_upload is not None:
                _parse_zip_test_data(zip_upload)
                zip_upload.seek(0)
                if item.test_data_zip:
                    # Replaced: do not leave the previous hidden test data behind.
                    item.test_data_zip.delete(save=False)
                item.test_data_zip.save(zip_upload.name or 'test_data.zip', zip_upload, save=False)

            if not item.data_needs_review:
                item.data_load_error = _test_data_load_error(item)
            if item.data_load_error and payload.get('confirm_data_load') == '1':
                # Confirmation explicitly means create this code problem without the unreadable test data.
                if item.test_data_zip:
                    item.test_data_zip.delete(save=False)
                item.test_data = ''
                item.data_load_error = ''
            item.data_needs_review = bool(item.data_load_error)
        else:
            item.data_needs_review = False
            item.data_load_error = ''

        previous_type_needs_review = item.type_needs_review
        if previous_type_needs_review and payload.get('confirm_question_type') != '1':
            pass
        else:
            item.type_needs_review = False
        if item.name_needs_review and name:
            item.name_needs_review = False
        _refresh_configuration_review(
            item,
            confirm=payload.get('confirm_unrecognized_configurations') == '1',
        )
        item.editor_data = payload
        item.formatting_error = _review_message(item)
        item.save()

    def _format(self, item):
        payload = _format_with_llm(item)
        if item.submission_type == ProblemSubmissionType.MCQ:
            options = payload.get('options', [])
            option_texts = [option['text'].strip() for option in options if option.get('text', '').strip()]
            item.options = '\n'.join(option_texts)
            try:
                answer_indices = _multiple_choice_answer_indices(item.answer, option_texts)
            except ValueError:
                # The Excel answer remains authoritative. Let the preview flag an unmatched
                # answer for revision instead of discarding a useful LLM option extraction.
                item.correct_options = ''
            else:
                item.correct_options = '\n'.join(str(index) for index in answer_indices)
            item.description = _remove_multiple_choice_option_block(
                item.description, payload.get('option_block', ''))
        elif item.submission_type == ProblemSubmissionType.FILL_BLANK:
            item.description = payload.get('description', item.description).strip()
            item.blanks = _serialize_fill_blanks(payload.get('blanks', []))
        else:
            item.description = payload.get('description', item.description).strip()
        item.formatting_error = ''
        _refresh_configuration_review(item)
        item.formatting_error = _review_message(item)
        # Formatting changes the legacy import fields, so rebuild the unified-editor payload on reload.
        item.editor_data = {}
        item.save()
        return _item_payload(item)


class ProblemImportCreateAllView(LoginRequiredMixin, View):
    """Create every remaining reviewed item as one all-or-nothing database operation."""
    def post(self, request, draft):
        draft = _draft_for(request, draft)
        promoted_images = []
        created_data_directories = []
        completed = False
        try:
            with transaction.atomic():
                items = list(draft.items.select_for_update().filter(created_problem__isnull=True))
                if not items:
                    return self._success_response(request, _('All problems have already been created.'), 0)
                errors = self._validation_errors(items)
                if errors:
                    return self._error_response(
                        request, draft,
                        _('Fix %(count)d problem(s) before creating all.') % {'count': len(errors)},
                        status=400, errors=errors,
                    )
                for item in items:
                    problem = _create_problem(request, item, promoted_images, created_data_directories)
                    item.created_problem = problem
                    item.save(update_fields=['created_problem', 'updated_at'])
            completed = True
        except ValueError as error:
            return self._error_response(request, draft, str(error), status=400)
        except OSError as error:
            if error.errno not in (errno.EACCES, errno.ENOENT, errno.EROFS):
                raise
            return self._error_response(
                request, draft,
                _(
                    'Cannot write code-problem test data. An administrator must create and grant write access to '
                    '%(path)s for the web service.',
                ) % {'path': '/mnt/problems'},
                status=500,
            )
        finally:
            if not completed:
                _delete_stored_images(promoted_images)
                _delete_problem_data_directories(created_data_directories)
        return self._success_response(request, _('Created %(count)d problems.') % {'count': len(items)}, len(items))

    @staticmethod
    def _wants_json(request):
        return request.headers.get('x-requested-with') == 'XMLHttpRequest'

    def _success_response(self, request, message, count):
        if not self._wants_json(request):
            return redirect('problem_list')
        return JsonResponse({
            'ok': True,
            'message': message,
            'count': count,
            'redirect_url': reverse('problem_list'),
        })

    @staticmethod
    def _validation_errors(items):
        errors = []
        for item in items:
            try:
                _validate_problem_import_item(item)
            except ValueError as error:
                message = str(error)
                if item.formatting_error != message:
                    item.formatting_error = message
                    item.save(update_fields=['formatting_error', 'updated_at'])
                errors.append({
                    'item_id': item.pk,
                    'row_number': item.row_number,
                    'name': item.name,
                    'message': message,
                })
        return errors

    def _error_response(self, request, draft, message, status, errors=None):
        if not self._wants_json(request):
            query = urlencode({'feedback': message, 'feedback_error': '1'})
            return redirect('{}?{}'.format(reverse('problem_import_preview', args=(draft.pk,)), query))
        payload = {'ok': False, 'message': message}
        if errors:
            payload['errors'] = errors
        return JsonResponse(payload, status=status)


def _format_with_llm(item):
    type_name = dict(ProblemImportItem.TYPE_CHOICES)[item.submission_type]
    system = (
        'You format educational questions. Return JSON only. Keep all facts in the source; do not invent '
        'answers. For multiple choice return {"options": [{"text": string}], "option_block": string}. '
        'Do not infer, return, or modify correct answers: the Excel answer column is authoritative. '
        'option_block must be an exact, contiguous substring copied from Description and contain only the '
        'displayed options. Do not rewrite, summarize, correct, or translate Description: the system removes '
        'only option_block and preserves all other text exactly. For fill-in-the-blank return '
        '{"description": string, "blanks": [{"label": string, "answers": [string]}]}; label is the short '
        'text shown beside that input. For code return {"description": string}. If an answer cannot be '
        'determined, return an empty correct/answers collection.'
    )
    user = 'Question type: {}\n\nDescription:\n{}'.format(type_name, item.description)
    if item.submission_type != ProblemSubmissionType.MCQ:
        user += '\n\nAnswer key, if supplied:\n{}'.format(item.answer)
    result = llm.chat([{'role': 'system', 'content': system}, {'role': 'user', 'content': user}],
                      profile='question_import')
    text = result.text.strip()
    if text.startswith('```'):
        text = text.split('\n', 1)[1].rsplit('```', 1)[0].strip()
    try:
        payload = json.loads(text)
    except (TypeError, ValueError) as error:
        raise ValueError('The LLM did not return valid JSON.') from error
    if not isinstance(payload, dict):
        raise ValueError('The LLM returned an invalid question format.')
    if item.submission_type == ProblemSubmissionType.MCQ:
        options = payload.get('options', [])
        if not isinstance(options, list) or any(
            not isinstance(option, dict) or not isinstance(option.get('text'), str)
            for option in options
        ):
            raise ValueError('The LLM returned invalid multiple-choice options.')
        if not isinstance(payload.get('option_block', ''), str):
            raise ValueError('The LLM returned an invalid multiple-choice option block.')
    elif not isinstance(payload.get('description', ''), str):
        raise ValueError('The LLM returned an invalid question format.')
    if item.submission_type == ProblemSubmissionType.FILL_BLANK:
        blanks = payload.get('blanks', [])
        if not isinstance(blanks, list) or any(not isinstance(blank, dict) for blank in blanks):
            raise ValueError('The LLM returned invalid blanks.')
    return payload


def _item_payload(item):
    payload = {
        field: getattr(item, field)
        for field in ('name', 'description', 'tags', 'difficulty', 'options', 'correct_options', 'blanks',
                      'rubric', 'grading_table', 'formatting_error')
    }
    payload['name_needs_review'] = item.name_needs_review
    payload['type_needs_review'] = item.type_needs_review
    payload['data_needs_review'] = item.data_needs_review
    payload['data_load_error'] = item.data_load_error
    payload['config_needs_review'] = item.config_needs_review
    payload['unrecognized_configurations'] = item.unrecognized_configurations
    payload['preview_options'] = _preview_options(item)
    payload['preview_blanks'] = _preview_blanks(item)
    payload['preview_language_keys'] = _preview_language_keys(item)
    payload['preview_type_label'], payload['preview_type_class'] = _preview_type_presentation(item)
    return payload


def _preview_type_presentation(item):
    if item.submission_type == ProblemSubmissionType.MCQ and item.is_true_false:
        return str(_('True/False')), 'regular'
    label = dict(ProblemImportItem.TYPE_CHOICES).get(item.submission_type, _('Question'))
    style = 'programming' if item.submission_type == ProblemSubmissionType.CODE else 'regular'
    return str(label), style


def _preview_options(item):
    options = _split_choice_lines(item.options)
    try:
        correct = set(_multiple_choice_answer_indices(item.answer, options)) if options else set()
    except ValueError:
        correct = set()
    return [{'text': text, 'is_correct': index in correct} for index, text in enumerate(options, start=1)]


def _preview_blanks(item):
    try:
        blanks = _parse_fill_blanks(item.blanks)
    except ValueError:
        blanks = []
    return [
        {'label': label, 'answers': answers}
        for label, answers in blanks
    ]


def _preview_language_keys(item):
    """Keep an invalid Excel language value editable instead of failing the preview page."""
    if not item.allowed_languages.strip():
        return []
    try:
        return [language.key for language in _resolve_allowed_languages(item.allowed_languages)]
    except ValueError:
        return []


def _split_lines(value):
    return [part.strip() for part in re.split(r'[\n,;|]+', value or '') if part.strip()]


def _split_choice_lines(value):
    """Treat each non-empty line as one multiple-choice option."""
    return [line.strip() for line in str(value or '').splitlines() if line.strip()]


def _resolve_import_organizations(request, raw_value):
    """Resolve one Excel organization using the unified editor's assignment scope."""
    tokens = _split_lines(raw_value)
    if not tokens:
        return [], []
    if len(tokens) > 1:
        return [], [{
            'column': 'Organizations',
            'value': raw_value,
            'reason': _('The preview editor currently supports one organization per problem.'),
        }]

    # Match the unified problem editor: creators may assign the courses they teach,
    # including a course that does not yet have any classes or members.
    queryset = assignable_organizations(request.user)
    token = tokens[0]
    matches = list(queryset.filter(slug__iexact=token))
    matches.extend(queryset.filter(short_name__iexact=token))
    matches.extend(queryset.filter(name__iexact=token))
    if token.isdigit():
        matches.extend(queryset.filter(pk=int(token)))
    unique_matches = {organization.pk: organization for organization in matches}
    if len(unique_matches) == 1:
        return [next(iter(unique_matches))], []

    reason = (
        _('matches multiple organizations available to you.') if unique_matches else
        _('was not found among organizations available to you.')
    )
    return [], [{
        'column': 'Organizations',
        'value': raw_value,
        'reason': _('Organization "%(organization)s" %(reason)s') % {
            'organization': token,
            'reason': reason,
        },
    }]


def _resolve_allowed_languages(raw_value):
    requested = _split_lines(raw_value)
    queryset = list(Language.objects.exclude(key='ANSWER'))
    if not requested:
        return queryset
    resolved = []
    seen = set()
    for token in requested:
        matches = _matching_import_languages(queryset, token)
        if not matches:
            raise ValueError('Language "{}" was not found.'.format(token))
        if len(matches) > 1:
            raise ValueError('Language "{}" matched multiple languages.'.format(token))
        match = matches[0]
        if match.pk not in seen:
            resolved.append(match)
            seen.add(match.pk)
    return resolved


def _matching_import_languages(languages, token):
    """Match spreadsheet languages by full name or key, ignoring only case."""
    normalized_token = str(token).casefold()
    return [
        language for language in languages
        if language.key.casefold() == normalized_token or language.name.casefold() == normalized_token
    ]


def _normalise_import_tags(raw_value):
    values = _split_lines(raw_value) if isinstance(raw_value, str) else [
        part
        for value in raw_value or []
        for part in _split_lines(str(value))
    ]
    tags = []
    seen = set()
    for value in values:
        key = value.casefold()
        if key not in seen:
            tags.append(value)
            seen.add(key)
    return tags


def _serialize_import_tags(raw_value):
    return ', '.join(_normalise_import_tags(raw_value))


def _validate_import_tags(raw_value):
    tags = _normalise_import_tags(raw_value)
    if len(tags) > 5:
        raise ValueError('Use at most five tags per problem.')
    for tag in tags:
        if len(tag) > 20:
            raise ValueError('Each tag must be at most 20 characters.')
        identifier = re.sub(r'[^a-z0-9]+', '_', tag.casefold()).strip('_')
        if not identifier or len(identifier) > 20:
            raise ValueError('Each tag must contain letters or numbers and be at most 20 characters.')
    return tags


def _resolve_import_tags(raw_value):
    tags = _validate_import_tags(raw_value)
    resolved = []
    seen = set()
    for tag in tags:
        existing = ProblemType.objects.filter(name__iexact=tag).first()
        if existing is None:
            existing = ProblemType.objects.filter(full_name__iexact=tag).first()
        if existing is None:
            identifier = re.sub(r'[^a-z0-9]+', '_', tag.casefold()).strip('_')
            existing, _created = ProblemType.objects.get_or_create(
                name=identifier,
                defaults={'full_name': tag},
            )
        if existing.pk not in seen:
            resolved.append(existing)
            seen.add(existing.pk)
    return resolved


def _validate_import_difficulty(raw_value):
    value = str(raw_value or '').strip().casefold()
    if not value:
        return None
    name = DIFFICULTY_VALUES.get(value, value)
    if name not in Problem.DIFFICULTY_TYPE_NAMES:
        raise ValueError('Difficulty must be 0 (easy), 1 (medium), or 2 (hard).')
    return name


def _resolve_import_difficulty(raw_value):
    name = _validate_import_difficulty(raw_value)
    if name is None:
        return None
    difficulty, _created = ProblemType.objects.get_or_create(
        name=name,
        defaults={'full_name': name.title()},
    )
    return difficulty


def _configuration_review_items(item):
    """Describe recognized columns whose values still cannot be safely imported."""
    issues = []

    def add(column, value, error):
        issues.append({
            'kind': 'validation',
            'column': str(column),
            'value': str(value or ''),
            'reason': str(error),
        })

    try:
        _validate_import_tags(item.tags)
    except ValueError as error:
        add(_('Tags'), item.tags, error)
    try:
        _validate_import_difficulty(item.difficulty)
    except ValueError as error:
        add(_('Difficulty'), item.difficulty, error)

    if item.submission_type == ProblemSubmissionType.CODE:
        try:
            _resolve_allowed_languages(item.allowed_languages)
        except ValueError as error:
            add(_('Allowed languages'), item.allowed_languages, error)
    elif item.submission_type == ProblemSubmissionType.MCQ:
        options = _split_choice_lines(item.options)
        if len(options) < 2:
            add(_('Choices'), item.options, _('At least two choices are required.'))
        else:
            try:
                _multiple_choice_answer_indices(item.answer, options)
            except ValueError as error:
                add(_('Correct answer'), item.answer, error)
    elif item.submission_type == ProblemSubmissionType.FILL_BLANK:
        try:
            blanks = _parse_fill_blanks(item.blanks or item.answer)
            if not blanks:
                raise ValueError(_('At least one blank answer is required.'))
        except ValueError as error:
            add(_('Blanks'), item.blanks or item.answer, error)
    elif item.submission_type == ProblemSubmissionType.SHORT_ANSWER:
        if not item.answer.strip():
            add(_('Reference answer'), '', _('A reference answer is required.'))
        if item.grading_table.strip():
            try:
                table = json.loads(item.grading_table)
                if not isinstance(table, list):
                    raise ValueError(_('The grading table must be a JSON list.'))
            except (TypeError, ValueError) as error:
                add(_('Grading table'), item.grading_table, error)
    return issues


def _refresh_configuration_review(item, *, confirm=False):
    """Require valid recognized values before confirming and dropping unknown columns."""
    preserved = [
        entry for entry in item.unrecognized_configurations or []
        if isinstance(entry, dict) and entry.get('kind') != 'validation'
    ]
    validation_issues = _configuration_review_items(item)
    if confirm and not validation_issues:
        # A teacher may discard Excel-only values only after every mappable value is valid.
        preserved = []
    configurations = preserved + validation_issues
    item.unrecognized_configurations = configurations
    item.config_review_signature = ''
    item.config_needs_review = bool(configurations)
    return validation_issues


def _parse_text_test_data(value):
    """Parse input/output blocks while allowing multi-line input and output values."""
    cases = {}
    current = None
    pattern = re.compile(r'^(input|output)\s+data\s+(\d+)\s*:\s?(.*)$', re.IGNORECASE)
    for line in (value or '').replace('\r\n', '\n').replace('\r', '\n').split('\n'):
        match = pattern.match(line)
        if match:
            kind, number, first_line = match.groups()
            key = int(number)
            if key < 1 or (key, kind.lower()) in cases:
                raise ValueError('Each input data/output data number must appear only once.')
            cases[key, kind.lower()] = [first_line] if first_line else []
            current = cases[key, kind.lower()]
        elif current is not None:
            current.append(line)
        elif line.strip():
            raise ValueError('Test data must start with "input data 1:" or "output data 1:".')
    numbers = sorted({number for number, _kind in cases})
    result = []
    for number in numbers:
        input_lines = cases.get((number, 'input'))
        output_lines = cases.get((number, 'output'))
        if input_lines is None or output_lines is None:
            raise ValueError('Test data {} needs both input and output.'.format(number))
        if input_lines and not input_lines[0].strip():
            input_lines = input_lines[1:]
        if output_lines and not output_lines[0].strip():
            output_lines = output_lines[1:]
        result.append(('case{:03d}.in'.format(number), '\n'.join(input_lines),
                       'case{:03d}.out'.format(number), '\n'.join(output_lines)))
    return result


def _parse_zip_test_data(uploaded):
    payload = uploaded.read()
    if len(payload) > 16 * 1024 * 1024:
        raise ValueError('The test data ZIP is too large.')
    try:
        with zipfile.ZipFile(io.BytesIO(payload)) as archive:
            entries = [info for info in archive.infolist() if not info.is_dir()]
            if sum(info.file_size for info in entries) > 64 * 1024 * 1024:
                raise ValueError('The test data ZIP is too large.')
            files = {}
            for info in entries:
                name = info.filename.replace('\\', '/')
                if name.startswith('/') or '/../' in '/{}/'.format(name):
                    raise ValueError('The test data ZIP contains an unsafe path.')
                root, extension = os.path.splitext(name)
                if extension.lower() in ('.in', '.out'):
                    files.setdefault(root, {})[extension.lower()] = (name, archive.read(info))
    except zipfile.BadZipFile as error:
        raise ValueError('The test data ZIP is invalid.') from error
    cases = []
    for root in sorted(files):
        pair = files[root]
        if '.in' not in pair or '.out' not in pair:
            raise ValueError('ZIP test data files must have matching .in and .out names.')
        cases.append((pair['.in'][0], pair['.in'][1], pair['.out'][0], pair['.out'][1]))
    if not cases:
        raise ValueError('The test data ZIP does not contain any .in/.out test pairs.')
    return payload, cases


def _read_test_data_zip(item):
    """Read and validate a draft ZIP without leaving its storage file open."""
    with item.test_data_zip.open('rb') as uploaded:
        return _parse_zip_test_data(uploaded)


def _test_data_load_error(item):
    """Return a user-facing error when draft test data cannot be shown in the editor."""
    if item.submission_type != ProblemSubmissionType.CODE:
        return ''
    try:
        if item.test_data_zip:
            _read_test_data_zip(item)
        elif item.test_data.strip():
            _parse_text_test_data(item.test_data)
    except ValueError as error:
        return str(error)
    return ''


def _validate_problem_import_item(item):
    """Check every condition that can be evaluated before problem creation writes data."""
    if item.name_needs_review:
        raise ValueError('Replace the generated missing-name placeholder before creating this problem.')
    if item.type_needs_review:
        raise ValueError('Select or confirm the question type before creating this problem.')
    if item.data_needs_review:
        raise ValueError('Review and confirm the test-data loading warning before creating this problem.')
    if item.config_needs_review:
        raise ValueError('Review and confirm the unrecognized imported configuration before creating this problem.')
    if not item.name or not item.description:
        raise ValueError('A name and description are required.')
    _validate_import_tags(item.tags)
    _validate_import_difficulty(item.difficulty)
    if item.submission_type == ProblemSubmissionType.CODE:
        languages = _resolve_allowed_languages(item.allowed_languages)
        if not languages:
            raise ValueError('At least one programming language is required for a code problem.')
        if item.test_data_zip:
            _read_test_data_zip(item)
        elif item.test_data.strip():
            _parse_text_test_data(item.test_data)
    elif item.submission_type == ProblemSubmissionType.MCQ:
        options = _split_choice_lines(item.options)
        correct = set(_multiple_choice_answer_indices(item.answer, options))
        if len(options) < 2:
            raise ValueError('Multiple-choice questions need at least two options.')
        if not correct or any(index < 1 or index > len(options) for index in correct):
            raise ValueError('Mark at least one valid correct option.')
    elif item.submission_type == ProblemSubmissionType.FILL_BLANK:
        signed_blanks = []
        if not item.blanks.strip():
            _description, signed_blanks = _fill_blanks_from_signs(item.description, item.answer)
        blanks = _parse_fill_blanks(item.blanks or item.answer) or signed_blanks
        if not blanks:
            raise ValueError('Fill-in-the-blank questions need at least one blank with an accepted answer.')
    elif item.submission_type == ProblemSubmissionType.SHORT_ANSWER:
        if not item.answer.strip():
            raise ValueError('Short-answer questions need a reference answer.')
        if item.grading_table.strip():
            try:
                table = json.loads(item.grading_table)
            except (TypeError, ValueError) as error:
                raise ValueError('Short-answer grading table must be valid JSON.') from error
            if not isinstance(table, list):
                raise ValueError('Short-answer grading table must be a JSON list.')


def _add_test_cases(problem, item, created_data_directories=None):
    if item.test_data_zip:
        payload, cases = _read_test_data_zip(item)
        _track_problem_data_directory(problem.code, created_data_directories)
        data = ProblemData.objects.create(problem=problem)
        data.zipfile.save('test_data.zip', ContentFile(payload), save=True)
    elif item.test_data.strip():
        cases = _parse_text_test_data(item.test_data)
        _track_problem_data_directory(problem.code, created_data_directories)
        data = ProblemData.objects.create(problem=problem)
        for input_name, input_data, output_name, output_data in cases:
            problem_data_storage.save('{}/{}'.format(problem.code, input_name), ContentFile(input_data))
            problem_data_storage.save('{}/{}'.format(problem.code, output_name), ContentFile(output_data))
    else:
        return
    created_cases = []
    for order, (input_name, _input_data, output_name, _output_data) in enumerate(cases, start=1):
        created_cases.append(ProblemTestCase.objects.create(
            dataset=problem, order=order, input_file=input_name, output_file=output_name,
            points=1, is_pretest=False,
        ))
    valid_files = [filename for case in cases for filename in (case[0], case[2])]
    ProblemDataCompiler.generate(problem, data, created_cases, valid_files)


def _track_problem_data_directory(problem_code, directories):
    if directories is not None and problem_code not in directories:
        directories.append(problem_code)


def _delete_problem_data_directories(problem_codes):
    """Remove test data written by an import transaction that did not commit."""
    for problem_code in set(problem_codes):
        directory = problem_data_storage.path(problem_code)
        if os.path.isdir(directory):
            shutil.rmtree(directory)


def _discard_test_data_zip_on_commit(item):
    """Delete the draft's test-data ZIP once the created problem is committed.

    The problem keeps its own copy in problem data storage; the draft copy is hidden test data
    and must not linger in storage.
    """
    if not item.test_data_zip:
        return
    name = item.test_data_zip.name
    storage = item.test_data_zip.storage
    item_id = item.pk

    def discard():
        ProblemImportItem.objects.filter(pk=item_id, test_data_zip=name).update(test_data_zip='')
        try:
            storage.delete(name)
        except Exception:
            logger.exception('Could not delete import test data %s', name)

    transaction.on_commit(discard)


def _create_problem(request, item, promoted_images=None, created_data_directories=None):
    if item.created_problem_id:
        return item.created_problem
    if not item.name or not item.description:
        raise ValueError('A name and description are required.')
    stored_images = []
    item_data_directories = []
    try:
        description, stored_images = _promote_draft_images(item, item.description)
        problem = _create_problem_records(request, item, description, item_data_directories)
        log_problem_admin_entry(request, problem, created=True)
        # The BinaryField data is no longer needed once every referenced image is public.
        item.images.all().delete()
        _discard_test_data_zip_on_commit(item)
        if promoted_images is not None:
            promoted_images.extend(stored_images)
        if created_data_directories is not None:
            created_data_directories.extend(item_data_directories)
        return problem
    except Exception:
        _delete_stored_images(stored_images)
        _delete_problem_data_directories(item_data_directories)
        raise


def _create_problem_records(request, item, description, created_data_directories=None):
    signed_blanks = []
    if item.submission_type == ProblemSubmissionType.FILL_BLANK:
        if item.blanks.strip():
            counter = iter(range(1, len(BLANK_SIGN_RE.findall(description)) + 1))
            description = BLANK_SIGN_RE.sub(
                lambda _match: '**[Blank {}]**'.format(next(counter)), description)
        else:
            description, signed_blanks = _fill_blanks_from_signs(description, item.answer)
    problem = Problem(name=item.name, description=description, submission_type=item.submission_type, points=10)
    apply_simple_problem_defaults(problem)
    _save_problem_with_unique_code(problem)
    problem.types.add(default_problem_type())
    problem.types.add(*_resolve_import_tags(item.tags))
    difficulty = _resolve_import_difficulty(item.difficulty)
    if difficulty is not None:
        problem.types.add(difficulty)
    problem.authors.add(request.user.profile)
    if item.solution.strip():
        solution = Solution.objects.create(
            problem=problem,
            content=item.solution,
            is_public=False,
            publish_on=timezone.now(),
        )
        solution.authors.add(request.user.profile)
    if item.submission_type == ProblemSubmissionType.CODE:
        languages = _resolve_allowed_languages(item.allowed_languages)
        if not languages:
            raise ValueError('At least one programming language is required for a code problem.')
        problem.allowed_languages.set(languages)
        _add_test_cases(problem, item, created_data_directories)
    elif item.submission_type == ProblemSubmissionType.MCQ:
        options = _split_choice_lines(item.options)
        correct = set(_multiple_choice_answer_indices(item.answer, options))
        if len(options) < 2:
            raise ValueError('Multiple-choice questions need at least two options.')
        if not correct or any(index < 1 or index > len(options) for index in correct):
            raise ValueError('Mark at least one valid correct option.')
        if len(correct) > 1:
            multiple_select = True
        else:
            multiple_select = False
        ProblemMCQConfig.objects.create(
            problem=problem, multiple_select=multiple_select, is_true_false=item.is_true_false,
        )
        for index, text in enumerate(options, start=1):
            ProblemMCQOption.objects.create(problem=problem, order=index - 1, text=text, is_correct=index in correct)
    elif item.submission_type == ProblemSubmissionType.FILL_BLANK:
        blanks = _parse_fill_blanks(item.blanks or item.answer) or signed_blanks
        if not blanks:
            raise ValueError('Fill-in-the-blank questions need at least one blank with an accepted answer.')
        ProblemFillBlankConfig.objects.create(problem=problem)
        for index, (label, answers) in enumerate(blanks):
            ProblemBlank.objects.create(problem=problem, order=index, label=label, answers=answers)
    elif item.submission_type == ProblemSubmissionType.SHORT_ANSWER:
        grading_table = json.loads(item.grading_table) if item.grading_table.strip() else None
        ProblemShortAnswerConfig.objects.create(
            problem=problem,
            reference_answer=item.answer,
            ai_rubric=item.rubric,
            grading_table=grading_table,
        )
    _apply_editor_configuration(request, item, problem)
    return problem


def _apply_editor_configuration(request, item, problem):
    """Apply the unified editor's common and advanced fields after import-specific records exist."""
    try:
        _apply_editor_configuration_fields(request, item, problem)
    except PermissionError as error:
        # The import views report ValueError to the teacher and roll the creation back.
        raise ValueError(str(error)) from error


def _apply_editor_configuration_fields(request, item, problem):
    if not item.editor_data:
        return
    saved_payload = item.editor_data
    if 'type' not in saved_payload:
        # Excel parsing can prefill only organizations. Apply those explicit
        # fields without treating the partial payload as a complete editor save.
        if 'organizations' in saved_payload:
            ProblemCreateV2View()._attach_orgs(problem, saved_payload, request.user)
            problem.is_organization_private = problem.organizations.exists()
            problem.save(update_fields=['is_organization_private'])
        return

    payload = _editor_data_for_item(item)
    # The legacy importer has already promoted private DOCX-image URLs and normalized blank markers.
    payload['description'] = problem.description
    editor = ProblemCreateV2View()
    problem.name = (payload.get('name') or problem.name).strip()
    problem.description = payload.get('description') or problem.description
    problem.points = editor._num(payload.get('points'), problem.points)
    problem.partial = bool(payload.get('partial'))
    if item.submission_type == ProblemSubmissionType.CODE:
        problem.time_limit = editor._num(payload.get('time_limit'), problem.time_limit)
        problem.memory_limit = int(editor._num(payload.get('memory_limit'), problem.memory_limit / 1024) * 1024)
    problem.group = editor._resolve_group(payload)
    problem.is_public = editor._resolve_public(request.user, bool(payload.get('is_public')), payload, problem=problem)
    problem.save()
    editor._attach_types(problem, payload)
    editor._attach_orgs(problem, payload, request.user)
    if item.submission_type == ProblemSubmissionType.CODE:
        editor._attach_languages(problem, payload)
    problem.is_organization_private = problem.organizations.exists()
    problem.save(update_fields=['is_organization_private'])
    editor._apply_advanced(problem, payload, request, is_create=True)
    if item.submission_type == ProblemSubmissionType.CODE:
        _apply_editor_testcase_configuration(problem, payload)


def _apply_editor_testcase_configuration(problem, payload):
    """Keep the unified editor's checker, case weights, and pretest flags on imported code problems."""
    try:
        data = problem.data_files
    except ProblemData.DoesNotExist:
        data = None
    if data is not None:
        data.checker = str(payload.get('checker') or '').strip()
        data.save(update_fields=['checker'])
    testcases = payload.get('testcases') or []
    for case, editor_case in zip(problem.cases.order_by('order'), testcases):
        if not isinstance(editor_case, dict):
            continue
        try:
            case.points = float(editor_case.get('points', case.points))
        except (TypeError, ValueError):
            pass
        case.is_pretest = bool(editor_case.get('pretest'))
        case.save(update_fields=['points', 'is_pretest'])


def _save_problem_with_unique_code(problem):
    """Retry only a generated-code collision before any problem side effects are written."""
    for attempt in range(3):
        try:
            # The caller may already be atomic; a savepoint keeps that transaction usable after a collision.
            with transaction.atomic():
                problem.save()
            return
        except IntegrityError:
            if not Problem.objects.filter(code=problem.code).exists():
                raise
            if attempt == 2:
                raise ValueError('Could not allocate a unique problem code. Please try creating the problem again.')
            problem.code = ''
            apply_simple_problem_defaults(problem)


def _edit_url(problem):
    if problem.submission_type == ProblemSubmissionType.MCQ:
        return reverse('problem_mcq_edit', args=(problem.code,))
    if problem.submission_type == ProblemSubmissionType.FILL_BLANK:
        return reverse('problem_fillblank_edit', args=(problem.code,))
    if problem.submission_type == ProblemSubmissionType.SHORT_ANSWER:
        return reverse('problem_edit_v2', args=(problem.code,))
    return reverse('admin:judge_problem_change', args=(problem.pk,))
