"""On-demand, content-addressed previews. No compiler runs during import/startup."""
from __future__ import annotations

from collections import OrderedDict
from dataclasses import dataclass
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import signal
import subprocess
import tempfile
import threading

from .files import Conflict, FileStore, confined, digest
from .inputs import ToolError, read_settings, without_comments
from .render import document_head, read_templates, review_fragment
from .storage import atomic_json

_COMPILER_LOCK = threading.Lock()

@dataclass(frozen=True)
class Snapshot:
    key: str
    path: str
    revision: str
    tex: str
    offset: int
    source_lines: int
    assets: dict[str, bytes]
    error: str | None = None


class LatexCompiler:
    def __init__(self, timeout: float = 30, font_cache: Path | None = None):
        self.timeout = timeout
        self.font_cache = font_cache

    def fingerprint(self) -> str:
        manifest = Path(os.environ.get('TEXLIVE_MANIFEST', '/opt/texlive/manifest.txt'))
        if manifest.is_file():
            return digest(manifest.read_bytes())
        executable = shutil.which('latexmk')
        return f'local:{executable}:{Path(executable).stat().st_mtime_ns if executable else "missing"}'

    def environment(self, directory: Path) -> dict[str, str]:
        env = {k: v for k, v in os.environ.items() if not k.startswith(('TEX', 'LATEX', 'BIB'))}
        font_cache = self.font_cache or directory / '.texmf-var'
        font_cache.mkdir(parents=True, exist_ok=True)
        env.update(HOME=str(directory), TEXMFOUTPUT=str(directory), TEXMFVAR=str(font_cache),
                   VARTEXFONTS=str(font_cache / 'fonts'), openin_any='p', openout_any='p')
        return env

    def __call__(self, snapshot: Snapshot, directory: Path) -> dict:
        # Full builds and previews share one TeX process slot and font cache.
        with _COMPILER_LOCK:
            return self._compile(snapshot, directory)

    def _compile(self, snapshot: Snapshot, directory: Path) -> dict:
        (directory / 'preview.tex').write_text(snapshot.tex, encoding='utf-8')
        for name, content in snapshot.assets.items():
            target = directory / name
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(content)
        command = [
            'latexmk', '-norc', '-pdf', '-interaction=nonstopmode', '-halt-on-error',
            '-file-line-error', '-pdflatex=pdflatex -no-shell-escape %O %S', 'preview.tex',
        ]
        env = self.environment(directory)
        with (directory / 'process.log').open('wb') as output:
            process = subprocess.Popen(command, cwd=directory, env=env, stdout=output, stderr=subprocess.STDOUT, start_new_session=True)
            timed_out = False
            try:
                process.wait(timeout=self.timeout)
            except subprocess.TimeoutExpired:
                timed_out = True
                os.killpg(process.pid, signal.SIGKILL)
                process.wait()
        with (directory / 'process.log').open('rb') as output:
            output.seek(max(0, output.seek(0, 2) - 512 * 1024))
            log = output.read().decode('utf-8', errors='replace')
        success = not timed_out and process.returncode == 0 and (directory / 'preview.pdf').is_file()
        match = re.search(r'(?m)^([^\n]*?\.(?:tex|sty|cls|def|fd)):(\d+):\s*([^\n]+)', log)
        line = int(match[2]) - snapshot.offset if match and Path(match[1]).name == 'preview.tex' else None
        if line is not None and not 1 <= line <= snapshot.source_lines:
            line = None
        if match:
            detail = match[3]
            if line is None:
                detail = f'{Path(match[1]).name}: {detail}'
        else:
            diagnostic = re.search(r'(?m)^(?:! (.+)|([^\n]+: (?:not found|Permission denied)))$', log)
            detail = next((value for value in diagnostic.groups() if value), '') if diagnostic else ''
        error = 'Превышено время компиляции.' if timed_out else (detail or 'Не удалось скомпилировать условие. Раскройте журнал.')
        return {'status': 'success' if success else 'error', 'error': None if success else error, 'line': line, 'log': log}


class PreviewManager:
    def __init__(self, root: Path, *, compiler=None, max_bytes: int = 1024 ** 3):
        self.root = root.resolve()
        self.files = FileStore(root)
        self.compiler = compiler or LatexCompiler(font_cache=confined(self.root, 'work/build/preview/fonts'))
        self.max_bytes = max_bytes
        self.cache = confined(self.root, 'work/build/preview/cache')
        self.scratch = confined(self.root, 'work/build/preview/tmp')
        # A server restart begins a new preview session. Keep font metrics and
        # all build/export/assignment directories outside these two folders.
        for folder in (self.cache, self.scratch):
            if folder.exists():
                shutil.rmtree(folder)
            folder.mkdir(parents=True, exist_ok=True)
        self.condition = threading.Condition(threading.RLock())
        self.pending: OrderedDict[str, Snapshot] = OrderedDict()
        self.jobs: dict[str, dict] = {}
        self._clean('')
        self.closed = False
        self.worker = threading.Thread(target=self._work, name='latex-preview', daemon=True)
        self.worker.start()

    def snapshot(self, name: str, revision: str) -> Snapshot:
        file = self.files.read(name)
        if file['revision'] != revision:
            raise Conflict('Исходник изменился. Сначала обновите файл в редакторе.')
        bank, number, variant = name.split('/')
        templates = read_templates(self.root)
        settings = read_settings(self.root)
        marker = '__TASK_BODY__'
        before, _, after = review_fragment(int(number), int(variant[:-4]), marker, show_variant=False).partition(marker)
        prefix = document_head(templates, settings, bank) + before
        tex = prefix + file['text'] + '\n' + after + templates['tail.tex']
        assets = {}
        dependencies = {}
        errors = []
        clean = without_comments(file['text'])
        if re.search(r'\\(?:input|include|usepackage|RequirePackage|documentclass)\b', clean):
            errors.append('Условие должно быть самостоятельным фрагментом без внешних input/include и пакетов.')
        for ref in re.findall(r'\\includegraphics\*?(?:\s*\[[^]]*\])?\s*\{([^}]+)\}', clean):
            try:
                if not ref.startswith('pic/'):
                    raise ToolError(f'Рисунок должен находиться в pic/: {ref}.')
                path = confined(self.root, f'TR/{bank}/{ref}')
                content = path.read_bytes()
                assets[ref] = content
                dependencies[ref] = digest(content)
            except (ToolError, OSError) as exc:
                dependencies[ref] = 'missing-or-invalid'
                errors.append(f'Не удалось прочитать рисунок {ref}: {exc}')
        payload = {'renderer': 2, 'path': name, 'tex': tex, 'dependencies': dependencies, 'environment': self.compiler.fingerprint()}
        key = hashlib.sha256(json.dumps(payload, sort_keys=True, ensure_ascii=False).encode()).hexdigest()
        return Snapshot(key, name, revision, tex, prefix.count('\n'), max(1, len(file['text'].splitlines())), assets, '\n'.join(errors) or None)

    def _cached(self, key: str) -> dict | None:
        folder = self.cache / key
        try:
            result = json.loads((folder / 'result.json').read_text(encoding='utf-8'))
            if result.get('status') not in ('success', 'error'):
                return None
            if result['status'] == 'success' and not (folder / 'preview.pdf').is_file():
                return None
            os.utime(folder, None)
            return dict(result, key=key, cached=True)
        except (OSError, ValueError):
            return None

    def request(self, name: str, revision: str) -> dict:
        snapshot = self.snapshot(name, revision)
        with self.condition:
            cached = self._cached(snapshot.key)
            if cached:
                return cached
            if snapshot.key in self.jobs and self.jobs[snapshot.key]['status'] in ('queued', 'compiling'):
                return dict(self.jobs[snapshot.key], key=snapshot.key, cached=False)
            # Only the latest waiting revision of a file is worth compiling.
            for key, previous in list(self.pending.items()):
                if previous.path == name:
                    del self.pending[key]
                    self.jobs[key] = {'status': 'superseded'}
            self.pending[snapshot.key] = snapshot
            self.jobs[snapshot.key] = {'status': 'queued'}
            self.condition.notify()
            return {'key': snapshot.key, 'status': 'queued', 'cached': False}

    def status(self, key: str) -> dict:
        if not re.fullmatch(r'[a-f0-9]{64}', key):
            raise ToolError('Неверный ключ предпросмотра.')
        with self.condition:
            result = self._cached(key) or self.jobs.get(key)
            if result is None:
                raise ToolError('Предпросмотр не найден. Откройте задание ещё раз.')
            return dict(result, key=key)

    def pdf(self, key: str) -> Path:
        with self.condition:
            result = self.status(key)
            if result['status'] != 'success':
                raise ToolError('Готового PDF для этой версии нет.')
            return self.cache / key / 'preview.pdf'

    def _clean(self, keep: str):
        folders = [p for p in self.cache.iterdir() if p.is_dir() and not p.is_symlink()]
        sizes = {p: sum(f.stat().st_size for f in p.iterdir() if f.is_file()) for p in folders}
        total = sum(sizes.values())
        for path in sorted(folders, key=lambda p: p.stat().st_mtime):
            if total <= self.max_bytes:
                break
            if path.name != keep:
                shutil.rmtree(path)
                total -= sizes[path]
                self.jobs.pop(path.name, None)
        # Also bound the in-memory status history.
        if len(self.jobs) > 512:
            for key in list(self.jobs):
                if len(self.jobs) <= 256:
                    break
                if key != keep and self.jobs[key]['status'] not in ('queued', 'compiling'):
                    del self.jobs[key]

    def _work(self):
        while True:
            with self.condition:
                self.condition.wait_for(lambda: self.closed or bool(self.pending))
                if self.closed:
                    return
                key, snapshot = self.pending.popitem(last=False)
                self.jobs[key] = {'status': 'compiling'}
            try:
                with tempfile.TemporaryDirectory(prefix='render-', dir=self.scratch) as temp:
                    directory = Path(temp)
                    if snapshot.error:
                        result = {'status': 'error', 'error': snapshot.error, 'line': None, 'log': snapshot.error}
                    else:
                        try:
                            result = self.compiler(snapshot, directory)
                        except (OSError, subprocess.SubprocessError) as exc:
                            result = {'status': 'error', 'error': f'Не удалось запустить компилятор: {exc}', 'line': None, 'log': str(exc)}
                    with self.condition:
                        destination = self.cache / key
                        destination.mkdir(exist_ok=True)
                        size = len(json.dumps(result, ensure_ascii=False).encode('utf-8'))
                        if result['status'] == 'success':
                            size += (directory / 'preview.pdf').stat().st_size
                        if size > self.max_bytes:
                            result = {'status': 'error', 'error': 'Результат превышает лимит кэша. Увеличьте PREVIEW_CACHE_MB.', 'line': None, 'log': ''}
                        if result['status'] == 'success':
                            shutil.copyfile(directory / 'preview.pdf', destination / 'preview.pdf')
                        atomic_json(destination / 'result.json', result)
                        self.jobs[key] = result
                        self._clean(key)
            except Exception as exc:
                with self.condition:
                    self.jobs[key] = {'status': 'error', 'error': f'Ошибка подготовки предпросмотра: {exc}', 'log': str(exc), 'line': None}

    def close(self):
        with self.condition:
            self.closed = True
            self.condition.notify_all()
        self.worker.join(timeout=2)
