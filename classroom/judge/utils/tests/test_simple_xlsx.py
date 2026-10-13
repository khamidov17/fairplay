import io
import zipfile

from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import SimpleTestCase

from judge.utils.simple_xlsx import (
    MAX_XLSX_UPLOAD_BYTES,
    MAX_XLSX_XML_PART_BYTES,
    build_xlsx_workbook,
    read_problem_xlsx_rows,
    read_xlsx_rows,
    validate_xlsx_upload,
)


class SimpleXlsxTestCase(SimpleTestCase):
    def test_build_and_read_round_trip(self):
        payload = build_xlsx_workbook([
            ('Problems', [['name', 'description'], ['A', 'B']]),
        ])

        rows = read_xlsx_rows(io.BytesIO(payload))

        self.assertEqual(rows, [['name', 'description'], ['A', 'B']])

    def test_read_rejects_missing_requested_sheet(self):
        payload = build_xlsx_workbook([
            ('Instructions', [['Use the Problems sheet.']]),
        ])

        with self.assertRaisesMessage(ValueError, 'sheet named "Problems"'):
            read_xlsx_rows(io.BytesIO(payload), sheet_name='Problems')

    def test_finds_problem_sheet_by_headers_when_its_name_is_not_problems(self):
        payload = build_xlsx_workbook([
            ('Instructions', [['Complete the question bank sheet.']]),
            (
                'Question Bank',
                [['Course questions'], ['Title', 'Problem Type', 'Content'], ['Sum', 'Programming Problem', 'Add.']],
            ),
        ])

        rows, sheet_name, header_index = read_problem_xlsx_rows(io.BytesIO(payload))

        self.assertEqual(sheet_name, 'Question Bank')
        self.assertEqual(header_index, 1)
        self.assertEqual(rows, [['Title', 'Problem Type', 'Content'], ['Sum', 'Programming Problem', 'Add.']])

    def test_build_writes_list_validation_for_select_column(self):
        payload = build_xlsx_workbook([
            (
                'Problems', [['question_type'], ['code']],
                [{'sqref': 'A2:A1000', 'values': ('code', 'multiple_choice', 'fill_blank')}],
            ),
        ])

        with zipfile.ZipFile(io.BytesIO(payload)) as archive:
            sheet = archive.read('xl/worksheets/sheet1.xml').decode()

        self.assertIn('dataValidation type="list"', sheet)
        self.assertIn('sqref="A2:A1000"', sheet)
        self.assertIn('"code,multiple_choice,fill_blank"', sheet)

    def test_validate_xlsx_upload_rejects_oversized_file(self):
        uploaded = SimpleUploadedFile(
            'big.xlsx',
            b'x' * (MAX_XLSX_UPLOAD_BYTES + 1),
            content_type='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet',
        )

        with self.assertRaisesMessage(ValueError, 'The Excel file is too large.'):
            validate_xlsx_upload(uploaded)

    def test_read_xlsx_rows_rejects_oversized_xml_part(self):
        oversized_xml = b'a' * (MAX_XLSX_XML_PART_BYTES + 1)
        payload = io.BytesIO()
        with zipfile.ZipFile(payload, 'w', zipfile.ZIP_DEFLATED) as archive:
            archive.writestr('[Content_Types].xml', oversized_xml)

        with self.assertRaisesMessage(ValueError, 'The Excel file is too large.'):
            read_xlsx_rows(io.BytesIO(payload.getvalue()))

    def test_read_xlsx_rows_rejects_invalid_shared_string_index(self):
        payload = io.BytesIO()
        with zipfile.ZipFile(payload, 'w', zipfile.ZIP_DEFLATED) as archive:
            archive.writestr(
                '[Content_Types].xml',
                (
                    '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
                    '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
                    '<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>'
                    '<Default Extension="xml" ContentType="application/xml"/>'
                    '<Override PartName="/xl/workbook.xml" '
                    'ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml"/>'
                    '<Override PartName="/xl/sharedStrings.xml" '
                    'ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.sharedStrings+xml"/>'
                    '<Override PartName="/xl/worksheets/sheet1.xml" '
                    'ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.worksheet+xml"/>'
                    '</Types>'
                ),
            )
            archive.writestr(
                '_rels/.rels',
                (
                    '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
                    '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
                    '<Relationship Id="rId1" '
                    'Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" '
                    'Target="xl/workbook.xml"/>'
                    '</Relationships>'
                ),
            )
            archive.writestr(
                'xl/workbook.xml',
                (
                    '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
                    '<workbook xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main" '
                    'xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships">'
                    '<sheets><sheet name="Problems" sheetId="1" r:id="rId1"/></sheets>'
                    '</workbook>'
                ),
            )
            archive.writestr(
                'xl/_rels/workbook.xml.rels',
                (
                    '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
                    '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
                    '<Relationship Id="rId1" '
                    'Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/worksheet" '
                    'Target="worksheets/sheet1.xml"/>'
                    '<Relationship Id="rId2" '
                    'Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/sharedStrings" '
                    'Target="sharedStrings.xml"/>'
                    '</Relationships>'
                ),
            )
            archive.writestr(
                'xl/sharedStrings.xml',
                (
                    '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
                    '<sst xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main" '
                    'count="1" uniqueCount="1"><si><t>only</t></si></sst>'
                ),
            )
            archive.writestr(
                'xl/worksheets/sheet1.xml',
                (
                    '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
                    '<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">'
                    '<sheetData><row r="1"><c r="A1" t="s"><v>9</v></c></row></sheetData>'
                    '</worksheet>'
                ),
            )

        with self.assertRaisesMessage(ValueError, 'The Excel file is invalid.'):
            read_xlsx_rows(io.BytesIO(payload.getvalue()))

    def test_read_xlsx_rows_rejects_column_reference_past_excel_limit(self):
        payload = io.BytesIO()
        with zipfile.ZipFile(payload, 'w', zipfile.ZIP_DEFLATED) as archive:
            archive.writestr(
                '[Content_Types].xml',
                (
                    '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
                    '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
                    '<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>'
                    '<Default Extension="xml" ContentType="application/xml"/>'
                    '<Override PartName="/xl/workbook.xml" '
                    'ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml"/>'
                    '<Override PartName="/xl/sharedStrings.xml" '
                    'ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.sharedStrings+xml"/>'
                    '<Override PartName="/xl/worksheets/sheet1.xml" '
                    'ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.worksheet+xml"/>'
                    '</Types>'
                ),
            )
            archive.writestr(
                '_rels/.rels',
                (
                    '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
                    '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
                    '<Relationship Id="rId1" '
                    'Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" '
                    'Target="xl/workbook.xml"/>'
                    '</Relationships>'
                ),
            )
            archive.writestr(
                'xl/workbook.xml',
                (
                    '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
                    '<workbook xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main" '
                    'xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships">'
                    '<sheets><sheet name="Problems" sheetId="1" r:id="rId1"/></sheets>'
                    '</workbook>'
                ),
            )
            archive.writestr(
                'xl/_rels/workbook.xml.rels',
                (
                    '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
                    '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
                    '<Relationship Id="rId1" '
                    'Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/worksheet" '
                    'Target="worksheets/sheet1.xml"/>'
                    '<Relationship Id="rId2" '
                    'Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/sharedStrings" '
                    'Target="sharedStrings.xml"/>'
                    '</Relationships>'
                ),
            )
            archive.writestr(
                'xl/sharedStrings.xml',
                (
                    '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
                    '<sst xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main" '
                    'count="1" uniqueCount="1"><si><t>only</t></si></sst>'
                ),
            )
            archive.writestr(
                'xl/worksheets/sheet1.xml',
                (
                    '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
                    '<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">'
                    '<sheetData><row r="1"><c r="XFE1" t="s"><v>0</v></c></row></sheetData>'
                    '</worksheet>'
                ),
            )

        with self.assertRaisesMessage(ValueError, 'The Excel file is invalid.'):
            read_xlsx_rows(io.BytesIO(payload.getvalue()))
