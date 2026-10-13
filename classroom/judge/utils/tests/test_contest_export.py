"""AUTH-06: the pandoc/lualatex assignment export must not run TeX from problem text, read server files
or fetch URLs. The runtime tests need pandoc, lualatex and pdftotext (as on the server); they are
skipped where those tools are missing."""
import os
import shutil
import struct
import subprocess
import tempfile
import zipfile
import zlib
from io import BytesIO
from types import SimpleNamespace

from django.test import SimpleTestCase, override_settings
from django.utils import timezone

from judge.utils.contest_export import (FILTER_PATH, INPUT_FORMAT, build_contest_export_html,
                                        convert_contest_html, pandoc_environment)

HAVE_PANDOC = shutil.which('pandoc') is not None
HAVE_LATEX = HAVE_PANDOC and shutil.which('lualatex') is not None and shutil.which('pdftotext') is not None


def _contest(descriptions):
    problems = [
        SimpleNamespace(display_name='', problem=SimpleNamespace(name='P%d' % i, points=10, description=text))
        for i, text in enumerate(descriptions, 1)
    ]
    contest = SimpleNamespace(name='Exam', description='', start_time=timezone.now(), end_time=timezone.now())
    return contest, problems


def _png_chunk(kind, data):
    return struct.pack('>I', len(data)) + kind + data + struct.pack('>I', zlib.crc32(kind + data) & 0xffffffff)


class ExportSecretMixin:
    marker = 'FAIRPLAYSECRETMARKER'

    def setUp(self):
        super().setUp()
        self._secret_dir = tempfile.TemporaryDirectory()
        self.secret_path = os.path.join(self._secret_dir.name, 'local_settings.py')
        with open(self.secret_path, 'w') as f:
            f.write('%s 42\n' % self.marker)

    def tearDown(self):
        self._secret_dir.cleanup()
        super().tearDown()

    def attack_descriptions(self):
        path = self.secret_path
        return [
            'Dollar math: $\\text{\\input{%s}}$ end.' % path,
            'Paren math: \\(\\text{\\input{%s}}\\) end.' % path,
            '<script type="math/tex">\\text{\\input{%s}}</script>' % path,
            'Lua: $\\text{\\directlua{tex.print((io.open("%s"):read("*a")))}}$' % path,
            'Caret: $\\^^69nput{%s}$ and csname: $\\csname input\\endcsname{%s}$' % (path, path),
            'Pdf object: $\\pdfobj file {%s}$' % path,
            'Local image: <img src="%s" alt="local"> remote image: <img src="http://127.0.0.1:9/x.png" alt="r">'
            % path,
            'Normal math stays: $\\frac{a}{b} + \\sqrt{x}$',
        ]


class PandocFilterTest(ExportSecretMixin, SimpleTestCase):
    def _to_latex(self, html):
        result = subprocess.run(
            ['pandoc', '-f', INPUT_FORMAT, '-t', 'latex', '--sandbox', '--lua-filter', FILTER_PATH],
            input=html.encode(), stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=60,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        return result.stdout.decode()

    def setUp(self):
        if not HAVE_PANDOC:
            self.skipTest('pandoc is not installed')
        super().setUp()

    def test_dangerous_tex_never_reaches_latex(self):
        contest, problems = _contest(self.attack_descriptions())
        latex = self._to_latex(build_contest_export_html(contest, problems))
        for command in ('\\input{', '\\directlua', '\\^^', '\\csname', '\\pdfobj', '\\includegraphics'):
            self.assertNotIn(command, latex)
        self.assertIn('\\frac{a}{b}', latex)
        self.assertNotIn(self.marker, latex)


class PandocRuntimeTest(ExportSecretMixin, SimpleTestCase):
    def setUp(self):
        if not HAVE_LATEX:
            self.skipTest('pandoc, lualatex or pdftotext is not installed')
        super().setUp()

    def test_pdf_export_does_not_expand_input_of_server_files(self):
        contest, problems = _contest(self.attack_descriptions())
        pdf = convert_contest_html(build_contest_export_html(contest, problems), 'pdf')
        self.assertTrue(pdf.startswith(b'%PDF'))
        text = subprocess.run(['pdftotext', '-', '-'], input=pdf, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                              timeout=60).stdout
        self.assertIn(b'Normal math stays', text)
        self.assertNotIn(self.marker.encode(), text)
        self.assertNotIn(self.marker.encode(), pdf)

    def test_word_export_does_not_embed_server_files(self):
        contest, problems = _contest(self.attack_descriptions())
        docx = convert_contest_html(build_contest_export_html(contest, problems), 'docx')
        with zipfile.ZipFile(BytesIO(docx)) as archive:
            for name in archive.namelist():
                self.assertNotIn(self.marker.encode(), archive.read(name), name)

    def test_site_media_images_are_kept(self):
        png = (b'\x89PNG\r\n\x1a\n' + _png_chunk(b'IHDR', struct.pack('>IIBBBBB', 1, 1, 8, 0, 0, 0, 0)) +
               _png_chunk(b'IDAT', zlib.compress(b'\x00\x00')) + _png_chunk(b'IEND', b''))
        with tempfile.TemporaryDirectory() as media_root:
            os.makedirs(os.path.join(media_root, 'martor'))
            with open(os.path.join(media_root, 'martor', 'figure.png'), 'wb') as f:
                f.write(png)
            with override_settings(MEDIA_ROOT=media_root, MEDIA_URL='/media/'):
                contest, problems = _contest(['Figure: ![figure](/media/martor/figure.png)'])
                docx = convert_contest_html(build_contest_export_html(contest, problems), 'docx')
        with zipfile.ZipFile(BytesIO(docx)) as archive:
            media = [name for name in archive.namelist() if name.startswith('word/media/')]
        self.assertEqual(len(media), 1)

    def test_lualatex_cannot_read_absolute_paths(self):
        """Defence in depth: even raw \\input in the .tex cannot open a file outside the work dir."""
        with tempfile.TemporaryDirectory() as workdir:
            with open(os.path.join(workdir, 'doc.tex'), 'w') as f:
                f.write('\\documentclass{article}\\begin{document}\\input{%s}\\end{document}\n' % self.secret_path)
            result = subprocess.run(
                ['lualatex', '-no-shell-escape', '-interaction=nonstopmode', '-halt-on-error', 'doc.tex'],
                cwd=workdir, env=pandoc_environment(workdir), stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                timeout=120,
            )
            self.assertNotEqual(result.returncode, 0)
            pdf_path = os.path.join(workdir, 'doc.pdf')
            if os.path.exists(pdf_path):
                with open(pdf_path, 'rb') as f:
                    self.assertNotIn(self.marker.encode(), f.read())
