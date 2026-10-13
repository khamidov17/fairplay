import io
import json
import zipfile
from types import SimpleNamespace
from unittest import mock

from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import SimpleTestCase

from judge.utils.excel_embedded_documents import (
    _description_text, image_placeholder, read_xlsx_rows_with_embedded_files,
)
from judge.utils.simple_xlsx import build_xlsx_workbook
from judge.views.problem_import import (
    _auto_fill_blank_config, _draft_image_url, _extract_labeled_multiple_choice_options, _fill_blanks_from_signs,
    _format_with_llm, _header_name,
    _import_problem_name, _import_question_type, _matching_import_languages, _multiple_choice_answer_indices,
    _normalise_type, _parse_fill_blanks, _parse_text_test_data,
    _parse_zip_test_data, _promote_draft_images, _read_test_data_zip, _remove_multiple_choice_option_block,
    _serialize_fill_blanks, _validate_problem_import_item,
)


class ProblemImportTestDataParsingTestCase(SimpleTestCase):
    def test_language_matching_only_ignores_capitalization(self):
        java8 = SimpleNamespace(key='JAVA8', name='Java 8', common_name='Java')

        self.assertEqual(_matching_import_languages([java8], 'java8'), [java8])
        self.assertEqual(_matching_import_languages([java8], 'java 8'), [java8])
        self.assertEqual(_matching_import_languages([java8], 'java'), [])

    def test_docx_images_remain_in_memory_until_a_draft_is_created(self):
        document = io.BytesIO()
        with zipfile.ZipFile(document, 'w') as archive:
            archive.writestr('word/document.xml', """
                <w:document xmlns:w="urn:w" xmlns:a="urn:a" xmlns:r="urn:r">
                  <w:body><w:p><w:r><w:t>Figure:</w:t></w:r>
                  <w:r><a:blip r:embed="rId1" /></w:r></w:p></w:body>
                </w:document>
            """)
            archive.writestr('word/_rels/document.xml.rels', """
                <Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">
                  <Relationship Id="rId1"
                    Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/image"
                    Target="media/figure.png" />
                </Relationships>
            """)
            archive.writestr('word/media/figure.png', b'png-data')

        text, images = _description_text('question.docx', document.getvalue())

        self.assertEqual(len(images), 1)
        self.assertEqual(images[0].filename, 'figure.png')
        self.assertEqual(images[0].content, b'png-data')
        self.assertIn(image_placeholder(images[0].token), text)

    def test_embedded_docx_zip_bomb_is_rejected(self):
        """AUTH-11: a small deflated DOCX that expands to a huge document.xml is refused before reading."""
        document = io.BytesIO()
        body = '<w:document xmlns:w="urn:w"><w:body><w:p><w:r><w:t>%s</w:t></w:r></w:p></w:body></w:document>'
        with zipfile.ZipFile(document, 'w', compression=zipfile.ZIP_DEFLATED) as archive:
            archive.writestr('word/document.xml', body % ('a' * (64 * 1024 * 1024)))
        self.assertLess(len(document.getvalue()), 4 * 1024 * 1024)
        with self.assertRaisesRegex(ValueError, 'too large'):
            _description_text('question.docx', document.getvalue())

    def test_embedded_docx_with_highly_compressed_part_is_rejected(self):
        document = io.BytesIO()
        with zipfile.ZipFile(document, 'w', compression=zipfile.ZIP_DEFLATED) as archive:
            archive.writestr('word/document.xml', '<w:document xmlns:w="urn:w"><w:body/></w:document>')
            archive.writestr('word/media/figure.png', b'\0' * (4 * 1024 * 1024))
        with self.assertRaisesRegex(ValueError, 'too large'):
            _description_text('question.docx', document.getvalue())

    def test_embedded_docx_with_normal_compression_is_read(self):
        document = io.BytesIO()
        with zipfile.ZipFile(document, 'w', compression=zipfile.ZIP_DEFLATED) as archive:
            archive.writestr('word/document.xml', '<w:document xmlns:w="urn:w"><w:body><w:p><w:r>'
                                                  '<w:t>Sum two numbers.</w:t></w:r></w:p></w:body></w:document>')
        text, images = _description_text('question.docx', document.getvalue())
        self.assertEqual(text, 'Sum two numbers.')
        self.assertEqual(images, [])

    def test_draft_image_url_accepts_compact_uuid_tokens_from_existing_drafts(self):
        self.assertEqual(
            _draft_image_url(7, 'e46f2ff7126a4bed8d5f80c12a37c6ca'),
            '/problems/import-excel/7/images/e46f2ff7-126a-4bed-8d5f-80c12a37c6ca/',
        )

    @mock.patch('judge.views.problem_import.default_storage.delete')
    @mock.patch('judge.views.problem_import.store_docx_image')
    def test_cleans_up_partially_promoted_images_after_storage_error(self, store_docx_image, delete):
        first = SimpleNamespace(token='00000000-0000-0000-0000-000000000001', filename='first.png', content=b'first')
        second = SimpleNamespace(token='00000000-0000-0000-0000-000000000002', filename='second.png', content=b'second')
        item = SimpleNamespace(
            draft_id=7,
            images=SimpleNamespace(all=lambda: [first, second]),
        )
        description = (
            '![](/problems/import-excel/7/images/00000000-0000-0000-0000-000000000001/) '
            '![](/problems/import-excel/7/images/00000000-0000-0000-0000-000000000002/)'
        )
        store_docx_image.side_effect = [
            ('martor/first.png', '/media/martor/first.png'),
            OSError('storage failed'),
        ]

        with self.assertRaises(OSError):
            _promote_draft_images(item, description)

        delete.assert_called_once_with('martor/first.png')

    def test_reads_the_problems_sheet_when_instructions_come_first(self):
        workbook = build_xlsx_workbook([
            ('Instructions', [['Import Instructions']]),
            ('Problems', [['name*', 'problem_type*', 'description*'], ['Example', 'code', 'Body']]),
        ])

        rows, attachments = read_xlsx_rows_with_embedded_files(io.BytesIO(workbook))

        self.assertEqual(rows[0], ['name', 'question_type', 'description'])
        self.assertEqual(rows[1], ['Example', 'code', 'Body'])
        self.assertEqual(attachments, [])

    def test_skips_protected_example_problem_sheet(self):
        workbook = build_xlsx_workbook([
            ('Example Problems', [['name', 'question_type'], ['Example', 'code']], (), True),
            ('Problems', [['name', 'question_type'], ['Teacher problem', 'code']]),
        ])

        rows, attachments = read_xlsx_rows_with_embedded_files(io.BytesIO(workbook))

        self.assertEqual(rows, [['name', 'question_type'], ['Teacher problem', 'code']])
        self.assertEqual(attachments, [])

    def test_removes_leading_blank_lines_from_text_test_data(self):
        cases = _parse_text_test_data('input data 1:\n\nfirst\noutput data 1:\n\nsecond')

        self.assertEqual(cases, [('case001.in', 'first', 'case001.out', 'second')])

    def test_merges_every_recognized_problem_sheet_and_aligns_headers(self):
        workbook = build_xlsx_workbook([
            ('Question Bank A', [['Title', 'Problem Type', 'Content'], ['First', 'Coding', 'One']]),
            ('Question Bank B', [['Description', 'Name', 'Question Type'], ['Two', 'Second', 'Cloze']]),
        ])

        rows, attachments = read_xlsx_rows_with_embedded_files(io.BytesIO(workbook))

        self.assertEqual(rows, [
            ['name', 'question_type', 'description'],
            ['First', 'Coding', 'One'],
            ['Second', 'Cloze', 'Two'],
        ])
        self.assertEqual(attachments, [])

    def test_normalizes_new_template_column_headers(self):
        self.assertEqual(_header_name('problem_type*'), 'question_type')
        self.assertEqual(_header_name('tag（less than 20 characters, spaced with commas)'), 'tags')
        self.assertEqual(_header_name('difficulty\n(easy:0; medium:1; difficult:2)'), 'difficulty')
        self.assertEqual(_header_name('explanation'), 'solution')

    def test_maps_multiple_choice_excel_answers_without_llm_correctness(self):
        self.assertEqual(_multiple_choice_answer_indices('B|3', ['one', 'two', 'three']), [2, 3])

    def test_matches_common_teacher_question_type_names(self):
        self.assertEqual(_normalise_type('Programming Problem'), 'C')
        self.assertEqual(_normalise_type('Single Choice Question'), 'M')
        self.assertEqual(_normalise_type('Fill-in-the-Blank'), 'B')

    def test_keeps_unrecognized_question_type_editable_in_preview(self):
        question_type, warning = _import_question_type('Written response')

        self.assertEqual(question_type, 'C')
        self.assertIn('Written response', warning)

    def test_generates_a_reviewable_name_for_a_problem_without_one(self):
        name, needs_review = _import_problem_name('', 7)

        self.assertEqual(name, 'Missing name problem 7')
        self.assertTrue(needs_review)

    def test_requires_question_type_confirmation_before_batch_creation(self):
        item = SimpleNamespace(name_needs_review=False, type_needs_review=True)

        with self.assertRaisesMessage(ValueError, 'Select or confirm'):
            _validate_problem_import_item(item)

    def test_requires_a_replacement_for_a_generated_missing_name(self):
        item = SimpleNamespace(name_needs_review=True)

        with self.assertRaisesMessage(ValueError, 'missing-name placeholder'):
            _validate_problem_import_item(item)

    def test_extracts_consecutive_lettered_multiple_choice_options(self):
        options, block = _extract_labeled_multiple_choice_options(
            'Which number is prime?\nA. 4\nB. 5\nC. 6\nExplain your answer.',
        )

        self.assertEqual(options, ['4', '5', '6'])
        self.assertEqual(block, 'A. 4\nB. 5\nC. 6')

    def test_does_not_treat_unlabeled_paragraphs_as_options(self):
        description = 'What is 1 + 2?\n\n2\n\n3\n\n4\n\n5\n\nThis explains the calculation.'

        self.assertEqual(_extract_labeled_multiple_choice_options(description), ([], ''))

    def test_does_not_extract_incomplete_lettered_multiple_choice_options(self):
        self.assertEqual(
            _extract_labeled_multiple_choice_options('Question\nA. one\nC. three'),
            ([], ''),
        )

    def test_maps_blank_signs_to_pipe_separated_answers(self):
        description, blanks = _fill_blanks_from_signs(
            'The capital is [[blank]] and 2 + 2 = [[blank]].', 'Paris|4',
        )
        self.assertEqual(description, 'The capital is **[Blank 1]** and 2 + 2 = **[Blank 2]**.')
        self.assertEqual(blanks, [('Blank 1', 'Paris'), ('Blank 2', '4')])

    def test_auto_maps_blank_signs_from_embedded_description_content(self):
        blanks = _auto_fill_blank_config('', 'Read the attachment: [[blank]].', 'answer')

        self.assertEqual(_parse_fill_blanks(blanks), [('Blank 1', 'answer')])

    @mock.patch('judge.views.problem_import.llm.chat')
    def test_rejects_non_string_multiple_choice_option_text_from_llm(self, chat):
        chat.return_value = SimpleNamespace(text=json.dumps({
            'options': [{'text': 1}],
            'option_block': 'A. 1',
        }))
        item = SimpleNamespace(submission_type='M', description='Question', answer='A')

        with self.assertRaisesMessage(ValueError, 'invalid multiple-choice options'):
            _format_with_llm(item)

    def test_rejects_mismatched_blank_sign_answers(self):
        with self.assertRaisesMessage(ValueError, 'separated by |'):
            _fill_blanks_from_signs('A [[blank]] B [[blank]]', 'one')

    def test_removes_only_the_exact_multiple_choice_option_block(self):
        description = 'Which number is prime?\nA. 4\nB. 5\nExplain your answer.'
        self.assertEqual(
            _remove_multiple_choice_option_block(description, 'A. 4\nB. 5'),
            'Which number is prime?\nExplain your answer.',
        )

    def test_keeps_description_when_option_block_is_not_exact(self):
        description = 'Which number is prime?\nA. 4\nB. 5'
        self.assertEqual(_remove_multiple_choice_option_block(description, 'A. four\nB. five'), description)

    def test_parses_labeled_fill_blanks_with_multiple_answers(self):
        blanks = _parse_fill_blanks(
            '[blank 1]\nlabel: Country\nanswers:\nFrance\nFrench Republic\n---\n'
            '[blank 2]\nlabel: Capital\nanswers:\nParis',
        )
        self.assertEqual(blanks, [('Country', 'France\nFrench Republic'), ('Capital', 'Paris')])

    def test_serializes_fill_blanks_in_the_preview_format(self):
        value = _serialize_fill_blanks([
            {'label': 'Country', 'answers': ['France', 'French Republic']},
            {'label': '', 'answers': ['Paris']},
        ])
        self.assertEqual(_parse_fill_blanks(value), [('Country', 'France\nFrench Republic'), ('', 'Paris')])

    def test_parses_multiple_text_test_data_pairs(self):
        cases = _parse_text_test_data(
            'input data 1: 2 3\noutput data 1: 5\ninput data 2: hello\nworld\noutput data 2: ok',
        )
        self.assertEqual(cases, [
            ('case001.in', '2 3', 'case001.out', '5'),
            ('case002.in', 'hello\nworld', 'case002.out', 'ok'),
        ])

    def test_parses_zip_test_data_pairs(self):
        payload = io.BytesIO()
        with zipfile.ZipFile(payload, 'w') as archive:
            archive.writestr('sample.in', '1 2')
            archive.writestr('sample.out', '3')
        uploaded = SimpleUploadedFile('tests.zip', payload.getvalue(), content_type='application/zip')

        archive_payload, cases = _parse_zip_test_data(uploaded)

        self.assertEqual(archive_payload, payload.getvalue())
        self.assertEqual(cases, [('sample.in', b'1 2', 'sample.out', b'3')])

    def test_reads_draft_zip_in_a_context_manager(self):
        payload = io.BytesIO()
        with zipfile.ZipFile(payload, 'w') as archive:
            archive.writestr('sample.in', '1 2')
            archive.writestr('sample.out', '3')
        uploaded = SimpleUploadedFile('tests.zip', payload.getvalue(), content_type='application/zip')
        draft_zip = mock.MagicMock()
        draft_zip.open.return_value.__enter__.return_value = uploaded
        item = SimpleNamespace(test_data_zip=draft_zip)

        _read_test_data_zip(item)

        draft_zip.open.assert_called_once_with('rb')
        draft_zip.open.return_value.__exit__.assert_called_once()

    def test_rejects_unpaired_zip_test_data(self):
        payload = io.BytesIO()
        with zipfile.ZipFile(payload, 'w') as archive:
            archive.writestr('sample.in', '1 2')
        uploaded = SimpleUploadedFile('tests.zip', payload.getvalue(), content_type='application/zip')

        with self.assertRaisesMessage(ValueError, 'matching .in and .out'):
            _parse_zip_test_data(uploaded)
