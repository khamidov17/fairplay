import io
import re
import zipfile
from xml.etree import ElementTree as ET
from xml.sax.saxutils import escape


MAIN_NS = 'http://schemas.openxmlformats.org/spreadsheetml/2006/main'
REL_NS = 'http://schemas.openxmlformats.org/officeDocument/2006/relationships'
PKG_REL_NS = 'http://schemas.openxmlformats.org/package/2006/relationships'
MAX_XLSX_UPLOAD_BYTES = 2 * 1024 * 1024
MAX_XLSX_TOTAL_UNCOMPRESSED_BYTES = 8 * 1024 * 1024
MAX_XLSX_XML_PART_BYTES = 4 * 1024 * 1024
MAX_XLSX_COMPRESSION_RATIO = 100
MAX_XLSX_COLUMNS = 16384
PROBLEM_NAME_HEADERS = {
    'name', 'title', 'problem_name', 'problem_title', 'question', 'question_name', 'question_title',
}
PROBLEM_TYPE_HEADERS = {
    'type', 'problem_type', 'question_type', 'question_category', 'question_kind',
}
PROBLEM_DESCRIPTION_HEADERS = {
    'content', 'description', 'problem_description', 'question_description', 'question_text', 'statement',
}
EXAMPLE_PROBLEM_SHEET_NAMES = {'example problem', 'example problems'}


def _excel_column_name(index):
    result = ''
    value = index + 1
    while value:
        value, remainder = divmod(value - 1, 26)
        result = chr(65 + remainder) + result
    return result


def _sheet_xml(rows, shared_string_lookup, validations=(), protected=False):
    sheet_rows = []
    for row_index, row in enumerate(rows, start=1):
        cells = []
        for col_index, value in enumerate(row):
            if value is None or value == '':
                continue
            text = str(value)
            string_index = shared_string_lookup[text]
            ref = '{}{}'.format(_excel_column_name(col_index), row_index)
            cells.append('<c r="{ref}" t="s"><v>{idx}</v></c>'.format(ref=ref, idx=string_index))
        sheet_rows.append('<row r="{idx}">{cells}</row>'.format(idx=row_index, cells=''.join(cells)))
    validation_xml = []
    for validation in validations:
        values = validation.get('values', ())
        sqref = validation.get('sqref', '')
        if not values or not sqref:
            continue
        formula = '"{}"'.format(','.join(str(value) for value in values))
        validation_xml.append(
            '<dataValidation type="list" allowBlank="1" showErrorMessage="1" sqref="{sqref}">'
            '<formula1>{formula}</formula1></dataValidation>'.format(
                sqref=escape(sqref), formula=escape(formula),
            ),
        )
    validation_block = ''
    if validation_xml:
        validation_block = '<dataValidations count="{count}">{items}</dataValidations>'.format(
            count=len(validation_xml), items=''.join(validation_xml),
        )
    protection_block = '<sheetProtection sheet="1" objects="1" scenarios="1"/>' if protected else ''
    return (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<worksheet xmlns="{main}"><sheetData>{rows}</sheetData>{protection}{validations}'
        '</worksheet>'
    ).format(main=MAIN_NS, rows=''.join(sheet_rows), protection=protection_block, validations=validation_block)


def build_xlsx_workbook(sheets):
    shared_strings = []
    shared_string_lookup = {}
    total_string_count = 0

    for sheet in sheets:
        _, rows = sheet[:2]
        for row in rows:
            for value in row:
                if value is None or value == '':
                    continue
                text = str(value)
                total_string_count += 1
                if text not in shared_string_lookup:
                    shared_string_lookup[text] = len(shared_strings)
                    shared_strings.append(text)

    shared_strings_xml = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<sst xmlns="{main}" count="{count}" uniqueCount="{unique}">{items}</sst>'
    ).format(
        main=MAIN_NS,
        count=total_string_count,
        unique=len(shared_strings),
        items=''.join('<si><t xml:space="preserve">{}</t></si>'.format(escape(text)) for text in shared_strings),
    )

    workbook_sheets = []
    workbook_rels = []
    content_type_overrides = [
        '<Override PartName="/xl/workbook.xml" '
        'ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml"/>',
        '<Override PartName="/xl/styles.xml" '
        'ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.styles+xml"/>',
        '<Override PartName="/xl/sharedStrings.xml" '
        'ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.sharedStrings+xml"/>',
    ]

    for index, sheet in enumerate(sheets, start=1):
        sheet_name, _ = sheet[:2]
        workbook_sheets.append(
            '<sheet name="{name}" sheetId="{idx}" r:id="rId{idx}"/>'.format(
                name=escape(sheet_name),
                idx=index,
            ),
        )
        workbook_rels.append(
            '<Relationship Id="rId{idx}" '
            'Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/worksheet" '
            'Target="worksheets/sheet{idx}.xml"/>'.format(idx=index),
        )
        content_type_overrides.append(
            '<Override PartName="/xl/worksheets/sheet{idx}.xml" '
            'ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.worksheet+xml"/>'.format(
                idx=index,
            ),
        )

    workbook_xml = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<workbook xmlns="{main}" xmlns:r="{rel}"><sheets>{sheets}</sheets></workbook>'
    ).format(main=MAIN_NS, rel=REL_NS, sheets=''.join(workbook_sheets))

    workbook_rels_xml = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<Relationships xmlns="{pkg_rel}">{sheet_rels}'
        '<Relationship Id="rId{styles}" '
        'Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/styles" '
        'Target="styles.xml"/>'
        '<Relationship Id="rId{strings}" '
        'Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/sharedStrings" '
        'Target="sharedStrings.xml"/>'
        '</Relationships>'
    ).format(
        pkg_rel=PKG_REL_NS,
        sheet_rels=''.join(workbook_rels),
        styles=len(sheets) + 1,
        strings=len(sheets) + 2,
    )

    content_types_xml = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
        '<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>'
        '<Default Extension="xml" ContentType="application/xml"/>'
        '{overrides}'
        '</Types>'
    ).format(overrides=''.join(content_type_overrides))

    root_rels_xml = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<Relationships xmlns="{pkg_rel}">'
        '<Relationship Id="rId1" '
        'Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" '
        'Target="xl/workbook.xml"/>'
        '</Relationships>'
    ).format(pkg_rel=PKG_REL_NS)

    styles_xml = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<styleSheet xmlns="{main}">'
        '<fonts count="1"><font><sz val="11"/><name val="Calibri"/></font></fonts>'
        '<fills count="1"><fill><patternFill patternType="none"/></fill></fills>'
        '<borders count="1"><border/></borders>'
        '<cellStyleXfs count="1"><xf/></cellStyleXfs>'
        '<cellXfs count="1"><xf xfId="0"/></cellXfs>'
        '<cellStyles count="1"><cellStyle name="Normal" xfId="0" builtinId="0"/></cellStyles>'
        '</styleSheet>'
    ).format(main=MAIN_NS)

    output = io.BytesIO()
    with zipfile.ZipFile(output, 'w', zipfile.ZIP_DEFLATED) as archive:
        archive.writestr('[Content_Types].xml', content_types_xml)
        archive.writestr('_rels/.rels', root_rels_xml)
        archive.writestr('xl/workbook.xml', workbook_xml)
        archive.writestr('xl/_rels/workbook.xml.rels', workbook_rels_xml)
        archive.writestr('xl/styles.xml', styles_xml)
        archive.writestr('xl/sharedStrings.xml', shared_strings_xml)
        for index, sheet in enumerate(sheets, start=1):
            _, rows = sheet[:2]
            validations = sheet[2] if len(sheet) > 2 else ()
            protected = sheet[3] if len(sheet) > 3 else False
            archive.writestr(
                'xl/worksheets/sheet{}.xml'.format(index),
                _sheet_xml(rows, shared_string_lookup, validations, protected),
            )
    return output.getvalue()


def validate_xlsx_upload(file_obj):
    size = getattr(file_obj, 'size', None)
    if size is not None and size > MAX_XLSX_UPLOAD_BYTES:
        raise ValueError('The Excel file is too large.')


def _validate_archive_members(archive):
    total_uncompressed_bytes = 0
    for info in archive.infolist():
        if info.is_dir():
            continue
        total_uncompressed_bytes += info.file_size
        if info.file_size > MAX_XLSX_XML_PART_BYTES:
            raise ValueError('The Excel file is too large.')
        if total_uncompressed_bytes > MAX_XLSX_TOTAL_UNCOMPRESSED_BYTES:
            raise ValueError('The Excel file is too large.')
        if info.compress_size == 0:
            if info.file_size > 0:
                raise ValueError('The Excel file is invalid.')
            continue
        if info.file_size / float(info.compress_size) > MAX_XLSX_COMPRESSION_RATIO:
            raise ValueError('The Excel file is too large.')


def _safe_archive_read(archive, name):
    info = archive.getinfo(name)
    if info.file_size > MAX_XLSX_XML_PART_BYTES:
        raise ValueError('The Excel file is too large.')
    payload = archive.read(name)
    if len(payload) > MAX_XLSX_XML_PART_BYTES:
        raise ValueError('The Excel file is too large.')
    return payload


def _load_shared_strings(archive):
    try:
        payload = _safe_archive_read(archive, 'xl/sharedStrings.xml')
    except KeyError:
        return []

    root = ET.fromstring(payload)
    namespace = {'x': MAIN_NS}
    result = []
    for item in root.findall('x:si', namespace):
        text = ''.join(node.text or '' for node in item.iterfind('.//x:t', namespace))
        result.append(text)
    return result


def _find_sheet_path(archive, sheet_name=None):
    workbook = ET.fromstring(_safe_archive_read(archive, 'xl/workbook.xml'))
    rels = ET.fromstring(_safe_archive_read(archive, 'xl/_rels/workbook.xml.rels'))
    namespace = {'x': MAIN_NS, 'r': REL_NS, 'pr': PKG_REL_NS}
    first_sheet = workbook.find('x:sheets/x:sheet', namespace)
    if first_sheet is None:
        raise ValueError('Workbook does not contain any sheets.')

    sheet = first_sheet
    if sheet_name:
        expected_name = str(sheet_name).casefold()
        sheet = next(
            (candidate for candidate in workbook.findall('x:sheets/x:sheet', namespace)
             if candidate.attrib.get('name', '').casefold() == expected_name),
            None,
        )
        if sheet is None:
            raise ValueError('Workbook does not contain a sheet named "{}".'.format(sheet_name))

    rel_id = sheet.attrib.get('{%s}id' % REL_NS)
    for relation in rels.findall('pr:Relationship', namespace):
        if relation.attrib.get('Id') == rel_id:
            target = relation.attrib.get('Target', '')
            if target.startswith('/'):
                return target.lstrip('/')
            return 'xl/{}'.format(target.lstrip('/'))
    raise ValueError('Workbook sheet relationship is missing.')


def _find_first_sheet_path(archive):
    return _find_sheet_path(archive)


def _problem_header_key(value):
    text = re.sub(r'\s+', ' ', str(value or '').replace('*', '').strip().casefold())
    text = re.split(r'[（(]', text, maxsplit=1)[0].strip()
    return re.sub(r'[^a-z0-9]+', '_', text).strip('_')


def _problem_header_score(row):
    headers = {_problem_header_key(value) for value in row if _problem_header_key(value)}
    has_name = bool(headers & PROBLEM_NAME_HEADERS)
    has_type = bool(headers & PROBLEM_TYPE_HEADERS)
    if not has_name or not has_type:
        return 0
    return 8 + int(bool(headers & PROBLEM_DESCRIPTION_HEADERS))


def canonical_problem_header(value):
    """Normalize synonymous problem-table headings before multiple sheets are merged."""
    key = _problem_header_key(value)
    if key in PROBLEM_NAME_HEADERS:
        return 'name'
    if key in PROBLEM_TYPE_HEADERS:
        return 'question_type'
    if key in PROBLEM_DESCRIPTION_HEADERS:
        return 'description'
    # Keep unrecognized headings human-readable for the preview's configuration
    # review. The importer still normalizes them internally for comparisons.
    return key.replace('_', ' ')


def _xlsx_sheet_names(file_obj):
    file_obj.seek(0)
    validate_xlsx_upload(file_obj)
    with zipfile.ZipFile(file_obj) as archive:
        _validate_archive_members(archive)
        workbook = ET.fromstring(_safe_archive_read(archive, 'xl/workbook.xml'))
    namespace = {'x': MAIN_NS}
    names = [sheet.attrib.get('name', '') for sheet in workbook.findall('x:sheets/x:sheet', namespace)]
    if not names:
        raise ValueError('Workbook does not contain any sheets.')
    return names


def read_problem_xlsx_sheets(file_obj):
    """Return every worksheet with a recognizable problem table, starting at its header."""
    candidates = []
    for sheet_index, sheet_name in enumerate(_xlsx_sheet_names(file_obj)):
        if sheet_name.casefold() in EXAMPLE_PROBLEM_SHEET_NAMES:
            continue
        rows = read_xlsx_rows(file_obj, sheet_name=sheet_name)
        best_header = None
        for row_index, row in enumerate(rows[:25]):
            score = _problem_header_score(row)
            if score:
                candidate = (score, -row_index)
                if best_header is None or candidate > best_header[0]:
                    best_header = (candidate, row_index)
        if best_header is not None:
            candidates.append((sheet_index, sheet_name, best_header[1], rows))
    if not candidates:
        raise ValueError(
            'Workbook does not contain a recognizable problem sheet with name/title and question type columns.',
        )
    return [(sheet_name, header_index, rows[header_index:])
            for _sheet_index, sheet_name, header_index, rows in candidates]


def read_problem_xlsx_rows(file_obj):
    """Return the preferred recognized problem table for callers that need one sheet."""
    sheets = read_problem_xlsx_sheets(file_obj)
    sheet_name, header_index, rows = next(
        (sheet for sheet in sheets if sheet[0].casefold() == 'problems'), sheets[0],
    )
    return rows, sheet_name, header_index


def _cell_column_index(cell_ref):
    letters = ''.join(ch for ch in cell_ref if ch.isalpha())
    if not letters:
        raise ValueError('The Excel file is invalid.')
    value = 0
    for ch in letters:
        value = value * 26 + (ord(ch.upper()) - 64)
    if value > MAX_XLSX_COLUMNS:
        raise ValueError('The Excel file is invalid.')
    return value - 1


def _cell_value(cell, shared_strings, namespace):
    cell_type = cell.attrib.get('t')
    if cell_type == 'inlineStr':
        return ''.join(node.text or '' for node in cell.iterfind('.//x:t', namespace))

    raw_value = cell.findtext('x:v', default='', namespaces=namespace)
    if cell_type == 's':
        if raw_value == '':
            return ''
        try:
            return shared_strings[int(raw_value)]
        except (IndexError, ValueError):
            raise ValueError('The Excel file is invalid.')
    if cell_type == 'b':
        return 'TRUE' if raw_value == '1' else 'FALSE'
    return raw_value or ''


def read_xlsx_rows(file_obj, sheet_name=None):
    validate_xlsx_upload(file_obj)
    with zipfile.ZipFile(file_obj) as archive:
        _validate_archive_members(archive)
        shared_strings = _load_shared_strings(archive)
        sheet_path = _find_sheet_path(archive, sheet_name)
        sheet = ET.fromstring(_safe_archive_read(archive, sheet_path))

    namespace = {'x': MAIN_NS}
    rows = []
    for row in sheet.findall('x:sheetData/x:row', namespace):
        values = {}
        max_col = -1
        for cell in row.findall('x:c', namespace):
            cell_ref = cell.attrib.get('r', '')
            if not cell_ref:
                continue
            col_index = _cell_column_index(cell_ref)
            values[col_index] = _cell_value(cell, shared_strings, namespace)
            max_col = max(max_col, col_index)
        if max_col < 0:
            rows.append([])
            continue
        rows.append([values.get(index, '') for index in range(max_col + 1)])
    return rows
