from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
import io
import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

from tool.files import Conflict, FileStore
from tool.inputs import ToolError
from tool.preview import LatexCompiler, PreviewManager
from tool.web import create_app


PROJECT = Path(__file__).resolve().parents[1]


def fixture(root):
    shutil.copytree(PROJECT / 'templates', root / 'templates')
    (root / 'config').mkdir()
    (root / 'config/project.toml').write_text('course="ДМ"\nterm="Осень 2026"\nperiod="test"\nseed=42\n', encoding='utf-8')
    for n in (1, 2):
        folder = root / f'TR/TR1/{n}'
        folder.mkdir(parents=True)
        for k in (1, 2):
            (folder / f'{k}.tex').write_text(f'\\question Задание {n}, вариант {k}.\n', encoding='utf-8')
    (root / 'TR/TR1/pic').mkdir()
    (root / 'work/students').mkdir(parents=True)
    (root / 'work/students/list.csv').write_text('ИСУ ID,ФИО,Поток,Группа\n1,Иванов,1,M3101\n2,Петров,2,M3111\n', encoding='utf-8')


class FakeCompiler:
    def __init__(self):
        self.calls = []
        self.version = 'test-v1'
        self.entered = threading.Event()
        self.release = threading.Event()
        self.release.set()
        self.failure = False

    def fingerprint(self):
        return self.version

    def __call__(self, snapshot, directory):
        self.calls.append(snapshot)
        self.entered.set()
        self.release.wait(5)
        if self.failure:
            return {'status': 'error', 'error': 'Ошибка теста', 'log': 'тестовый журнал', 'line': 1}
        # One blank fixture page, not a rendered task. No TeX process is started.
        data = b'%PDF-1.4\n'
        offsets = [0]
        objects = [b'<< /Type /Catalog /Pages 2 0 R >>',
                   b'<< /Type /Pages /Kids [3 0 R] /Count 1 >>',
                   b'<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] /Resources << >> >>']
        for i, body in enumerate(objects, 1):
            offsets.append(len(data))
            data += f'{i} 0 obj\n'.encode() + body + b'\nendobj\n'
        start = len(data)
        data += b'xref\n0 4\n0000000000 65535 f \n'
        data += b''.join(f'{offset:010} 00000 n \n'.encode() for offset in offsets[1:])
        data += f'trailer\n<< /Size 4 /Root 1 0 R >>\nstartxref\n{start}\n%%EOF\n'.encode()
        (directory / 'preview.pdf').write_bytes(data)
        return {'status': 'success', 'error': None, 'log': '', 'line': None}


def finished(manager, key):
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        state = manager.status(key)
        if state['status'] not in ('queued', 'compiling'):
            return state
        time.sleep(.005)
    raise AssertionError('Тестовая очередь не завершилась')


class FileTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        fixture(self.root)
        self.files = FileStore(self.root)

    def test_tree_does_not_validate_broken_other_task(self):
        (self.root / 'TR/TR1/2/1.tex').write_text('broken', encoding='utf-8')
        self.assertEqual(self.files.tree()[0]['tasks'][1]['variants'], [1, 2])
        self.assertIn('Задание 1', self.files.read('TR1/1/1.tex')['text'])

    def test_save_unfinished_tex_exactly_and_conflict(self):
        original = self.files.read('TR1/1/1.tex')
        result = self.files.save(original['path'], '\\question {unfinished', original['revision'])
        self.assertNotEqual(original['revision'], result['revision'])
        self.assertEqual(self.files.read(original['path'])['text'], '\\question {unfinished')
        with self.assertRaises(Conflict):
            self.files.save(original['path'], 'discarded', original['revision'])
        self.assertEqual(self.files.read(original['path'])['text'], '\\question {unfinished')
        self.assertFalse(list((self.root / 'TR/TR1/1').glob('.edit-*')))

    def test_create_concurrent_variants_and_gap(self):
        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(lambda _: self.files.create('TR1', 1), range(2)))
        self.assertEqual({r['path'] for r in results}, {'TR1/1/3.tex', 'TR1/1/4.tex'})
        (self.root / 'TR/TR1/1/2.tex').unlink()
        with self.assertRaisesRegex(ToolError, 'пропуски'):
            self.files.create('TR1', 1)

    def test_deleted_history_cleanup_refuses_symlink(self):
        folder = self.root / 'work/build/editor/deleted'
        folder.parent.mkdir(parents=True)
        folder.symlink_to(self.root / 'TR', target_is_directory=True)
        before = self.files.read('TR1/1/1.tex')['text']
        with self.assertRaisesRegex(ToolError, 'Символические ссылки'):
            self.files.clear_deleted_history()
        self.assertEqual(self.files.read('TR1/1/1.tex')['text'], before)

    def test_traversal_and_symlinks(self):
        for name in ('../config/project.toml', 'TR1/1/../1.tex', '/TR1/1/1.tex', 'TR1\\1\\1.tex'):
            with self.assertRaises(ToolError):
                self.files.read(name)
        target = self.root / 'TR/TR1/1/1.tex'
        target.unlink()
        target.symlink_to(self.root / 'config/project.toml')
        with self.assertRaises(ToolError):
            self.files.read('TR1/1/1.tex')
        self.assertEqual(self.files.tree()[0]['tasks'][0]['variants'], [2])

    def test_delete_compacts_numbers_preserves_bytes_and_backups(self):
        folder = self.root / 'TR/TR1/1'
        (folder / '3.tex').write_bytes(b'\\question third\r\n')
        (folder / '4.tex').write_bytes(b'\\question fourth')
        before = {p.name: p.read_bytes() for p in folder.glob('*.tex')}
        other = (self.root / 'TR/TR1/2/1.tex').read_bytes()
        file = self.files.read('TR1/1/2.tex')
        result = self.files.delete(file['path'], file['revision'])
        self.assertEqual(result['next'], 'TR1/1/2.tex')
        self.assertEqual(result['renamed'], 2)
        self.assertEqual({p.name: p.read_bytes() for p in folder.glob('*.tex')},
                         {'1.tex': before['1.tex'], '2.tex': before['3.tex'], '3.tex': before['4.tex']})
        for name in ('2.tex', '3.tex', '4.tex'):
            self.assertEqual((self.root / result['backup'] / name).read_bytes(), before[name])
        self.assertEqual((self.root / 'TR/TR1/2/1.tex').read_bytes(), other)

    def test_delete_stale_revision_does_not_change_bank(self):
        file = self.files.read('TR1/1/1.tex')
        self.files.save(file['path'], '\\question changed', file['revision'])
        before = {p.name: p.read_bytes() for p in (self.root / 'TR/TR1/1').glob('*.tex')}
        with self.assertRaises(Conflict):
            self.files.delete(file['path'], file['revision'])
        self.assertEqual({p.name: p.read_bytes() for p in (self.root / 'TR/TR1/1').glob('*.tex')}, before)

    def test_delete_rolls_back_interrupted_renumbering(self):
        folder = self.root / 'TR/TR1/1'
        for number in (3, 4):
            (folder / f'{number}.tex').write_text(f'\\question {number}')
        before = {p.name: p.read_bytes() for p in folder.glob('*.tex')}
        replace = os.replace
        def fail(source, target):
            if Path(source).name == '4.tex' and Path(target).name == '3.tex':
                raise OSError('test disk error')
            return replace(source, target)
        file = self.files.read('TR1/1/2.tex')
        with patch('tool.files.os.replace', side_effect=fail), self.assertRaisesRegex(ToolError, 'восстановлены'):
            self.files.delete(file['path'], file['revision'])
        self.assertEqual({p.name: p.read_bytes() for p in folder.glob('*.tex')}, before)

    def test_delete_last_variant_and_create_in_empty_task(self):
        file = self.files.read('TR1/1/2.tex')
        result = self.files.delete(file['path'], file['revision'])
        self.assertEqual(result['next'], 'TR1/1/1.tex')
        file = self.files.read(result['next'])
        self.assertIsNone(self.files.delete(file['path'], file['revision'])['next'])
        self.assertEqual(self.files.tree()[0]['tasks'][0]['variants'], [])
        self.assertEqual(self.files.create('TR1', 1)['path'], 'TR1/1/1.tex')

    def test_delete_rejects_symlinks_and_gaps(self):
        file = self.files.read('TR1/1/1.tex')
        folder = self.root / 'TR/TR1/1'
        (folder / '2.tex').unlink()
        (folder / '3.tex').write_text('\\question other')
        with self.assertRaisesRegex(ToolError, 'пропуски'):
            self.files.delete(file['path'], file['revision'])
        (folder / '3.tex').unlink()
        (folder / '2.tex').symlink_to(self.root / 'config/project.toml')
        with self.assertRaisesRegex(ToolError, 'символические ссылки'):
            self.files.delete(file['path'], file['revision'])
        self.assertEqual(self.files.read(file['path'])['revision'], file['revision'])

    def test_restore_middle_variant_restores_numbers_and_consumes_backup(self):
        folder = self.root / 'TR/TR1/1'
        (folder / '3.tex').write_bytes(b'\\question third\r\n')
        before = {p.name: p.read_bytes() for p in folder.glob('*.tex')}
        for _ in range(2):
            file = self.files.read('TR1/1/2.tex')
            deleted = self.files.delete(file['path'], file['revision'])
            restored = self.files.restore(deleted['backup'])
            self.assertEqual(restored['path'], file['path'])
            self.assertEqual({p.name: p.read_bytes() for p in folder.glob('*.tex')}, before)
            self.assertFalse((self.root / deleted['backup']).exists())

    def test_restore_only_variant_in_empty_task(self):
        file = self.files.read('TR1/1/2.tex')
        self.files.delete(file['path'], file['revision'])
        file = self.files.read('TR1/1/1.tex')
        deleted = self.files.delete(file['path'], file['revision'])
        self.assertEqual(self.files.tree()[0]['tasks'][0]['variants'], [])
        self.files.restore(deleted['backup'])
        self.assertEqual(self.files.read(file['path'])['text'], file['text'])
        self.assertEqual(self.files.tree()[0]['tasks'][0]['variants'], [1])
        self.assertFalse((self.root / deleted['backup']).exists())

    def test_restore_preserves_external_edits_and_keeps_backup(self):
        file = self.files.read('TR1/1/1.tex')
        deleted = self.files.delete(file['path'], file['revision'])
        remaining = self.files.read(file['path'])
        self.files.save(remaining['path'], '\\question external edit', remaining['revision'])
        with self.assertRaises(Conflict):
            self.files.restore(deleted['backup'])
        self.assertEqual(self.files.read(file['path'])['text'], '\\question external edit')
        self.assertTrue((self.root / deleted['backup']).is_dir())

    def test_restore_rolls_back_failed_renumbering(self):
        folder = self.root / 'TR/TR1/1'
        for number in (3, 4):
            (folder / f'{number}.tex').write_text(f'\\question {number}')
        file = self.files.read('TR1/1/2.tex')
        deleted = self.files.delete(file['path'], file['revision'])
        before = {p.name: p.read_bytes() for p in folder.glob('*.tex')}
        replace = os.replace
        def fail(source, target):
            if Path(source).name == '2.tex' and Path(target).name == '3.tex':
                raise OSError('test disk error')
            return replace(source, target)
        with patch('tool.files.os.replace', side_effect=fail), self.assertRaisesRegex(ToolError, 'не изменены'):
            self.files.restore(deleted['backup'])
        self.assertEqual({p.name: p.read_bytes() for p in folder.glob('*.tex')}, before)
        self.assertFalse(list(folder.glob('.restore-*')))
        self.files.restore(deleted['backup'])

    def test_restore_rejects_corrupt_copy_and_unsafe_path(self):
        file = self.files.read('TR1/1/1.tex')
        deleted = self.files.delete(file['path'], file['revision'])
        (self.root / deleted['backup'] / '1.tex').write_text('corrupt')
        with self.assertRaisesRegex(ToolError, 'Повреждена'):
            self.files.restore(deleted['backup'])
        with self.assertRaises(ToolError):
            self.files.restore('../config')
        self.assertEqual(self.files.read(file['path'])['text'], '\\question Задание 1, вариант 2.\n')


class PreviewTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        fixture(self.root)
        self.compiler = FakeCompiler()
        self.manager = PreviewManager(self.root, compiler=self.compiler)
        self.addCleanup(self.temp.cleanup)
        self.addCleanup(self.manager.close)

    def request(self, name='TR1/1/1.tex'):
        return self.manager.request(name, FileStore(self.root).read(name)['revision'])

    def test_no_warmup_dedup_and_restart_clears_cache(self):
        self.assertEqual(self.compiler.calls, [])
        self.compiler.release.clear()
        request = self.request()
        self.assertTrue(self.compiler.entered.wait(1))
        self.assertEqual(self.request()['key'], request['key'])
        self.compiler.release.set()
        self.assertEqual(finished(self.manager, request['key'])['status'], 'success')
        self.assertTrue(self.request()['cached'])
        self.assertEqual(len(self.compiler.calls), 1)
        self.manager.close()
        other = FakeCompiler()
        restarted = PreviewManager(self.root, compiler=other)
        self.addCleanup(restarted.close)
        self.assertEqual(list(restarted.cache.iterdir()), [])
        self.assertEqual(other.calls, [])
        request = restarted.request('TR1/1/1.tex', FileStore(self.root).read('TR1/1/1.tex')['revision'])
        self.assertFalse(request['cached'])
        self.assertEqual(finished(restarted, request['key'])['status'], 'success')
        self.assertEqual(len(other.calls), 1)
        self.assertTrue(restarted.request('TR1/1/1.tex', FileStore(self.root).read('TR1/1/1.tex')['revision'])['cached'])

    def test_dependency_invalidation_and_correct_number(self):
        path = self.root / 'TR/TR1/2/1.tex'
        path.write_text('\\question Рисунок \\includegraphics{pic/a.png}\n', encoding='utf-8')
        image = self.root / 'TR/TR1/pic/a.png'
        image.write_bytes(b'image-v1')
        keys = []
        for change in (lambda: None, lambda: image.write_bytes(b'image-v2'),
                       lambda: (self.root / 'templates/head.tex').write_text((self.root / 'templates/head.tex').read_text() + '\n% changed\n'),
                       lambda: setattr(self.compiler, 'version', 'test-v2'),
                       lambda: path.write_text(path.read_text() + '\n% new text\n')):
            change()
            request = self.request('TR1/2/1.tex')
            keys.append(request['key'])
            self.assertEqual(finished(self.manager, request['key'])['status'], 'success')
        self.assertEqual(len(set(keys)), 5)
        self.assertIn('\\begin{questions}\n\\setcounter{question}{1}', self.compiler.calls[0].tex)
        self.assertNotIn('\\textbf{Вариант ', self.compiler.calls[0].tex)
        self.assertEqual(self.compiler.calls[0].assets, {'pic/a.png': b'image-v1'})

    def test_missing_image_error_cached_and_recovers(self):
        (self.root / 'TR/TR1/1/1.tex').write_text('\\question \\includegraphics{pic/missing.png}\n')
        first = self.request()
        self.assertEqual(finished(self.manager, first['key'])['status'], 'error')
        self.assertTrue(self.request()['cached'])
        self.assertEqual(self.compiler.calls, [])
        (self.root / 'TR/TR1/pic/missing.png').write_bytes(b'fixed')
        second = self.request()
        self.assertNotEqual(first['key'], second['key'])
        self.assertEqual(finished(self.manager, second['key'])['status'], 'success')

    def test_compile_errors_cached_without_success_pdf(self):
        self.compiler.failure = True
        first = self.request()
        self.assertEqual(finished(self.manager, first['key'])['line'], 1)
        self.assertTrue(self.request()['cached'])
        with self.assertRaises(ToolError):
            self.manager.pdf(first['key'])
        self.assertEqual(len(self.compiler.calls), 1)

    def test_latest_waiting_revision_only(self):
        self.compiler.release.clear()
        first = self.request()
        self.assertTrue(self.compiler.entered.wait(1))
        path = self.root / 'TR/TR1/1/1.tex'
        path.write_text('\\question Second\n')
        second = self.request()
        path.write_text('\\question Third\n')
        third = self.request()
        self.assertEqual(self.manager.status(second['key'])['status'], 'superseded')
        self.compiler.release.set()
        finished(self.manager, first['key'])
        finished(self.manager, third['key'])
        self.assertEqual(len(self.compiler.calls), 2)
        self.assertIn('Third', self.compiler.calls[-1].tex)

    def test_cache_limit_removes_old_results(self):
        self.manager.max_bytes = 600
        first = self.request()
        finished(self.manager, first['key'])
        # Force a known large old cache item, then trigger normal eviction.
        (self.manager.cache / first['key'] / 'padding').write_bytes(b'x' * 1000)
        second = self.request('TR1/1/2.tex')
        finished(self.manager, second['key'])
        self.assertFalse((self.manager.cache / first['key']).exists())

    def test_stale_revision_rejected_and_external_input_blocked(self):
        with self.assertRaises(Conflict):
            self.manager.request('TR1/1/1.tex', 'old')
        (self.root / 'TR/TR1/1/1.tex').write_text('\\question \\input{/etc/passwd}\n')
        request = self.request()
        self.assertEqual(finished(self.manager, request['key'])['status'], 'error')
        self.assertEqual(self.compiler.calls, [])

    def test_image_symlink_cannot_read_outside_bank(self):
        (self.root / 'TR/TR1/pic/linked.png').symlink_to(self.root / 'config/project.toml')
        (self.root / 'TR/TR1/1/1.tex').write_text('\\question \\includegraphics{pic/linked.png}\n')
        request = self.request()
        self.assertEqual(finished(self.manager, request['key'])['status'], 'error')
        self.assertEqual(self.compiler.calls, [])

    def test_restart_removes_errors_and_abandoned_scratch_but_preserves_other_data(self):
        self.compiler.failure = True
        first = self.request()
        finished(self.manager, first['key'])
        self.manager.close()
        abandoned = self.manager.scratch / 'render-abandoned'
        abandoned.mkdir()
        (abandoned / 'preview.tex').write_text('unfinished')
        keep = ('work/build/preview/fonts/keep.tfm', 'work/exports/TR1/keep.pdf',
                'work/assignments/test/TR1/current.json', 'work/build/TR1/keep.tex',
                'work/build/editor/deleted/keep.tex', 'TR/TR1/1/1.tex')
        before = {}
        for name in keep:
            path = self.root / name
            path.parent.mkdir(parents=True, exist_ok=True)
            if not path.exists():
                path.write_text('keep')
            before[name] = path.read_bytes()
        # A link inside disposable data must not cause deletion of its target.
        (self.manager.cache / 'external-link').symlink_to(self.root / 'work/exports', target_is_directory=True)
        other = FakeCompiler()
        restarted = PreviewManager(self.root, compiler=other)
        self.addCleanup(restarted.close)
        self.assertEqual(list(restarted.cache.iterdir()), [])
        self.assertEqual(list(restarted.scratch.iterdir()), [])
        self.assertEqual(other.calls, [])
        for name, data in before.items():
            self.assertEqual((self.root / name).read_bytes(), data, name)

    def test_restart_refuses_symlink_instead_of_clearing_its_target(self):
        self.manager.close()
        self.manager.cache.rmdir()
        self.manager.cache.symlink_to(self.root / 'TR', target_is_directory=True)
        before = FileStore(self.root).read('TR1/1/1.tex')['text']
        with self.assertRaisesRegex(ToolError, 'Символические ссылки'):
            PreviewManager(self.root, compiler=FakeCompiler())
        self.assertEqual(FileStore(self.root).read('TR1/1/1.tex')['text'], before)

    def test_timeout_terminates_process_group_without_running_tex(self):
        snapshot = self.manager.snapshot('TR1/1/1.tex', FileStore(self.root).read('TR1/1/1.tex')['revision'])
        class Process:
            pid = 99999
            returncode = -9
            count = 0
            def wait(self, timeout=None):
                if timeout is not None:
                    raise subprocess.TimeoutExpired('fake', timeout)
        with tempfile.TemporaryDirectory() as tmp, patch('tool.preview.subprocess.Popen', return_value=Process()) as spawn, patch('tool.preview.os.killpg') as kill:
            result = LatexCompiler(.01)(snapshot, Path(tmp))
        self.assertEqual(result['status'], 'error')
        self.assertIn('время', result['error'])
        self.assertTrue(spawn.call_args.kwargs['start_new_session'])
        self.assertIn('-pdflatex=pdflatex -no-shell-escape %O %S', spawn.call_args.args[0])
        env = spawn.call_args.kwargs['env']
        self.assertTrue(env['TEXMFVAR'].endswith('/.texmf-var'))
        self.assertEqual(env['VARTEXFONTS'], env['TEXMFVAR'] + '/fonts')
        kill.assert_called_once()

    def test_font_error_in_shared_preamble_has_useful_message_not_task_line(self):
        snapshot = self.manager.snapshot('TR1/1/1.tex', FileStore(self.root).read('TR1/1/1.tex')['revision'])
        font_cache = self.root / 'font-cache'
        class Process:
            returncode = 1
            def wait(self, timeout=None):
                return 1
        def spawn(*args, **kwargs):
            kwargs['stdout'].write(b'/opt/texlive/fontenc.sty:116: Font T2A/cmr/m/n/10=larm1000 not loadable: Metric (TFM) file not found.\n')
            return Process()
        with tempfile.TemporaryDirectory() as temp, patch('tool.preview.subprocess.Popen', side_effect=spawn) as process:
            result = LatexCompiler(font_cache=font_cache)(snapshot, Path(temp))
        self.assertEqual(result['status'], 'error')
        self.assertIsNone(result['line'])
        self.assertIn('larm1000', result['error'])
        self.assertIn('fontenc.sty', result['error'])
        self.assertEqual(process.call_args.kwargs['env']['TEXMFVAR'], str(font_cache))
        self.assertTrue(font_cache.is_dir())


class WebTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        fixture(self.root)
        self.compiler = FakeCompiler()
        self.app = create_app(self.root, compiler=self.compiler)
        self.app.testing = True
        self.client = self.app.test_client()
        boot = self.client.get('/api/bootstrap').get_json()
        self.headers = {'X-CSRF-Token': boot['csrf']}
        self.addCleanup(self.temp.cleanup)
        self.addCleanup(lambda: self.app.extensions['dm']['executor'].shutdown(wait=True))
        self.addCleanup(self.app.extensions['dm']['previews'].close)

    def post(self, url, body):
        return self.client.post(url, json=body, headers=self.headers)

    def test_server_restart_clears_deleted_backups_only(self):
        file = self.client.get('/api/file?path=TR1/1/1.tex').get_json()
        deleted = self.client.delete('/api/variants', json=file, headers=self.headers).get_json()
        backup = self.root / deleted['backup']
        self.assertTrue(backup.is_dir())
        keep = ('TR/TR1/1/1.tex', 'work/exports/TR1/keep.pdf',
                'work/assignments/test/TR1/keep.json', 'work/build/TR1/keep.tex',
                'work/build/preview/fonts/keep.tfm')
        before = {}
        for name in keep:
            path = self.root / name
            path.parent.mkdir(parents=True, exist_ok=True)
            if not path.exists():
                path.write_text('keep')
            before[name] = path.read_bytes()
        (backup / 'outside-link').symlink_to(self.root / 'work/exports', target_is_directory=True)
        self.app.extensions['dm']['previews'].close()
        compiler = FakeCompiler()
        restarted = create_app(self.root, compiler=compiler)
        self.addCleanup(lambda: restarted.extensions['dm']['executor'].shutdown(wait=True))
        self.addCleanup(restarted.extensions['dm']['previews'].close)
        self.assertFalse(backup.exists())
        self.assertEqual(compiler.calls, [])
        for name, content in before.items():
            self.assertEqual((self.root / name).read_bytes(), content, name)
        client = restarted.test_client()
        token = client.get('/api/bootstrap').get_json()['csrf']
        response = client.post('/api/variants/restore', json={'backup':deleted['backup']},
                               headers={'X-CSRF-Token':token})
        self.assertEqual(response.status_code, 400)

    def wait_build(self, response):
        self.assertEqual(response.status_code, 202, response.get_json())
        identifier = response.get_json()['id']
        for _ in range(500):
            result = self.client.get(f'/api/builds/{identifier}').get_json()
            if result['status'] not in ('queued', 'running'):
                return result
            time.sleep(.005)
        self.fail('Сборка не завершилась')

    def test_local_host_origin_and_csrf_protection(self):
        self.assertEqual(self.client.get('/api/tree', headers={'Host':'evil.test'}).status_code, 403)
        self.assertEqual(self.client.put('/api/file', json={}).status_code, 403)
        self.assertEqual(self.client.post('/api/variants', json={'bank':'TR1','number':1}, headers={**self.headers,'Origin':'https://evil.test'}).status_code, 403)
        self.assertEqual(self.post('/api/variants', {'bank':'TR1','number':1}).status_code, 201)

    def test_page_and_styles_available(self):
        with self.client.get('/') as response:
            self.assertEqual(response.status_code, 200)
            self.assertIn('/assets/app.js', response.get_data(as_text=True))
        with self.client.get('/assets/style.css') as response:
            self.assertEqual(response.status_code, 200)

    def test_read_save_conflict_and_paths(self):
        file = self.client.get('/api/file?path=TR1/1/1.tex').get_json()
        response = self.client.put('/api/file', json={**file,'text':'broken {'}, headers=self.headers)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(self.client.put('/api/file', json={**file,'text':'wrong'}, headers=self.headers).status_code, 409)
        self.assertEqual(self.client.get('/api/file?path=../config/project.toml').status_code, 400)

    def test_delete_api_requires_csrf_and_current_revision(self):
        file = self.client.get('/api/file?path=TR1/1/1.tex').get_json()
        self.assertEqual(self.client.delete('/api/variants', json=file).status_code, 403)
        self.assertEqual(self.client.delete('/api/variants', json=file, headers={**self.headers, 'Origin':'https://evil.test'}).status_code, 403)
        self.assertEqual(self.client.delete('/api/variants', json={**file, 'revision':'stale'}, headers=self.headers).status_code, 409)
        self.assertEqual(self.client.delete('/api/variants', json={'path':'../config/project.toml', 'revision':''}, headers=self.headers).status_code, 400)
        response = self.client.delete('/api/variants', json=file, headers=self.headers)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.get_json()['remaining'], 1)
        self.assertEqual(self.client.get('/api/file?path=TR1/1/1.tex').get_json()['text'], '\\question Задание 1, вариант 2.\n')

    def test_restore_api_protection_and_single_use(self):
        file = self.client.get('/api/file?path=TR1/1/1.tex').get_json()
        deleted = self.client.delete('/api/variants', json=file, headers=self.headers).get_json()
        body = {'backup': deleted['backup']}
        self.assertEqual(self.client.post('/api/variants/restore', json=body).status_code, 403)
        self.assertEqual(self.client.post('/api/variants/restore', json=body, headers={**self.headers, 'Origin':'https://evil.test'}).status_code, 403)
        self.assertEqual(self.post('/api/variants/restore', body).status_code, 200)
        self.assertFalse((self.root / deleted['backup']).exists())
        self.assertEqual(self.post('/api/variants/restore', body).status_code, 400)
        self.assertEqual(self.client.get('/api/file?path=TR1/1/1.tex').get_json()['text'], file['text'])

    def test_preview_api_only_current_task(self):
        (self.root / 'TR/TR1/2/2.tex').write_text('broken')
        file = self.client.get('/api/file?path=TR1/1/1.tex').get_json()
        response = self.post('/api/previews', {'path':file['path'],'revision':file['revision']}).get_json()
        finished(self.app.extensions['dm']['previews'], response['key'])
        with self.client.get(f'/api/previews/{response["key"]}/pdf') as pdf:
            self.assertEqual(pdf.status_code, 200)
        self.assertEqual(len(self.compiler.calls), 1)

    def test_csv_upload_validates_and_never_overwrites(self):
        old = (self.root / 'work/students/list.csv').read_bytes()
        for expected in (201, 201):
            response = self.client.post('/api/rosters', data={'file':(io.BytesIO(old),'list.csv')}, headers=self.headers)
            self.assertEqual(response.status_code, expected)
            self.assertNotEqual(response.get_json()['name'], 'list.csv')
        self.assertEqual((self.root / 'work/students/list.csv').read_bytes(), old)
        response = self.client.post('/api/rosters', data={'file':(io.BytesIO(b'invalid'),'bad.csv')}, headers=self.headers)
        self.assertEqual(response.status_code, 400)
        self.assertFalse((self.root / 'work/students/bad.csv').exists())

    def test_review_export_download_and_named_distribution_preserved(self):
        review = self.wait_build(self.post('/api/builds', {'bank':'TR1','kind':'review'}))
        self.assertEqual(review['status'], 'success')
        with self.client.get('/api/exports/' + review['archive']) as archive:
            self.assertEqual(archive.status_code, 200)
        self.assertEqual(self.client.get('/api/exports/../../config/project.toml').status_code, 400)
        body = {'bank':'TR1','kind':'students','csv':'list.csv','selection':'all','values':[]}
        result = self.wait_build(self.post('/api/builds', body))
        self.assertEqual(result['students'], 2)
        self.assertEqual(result['flows'], {'1':1,'2':1})
        state = self.root / 'work/assignments/test/TR1/current.json'
        before = json.loads(state.read_text())['records']
        (self.root / 'TR/TR1/1/1.tex').write_text('\\question Changed\n')
        self.assertEqual(self.wait_build(self.post('/api/builds', body))['status'], 'success')
        self.assertEqual(before, json.loads(state.read_text())['records'])
        self.assertEqual(self.post('/api/builds', {**body,'kind':'redistribute'}).status_code, 400)
        self.assertEqual(self.wait_build(self.post('/api/builds', {**body,'kind':'redistribute','confirmed':True}))['status'], 'success')

    def test_full_pdf_build_downloads_and_failure_diagnostics(self):
        body = {'bank':'TR1', 'kind':'students', 'csv':'list.csv', 'pdf':True}
        result = self.wait_build(self.post('/api/builds', body))
        self.assertEqual(result['status'], 'success')
        self.assertEqual(len(result['pdfs']), 2)
        for path in result['pdfs']:
            with self.client.get('/api/exports/' + path) as response:
                self.assertEqual(response.status_code, 200)
                self.assertEqual(response.mimetype, 'application/pdf')
        with self.client.get('/api/exports/' + result['pdf_archive']) as response:
            self.assertEqual(response.status_code, 200)
        self.compiler.failure = True
        failed = self.wait_build(self.post('/api/builds', {**body,'kind':'review'}))
        self.assertEqual(failed['status'], 'error')
        self.assertEqual(failed['document'], 'dump.tex')
        self.assertEqual(failed['source']['path'], 'templates/head.tex')
        self.assertEqual(failed['source']['line'], 1)
        self.assertIn('тестовый журнал', failed['log'])
        self.assertTrue((self.root / failed['directory'] / 'sources/dump.tex').is_file())
        self.assertEqual(self.post('/api/builds', {**body, 'pdf':'yes'}).status_code, 400)

    def test_build_error_api_identifies_original_task_line(self):
        class FailingCompiler(FakeCompiler):
            def __call__(self, snapshot, directory):
                line = next(i for i, text in enumerate(snapshot.tex.splitlines(), 1)
                            if text.startswith('\\question Задание 2, вариант 2.'))
                return {'status':'error', 'line':line, 'error':"LaTeX Error: There's no line here to end.",
                        'log':'raw compiler log'}
        self.app.extensions['dm']['build_compiler'] = FailingCompiler()
        failed = self.wait_build(self.post('/api/builds', {'bank':'TR1', 'kind':'review', 'pdf':True}))
        self.assertEqual(failed['status'], 'error')
        self.assertEqual(failed['source']['path'], 'TR/TR1/2/2.tex')
        self.assertEqual(failed['source']['line'], 1)
        self.assertEqual(failed['source']['context'], [{'line':1, 'text':'\\question Задание 2, вариант 2.'}])
        self.assertGreater(failed['line'], 1)
        self.assertIn('нет строки для переноса', failed['hint'])


if __name__ == '__main__':
    unittest.main()
