"""Read teacher-authored files embedded in an Excel workbook.

Excel stores ``Insert -> Object -> Create from file`` attachments as OLE packages
inside the XLSX zip. The worksheet's VML anchor tells us which spreadsheet row
owns each attachment, so an embedded document can safely augment that row's
``description`` field. ZIP files are retained for code-problem test data.
"""
import io
import posixpath
import re
import struct
import uuid
import zipfile
from dataclasses import dataclass, field
from typing import List
from urllib.parse import urljoin
from xml.etree import ElementTree as ET

from judge.utils import simple_xlsx

# Limits for a DOCX embedded in the workbook. The outer XLSX caps the embedded object itself, but
# a small deflated DOCX can expand to gigabytes, so its own members are capped before any read.
MAX_DOCX_PART_BYTES = 10 * 1024 * 1024
MAX_DOCX_TOTAL_UNCOMPRESSED_BYTES = 32 * 1024 * 1024
MAX_DOCX_COMPRESSION_RATIO = 100


@dataclass
class EmbeddedImage:
    """A DOCX image retained in memory until its import draft is valid."""
    token: str
    filename: str
    content: bytes


@dataclass
class EmbeddedFile:
    row_number: int
    filename: str
    content: bytes
    text: str = ''
    images: List[EmbeddedImage] = field(default_factory=list)


def read_xlsx_rows_with_embedded_files(file_obj):
    """Return merged problem-sheet rows and row-anchored DOCX, Markdown, or ZIP attachments."""
    problem_sheets = simple_xlsx.read_problem_xlsx_sheets(file_obj)
    rows, sheet_offsets = _merge_problem_sheets(problem_sheets)
    file_obj.seek(0)
    with zipfile.ZipFile(file_obj) as archive:
        simple_xlsx._validate_archive_members(archive)
        attachments = []
        for sheet_name, _header_index, _sheet_rows in problem_sheets:
            sheet_path = simple_xlsx._find_sheet_path(archive, sheet_name=sheet_name)
            sheet = ET.fromstring(simple_xlsx._safe_archive_read(archive, sheet_path))
            rels = _relationship_map(archive, sheet_path)
            anchors = _shape_rows(archive, sheet, rels)
            # Modern Excel wraps OLE objects in mc:AlternateContent; iterating also
            # supports the older direct-child layout. Choice/Fallback duplicates use
            # the same relationship, so process each embedded package only once.
            seen_objects = set()
            for ole in (node for node in sheet.iter() if node.tag.endswith('}oleObject')):
                relation_id = ole.attrib.get('{%s}id' % simple_xlsx.REL_NS)
                shape_id = ole.attrib.get('shapeId', '')
                object_key = (relation_id, shape_id)
                if object_key in seen_objects:
                    continue
                seen_objects.add(object_key)
                target = rels.get(relation_id)
                row_number = anchors.get(shape_id)
                if row_number is not None:
                    row_number += sheet_offsets[sheet_name]
                if not target or row_number is None:
                    raise ValueError('An embedded file is not attached to a worksheet row.')
                payload = simple_xlsx._safe_archive_read(archive, target)
                filename, content = _extract_embedded_file(payload)
                text, images = (
                    _description_text(filename, content)
                    if filename.lower().endswith(('.docx', '.md', '.markdown')) else ('', [])
                )
                attachments.append(EmbeddedFile(
                    row_number=row_number, filename=filename, content=content, text=text, images=images,
                ))
    return rows, attachments


def _merge_problem_sheets(problem_sheets):
    """Align synonymous headers and append data rows from every recognized sheet."""
    headers = []
    for _sheet_name, _header_index, rows in problem_sheets:
        for value in rows[0]:
            header = simple_xlsx.canonical_problem_header(value)
            if header and header not in headers:
                headers.append(header)

    merged_rows = [headers]
    sheet_offsets = {}
    next_row_number = 2
    for sheet_name, header_index, rows in problem_sheets:
        sheet_offsets[sheet_name] = next_row_number - header_index - 2
        positions = {
            simple_xlsx.canonical_problem_header(value): index
            for index, value in enumerate(rows[0])
            if simple_xlsx.canonical_problem_header(value)
        }
        for source_row in rows[1:]:
            values = []
            for header in headers:
                position = positions.get(header)
                values.append(source_row[position] if position is not None and position < len(source_row) else '')
            merged_rows.append(values)
        next_row_number += len(rows) - 1
    return merged_rows, sheet_offsets


# Retain the old name for callers outside this import flow.
read_xlsx_rows_with_embedded_descriptions = read_xlsx_rows_with_embedded_files
EmbeddedDescription = EmbeddedFile


def _relationship_map(archive, source_path):
    directory, filename = posixpath.split(source_path)
    rel_path = posixpath.join(directory, '_rels', filename + '.rels')
    try:
        root = ET.fromstring(simple_xlsx._safe_archive_read(archive, rel_path))
    except KeyError:
        return {}
    namespace = {'pr': simple_xlsx.PKG_REL_NS}
    result = {}
    for relation in root.findall('pr:Relationship', namespace):
        target = relation.attrib.get('Target', '')
        if target and not target.startswith('/'):
            target = posixpath.normpath(posixpath.join(directory, target))
        result[relation.attrib.get('Id')] = target.lstrip('/')
    return result


def _shape_rows(archive, sheet, relationships):
    namespace = {'x': simple_xlsx.MAIN_NS, 'r': simple_xlsx.REL_NS}
    legacy = sheet.find('x:legacyDrawing', namespace)
    if legacy is None:
        return {}
    vml_path = relationships.get(legacy.attrib.get('{%s}id' % simple_xlsx.REL_NS))
    if not vml_path:
        return {}
    root = ET.fromstring(simple_xlsx._safe_archive_read(archive, vml_path))
    result = {}
    for shape in root.iter():
        shape_id = shape.attrib.get('id', '')
        match = re.search(r'(\d+)$', shape_id)
        if not match:
            continue
        anchor = next((node.text for node in shape.iter() if node.tag.endswith('}Anchor')), None)
        if not anchor:
            continue
        parts = [part.strip() for part in anchor.split(',')]
        if len(parts) >= 3 and parts[2].isdigit():
            result[match.group(1)] = int(parts[2]) + 1  # Excel anchors are zero-based.
    return result


def _extract_embedded_file(payload):
    """Return the filename and body of a direct or OLE-packaged supported attachment."""
    if payload.startswith(b'PK\x03\x04'):
        try:
            with zipfile.ZipFile(io.BytesIO(payload)) as archive:
                return ('embedded.docx' if 'word/document.xml' in archive.namelist() else 'embedded.zip'), payload
        except zipfile.BadZipFile as error:
            raise ValueError('The embedded ZIP file is invalid.') from error
    try:
        import olefile
    except ImportError as error:
        raise ValueError('Embedded Office files require the olefile dependency.') from error

    try:
        container = olefile.OleFileIO(io.BytesIO(payload))
    except Exception as error:
        raise ValueError('The embedded description file is invalid.') from error
    try:
        streams = container.listdir(streams=True, storages=False)
        candidates = [stream for stream in streams if stream[-1].lower() in ('\x01ole10native', 'package')]
        if not candidates:
            candidates = streams
        for stream in candidates:
            body = container.openstream(stream).read()
            filename = _embedded_filename(body)
            if filename:
                return filename, _native_payload(body, filename)
    finally:
        container.close()
    raise ValueError('The embedded file must be a DOCX, Markdown, or ZIP file.')


def _embedded_filename(payload):
    for match in re.finditer(rb'([^\x00]{1,255}\.(?:docx|md|markdown|zip))\x00', payload, re.IGNORECASE):
        return match.group(1).decode('utf-8', 'replace').split('\\')[-1].split('/')[-1]
    return ''


def _native_payload(payload, filename):
    if filename.lower().endswith(('.docx', '.zip')):
        start = payload.find(b'PK\x03\x04')
        if start < 0:
            raise ValueError('The embedded {} file is invalid.'.format(filename.rsplit('.', 1)[-1].upper()))
        return payload[start:]

    # Ole10Native writes a 32-bit payload length immediately before the final
    # attachment bytes. Searching from the end avoids depending on version-specific
    # metadata fields while still requiring an exact stream boundary.
    for offset in range(len(payload) - 4, -1, -1):
        size = struct.unpack_from('<I', payload, offset)[0]
        if offset + 4 + size == len(payload):
            return payload[offset + 4:]
    raise ValueError('The embedded Markdown file is invalid.')


def _validate_docx_archive(archive):
    """Reject a DOCX whose members are too large or too highly compressed (zip bomb)."""
    total = 0
    for info in archive.infolist():
        if info.is_dir():
            continue
        total += info.file_size
        if info.file_size > MAX_DOCX_PART_BYTES or total > MAX_DOCX_TOTAL_UNCOMPRESSED_BYTES:
            raise ValueError('The embedded DOCX file is too large.')
        if info.compress_size == 0:
            if info.file_size > 0:
                raise ValueError('The embedded DOCX file is invalid.')
            continue
        if info.file_size / float(info.compress_size) > MAX_DOCX_COMPRESSION_RATIO:
            raise ValueError('The embedded DOCX file is too large.')


def _read_docx_member(archive, name):
    """Read one validated member; the size is checked again on the bytes actually produced."""
    with archive.open(name) as member:
        payload = member.read(MAX_DOCX_PART_BYTES + 1)
    if len(payload) > MAX_DOCX_PART_BYTES:
        raise ValueError('The embedded DOCX file is too large.')
    return payload


def _description_text(filename, payload):
    if filename.lower().endswith('.docx'):
        try:
            with zipfile.ZipFile(io.BytesIO(payload)) as archive:
                _validate_docx_archive(archive)
                root = ET.fromstring(_read_docx_member(archive, 'word/document.xml'))
                image_relationships = _docx_image_relationships(archive)
                paragraphs = []
                images = []
                image_tokens = {}
                for paragraph in root.iter():
                    if paragraph.tag.endswith('}p'):
                        parts = []
                        for node in paragraph.iter():
                            if node.tag.endswith('}t'):
                                parts.append(node.text or '')
                            elif node.tag.endswith('}blip'):
                                relationship_id = next((value for key, value in node.attrib.items()
                                                        if key.endswith('}embed')), '')
                                image_path = image_relationships.get(relationship_id)
                                if not image_path:
                                    raise ValueError('The embedded DOCX has an invalid image reference.')
                                if relationship_id not in image_tokens:
                                    image = EmbeddedImage(
                                        token=str(uuid.uuid4()),
                                        filename=posixpath.basename(image_path),
                                        content=_read_docx_member(archive, image_path),
                                    )
                                    validate_docx_image(image.filename, image.content)
                                    image_tokens[relationship_id] = image.token
                                    images.append(image)
                                parts.append('\n\n![]({})\n\n'.format(
                                    image_placeholder(image_tokens[relationship_id]),
                                ))
                        text = ''.join(parts).strip()
                        if text:
                            paragraphs.append(text)
                return '\n\n'.join(paragraphs), images
        except (KeyError, ET.ParseError, zipfile.BadZipFile) as error:
            raise ValueError('The embedded DOCX file is invalid.') from error
    try:
        return payload.decode('utf-8-sig').replace('\r\n', '\n').replace('\r', '\n'), []
    except UnicodeDecodeError as error:
        raise ValueError('Embedded Markdown must use UTF-8 encoding.') from error


def _docx_image_relationships(archive):
    """Map DOCX drawing relationship IDs to safely normalized media paths."""
    try:
        root = ET.fromstring(_read_docx_member(archive, 'word/_rels/document.xml.rels'))
    except KeyError:
        # A text-only DOCX can legitimately omit the relationships part.
        return {}
    except ET.ParseError as error:
        raise ValueError('The embedded DOCX image relationships are invalid.') from error
    result = {}
    for relation in root.iter():
        if not relation.tag.endswith('}Relationship') or not relation.attrib.get('Type', '').endswith('/image'):
            continue
        target = relation.attrib.get('Target', '')
        path = posixpath.normpath(posixpath.join('word', target)).lstrip('/')
        if path.startswith('../') or not path.startswith('word/media/'):
            raise ValueError('The embedded DOCX has an unsafe image path.')
        result[relation.attrib.get('Id')] = path
    return result


def image_placeholder(token):
    """A marker replaced with a draft-only image URL after row validation."""
    return '[[excel-docx-image:{}]]'.format(token)


def store_docx_image(filename, payload):
    """Store a confirmed DOCX figure and return its storage name and public URL."""
    import os

    from django.conf import settings
    from django.core.files.base import ContentFile
    from django.core.files.storage import default_storage

    validate_docx_image(filename, payload)
    extension = os.path.splitext(filename)[1].lower()
    if extension == '.jpeg':
        extension = '.jpg'
    name = '{}{}'.format(uuid.uuid4(), extension)
    saved_name = default_storage.save(os.path.join(settings.MARTOR_UPLOAD_MEDIA_DIR, name), ContentFile(payload))
    url_base = getattr(settings, 'MARTOR_UPLOAD_URL_PREFIX',
                       urljoin(settings.MEDIA_URL, settings.MARTOR_UPLOAD_MEDIA_DIR))
    if not url_base.endswith('/'):
        url_base += '/'
    return saved_name, urljoin(url_base, os.path.basename(saved_name))


def validate_docx_image(filename, payload):
    """Reject unsupported or oversized figures before they enter a preview draft."""
    extension = posixpath.splitext(filename)[1].lower()
    if extension not in {'.jpg', '.jpeg', '.png', '.gif'}:
        raise ValueError('Embedded DOCX figures must be PNG, JPG, or GIF images.')
    if not payload or len(payload) > 10 * 1024 * 1024:
        raise ValueError('Each embedded DOCX figure must be no larger than 10 MB.')
