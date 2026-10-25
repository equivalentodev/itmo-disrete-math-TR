"""TeX environment and font metrics: never compiles a task or a LaTeX document."""
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest

from tool.preview import LatexCompiler


@unittest.skipUnless(os.environ.get('TEXLIVE_MANIFEST') and shutil.which('pdflatex'), 'Только веб-образ с TeX Live')
class TexEnvironmentTests(unittest.TestCase):
    def test_pdftex_2026_format_and_all_shared_template_packages_available(self):
        result = subprocess.run(['pdflatex', '--version'], capture_output=True, text=True, check=True)
        self.assertIn('TeX Live 2026', result.stdout)
        subprocess.run(['latexmk', '--version'], capture_output=True, check=True)
        files = ['exam.cls', 'amssymb.sty', 'amsfonts.sty', 'amsmath.sty', 'latexsym.sty',
                 'verbatim.sty', 'xspace.sty', 'setspace.sty', 'tikz.sty', 'pgflibraryplotmarks.sty',
                 'graphicx.sty', 't2aenc.def', 'utf8.def', 'babel.sty', 'russianb.ldf', 'english.ldf',
                 'geometry.sty', 'enumitem.sty', 'multicol.sty', 'cm-super-t2a.map',
                 'larm1000.tfm', 'labx1000.tfm', 'lati1000.tfm', 'lacc1000.tfm']
        for name in files:
            with self.subTest(name=name):
                result = subprocess.run(['kpsewhich', name], capture_output=True, text=True)
                self.assertEqual(result.returncode, 0, name)
                self.assertTrue(result.stdout.strip(), name)
        result = subprocess.run(['kpsewhich', '--engine=pdftex', '--format=fmt', 'pdflatex.fmt'], capture_output=True, text=True)
        self.assertEqual(result.returncode, 0)
        self.assertTrue(result.stdout.strip())
        manifest = Path(os.environ['TEXLIVE_MANIFEST']).read_text()
        self.assertIn('2026/08/05', manifest)
        self.assertIn('name exam\nrevision ', manifest)
        self.assertIsNotNone(shutil.which('mf-nowin'))
        self.assertIsNotNone(shutil.which('gftopk'))
        subprocess.run(['mf-nowin', '--version'], capture_output=True, check=True)

    def test_cyrillic_metrics_can_be_generated_in_writable_preview_cache(self):
        # Generates only a font's metrics, never a LaTeX document or task PDF.
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            font_cache = directory / 'font-cache'
            env = LatexCompiler(font_cache=font_cache).environment(directory)
            result = subprocess.run(['mktextfm', 'larm0900'], cwd=directory, env=env,
                                    capture_output=True, text=True, timeout=30)
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            self.assertTrue(list(font_cache.rglob('larm0900.tfm')))

    def test_no_unused_engines_or_frontend_build_tools_in_runtime(self):
        for command in ('node', 'npm', 'xelatex', 'lualatex', 'luatex'):
            self.assertIsNone(shutil.which(command), command)

    def test_bundled_frontend_is_independent_of_the_repository_mount(self):
        assets = Path(os.environ['DM_WEB_ASSETS'])
        self.assertEqual(assets, Path('/app/tool/web_assets'))
        for name in ('index.html', 'style.css', 'app.js'):
            self.assertTrue((assets / name).is_file(), name)
