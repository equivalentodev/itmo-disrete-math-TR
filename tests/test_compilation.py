from pathlib import Path
import hashlib
import json
import os
import tempfile
import unittest
from unittest.mock import patch
import zipfile

from tests.test_web import FakeCompiler, fixture
from tool.compilation import CompilationError
from tool.inputs import ToolError
from tool.inputs import read_bank, read_settings
from tool.render import read_templates, render_documents, review_fragment, document_head
from tool.service import run


class FullCompilationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        fixture(self.root)
        self.csv = self.root / 'work/students/list.csv'

    def test_students_pdf_per_flow_preserves_assignments_and_source_zip(self):
        first = run(self.root, 'TR1', kind='students', csv_path=self.csv)
        state = self.root / 'work/assignments/test/TR1/current.json'
        records = json.loads(state.read_text())['records']
        compiler = FakeCompiler()
        messages = []
        result = run(self.root, 'TR1', kind='students', csv_path=self.csv, pdf=True,
                     compiler=compiler, progress=messages.append)
        self.assertEqual({p.name for p in result.pdfs}, {'flow_1.pdf', 'flow_2.pdf'})
        self.assertEqual(json.loads(state.read_text())['records'], records)
        self.assertEqual([s.path for s in compiler.calls], ['flow_1.tex', 'flow_2.tex'])
        for snapshot, name in zip(compiler.calls, ('Иванов', 'Петров')):
            self.assertIn(name, snapshot.tex)
            self.assertEqual(snapshot.tex.count('\\question'), 2)
        with zipfile.ZipFile(result.archive) as source:
            self.assertEqual(set(source.namelist()), {'flow_1.tex', 'flow_2.tex', 'README.txt'})
        with zipfile.ZipFile(result.pdf_archive) as pdf:
            self.assertEqual(set(pdf.namelist()), {'flow_1.pdf', 'flow_2.pdf'})
            self.assertIsNone(pdf.testzip())
            for path in result.pdfs:
                self.assertEqual(pdf.read(path.name), path.read_bytes())
        manifest = json.loads((result.directory / 'manifest.json').read_text())
        self.assertEqual(manifest['compiler_environment'], compiler.version)
        for path in result.pdfs:
            self.assertEqual(manifest['pdf_sha256']['pdf/' + path.name], hashlib.sha256(path.read_bytes()).hexdigest())
        self.assertTrue(any('2 из 2' in m for m in messages))
        self.assertTrue(first.archive.exists())

    def test_review_has_all_variants_no_names_and_copies_images(self):
        image = self.root / 'TR/TR1/pic/a.png'
        image.write_bytes(b'fixture-image')
        (self.root / 'TR/TR1/1/1.tex').write_text('\\question Рисунок \\includegraphics{pic/a.png}\n')
        compiler = FakeCompiler()
        result = run(self.root, 'TR1', pdf=True, compiler=compiler)
        self.assertEqual([p.name for p in result.pdfs], ['dump.pdf'])
        snapshot = compiler.calls[0]
        self.assertEqual(snapshot.tex.count('\\question'), 4)
        self.assertIn('Вариант 2', snapshot.tex)
        self.assertNotIn('Иванов', snapshot.tex)
        self.assertEqual(snapshot.assets, {'pic/a.png': b'fixture-image'})
        self.assertFalse((self.root / 'work/assignments/test/TR1/current.json').exists())

    def test_failure_does_not_publish_partial_pdfs_or_commit_assignments(self):
        old = run(self.root, 'TR1', kind='students', csv_path=self.csv)
        state = self.root / 'work/assignments/test/TR1/current.json'
        before = state.read_bytes()
        class FailingCompiler(FakeCompiler):
            def __call__(self, snapshot, directory):
                if snapshot.path == 'flow_2.tex':
                    return {'status':'error', 'line':8, 'error':'Тестовая ошибка', 'log':'diagnostic'}
                return super().__call__(snapshot, directory)
        with self.assertRaises(CompilationError) as caught:
            run(self.root, 'TR1', kind='students', csv_path=self.csv, pdf=True, compiler=FailingCompiler())
        error = caught.exception
        self.assertEqual(error.document, 'flow_2.tex')
        self.assertEqual(error.line, 8)
        self.assertEqual(state.read_bytes(), before)
        self.assertEqual(list((self.root / 'work/exports/TR1').iterdir()), [old.directory])
        self.assertTrue((error.directory / 'sources/flow_2.tex').is_file())
        self.assertEqual((error.directory / 'logs/flow_2.log').read_text(), 'diagnostic')
        self.assertFalse(list((self.root / 'work/build').glob('.prepare-*')))

    def test_incomplete_pdf_is_rejected(self):
        class BrokenCompiler(FakeCompiler):
            def __call__(self, snapshot, directory):
                result = super().__call__(snapshot, directory)
                (directory / 'preview.pdf').write_bytes(b'%PDF-partial')
                return result
        with self.assertRaisesRegex(CompilationError, 'полный PDF'):
            run(self.root, 'TR1', pdf=True, compiler=BrokenCompiler())
        self.assertEqual(list((self.root / 'work/exports/TR1').iterdir()), [])

    def test_new_source_during_compilation_rejects_release(self):
        root = self.root
        class ChangingCompiler(FakeCompiler):
            def __call__(self, snapshot, directory):
                (root / 'TR/TR1/1/3.tex').write_text('\\question New\n')
                return super().__call__(snapshot, directory)
        with self.assertRaisesRegex(ToolError, 'Исходники изменились'):
            run(self.root, 'TR1', pdf=True, compiler=ChangingCompiler())
        self.assertEqual(list((self.root / 'work/exports/TR1').iterdir()), [])

    def test_bulk_timeout_configuration_without_running_tex(self):
        with patch.dict(os.environ, {'BUILD_TIMEOUT_SECONDS':'75'}), \
                patch('tool.compilation.shutil.which', return_value='/bin/latexmk'), \
                patch('tool.compilation.LatexCompiler', return_value=FakeCompiler()) as constructor:
            run(self.root, 'TR1', pdf=True)
        self.assertEqual(constructor.call_args.args, (75,))

    def test_original_variant_line_and_context_for_review_and_repeated_student_fragments(self):
        for k in (1, 2):
            (self.root / f'TR/TR1/2/{k}.tex').write_text(
                '\\question\nТекст\n\\begin{itemize}\n\\item Пункт\n\\end{itemize}\n\\\\\n'
                f'SOURCE-{k}', encoding='utf-8')
        self.csv.write_text('ИСУ ID,ФИО,Поток,Группа\n1,Иванов,1,M3101\n2,Петров,1,M3101\n')
        class FailingCompiler(FakeCompiler):
            def __call__(self, snapshot, directory):
                self.tex = snapshot.tex
                self.line, self.marker = [(i, s) for i, s in enumerate(snapshot.tex.splitlines(), 1)
                                          if s.startswith('SOURCE-')][-1]
                return {'status':'error', 'line':self.line,
                        'error':"LaTeX Error: There's no line here to end.", 'log':'raw log'}
        for kind in ('review', 'students'):
            with self.subTest(kind=kind):
                compiler = FailingCompiler()
                with self.assertRaises(CompilationError) as caught:
                    run(self.root, 'TR1', kind=kind, csv_path=self.csv, pdf=True, compiler=compiler)
                error = caught.exception
                variant = compiler.marker.removeprefix('SOURCE-')
                self.assertEqual(error.source['path'], f'TR/TR1/2/{variant}.tex')
                self.assertEqual(error.source['line'], 7)
                self.assertEqual(error.line, compiler.line)
                self.assertIn({'line':6, 'text':'\\\\'}, error.source['context'])
                self.assertIn('переноса', error.hint)
                self.assertIn(f'TR/TR1/2/{variant}.tex, строка 7', str(error))
                saved = json.loads((error.directory / 'error.json').read_text())
                self.assertEqual(saved['source'], error.source)

    def test_template_and_unknown_location_are_not_attributed_to_a_task(self):
        class FailingCompiler(FakeCompiler):
            def __call__(self, snapshot, directory):
                return {'status':'error', 'line':1, 'error':'Template error', 'log':''}
        with self.assertRaises(CompilationError) as caught:
            run(self.root, 'TR1', pdf=True, compiler=FailingCompiler())
        self.assertEqual(caught.exception.source['path'], 'templates/head.tex')
        self.assertEqual(caught.exception.source['line'], 1)
        error = CompilationError('flow_3.tex', {'error':'Превышено время компиляции.'})
        self.assertIsNone(error.source)
        self.assertIn('flow_3.tex', str(error))

    def test_source_mapping_keeps_generated_tex_unchanged(self):
        bank = read_bank(self.root, 'TR1')
        settings = read_settings(self.root)
        templates = read_templates(self.root)
        expected = document_head(templates, settings, bank.name)
        expected += ''.join(review_fragment(n, k, text) for n, variants in enumerate(bank.tasks, 1)
                            for k, text in enumerate(variants, 1)) + templates['tail.tex']
        maps = {}
        documents = render_documents(templates, settings, bank, source_maps=maps)
        self.assertEqual(documents['dump.tex'], expected)
        for span in maps['dump.tex']:
            lines = documents['dump.tex'].splitlines()[span.start - 1:span.end]
            self.assertEqual(lines, span.text.splitlines())


if __name__ == '__main__':
    unittest.main()
