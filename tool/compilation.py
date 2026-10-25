"""Compile generated documents from their prepared snapshot, never from TR/."""
from __future__ import annotations

import os
from pathlib import Path
import shutil
import tempfile
from collections.abc import Sequence

from .inputs import ToolError
from .files import confined
from .preview import LatexCompiler, Snapshot
from .render import SourceSpan


class CompilationError(ToolError):
    def __init__(self, document: str, result: dict, spans: Sequence[SourceSpan] = ()):
        self.document = document
        self.line = result.get('line')
        self.log = result.get('log', '')
        self.directory = None
        self.source = None
        self.detail = result.get('error') or 'Не удалось собрать PDF.'
        self.hint = None
        if "There's no line here to end" in self.detail:
            self.hint = ('Команда \\\\ используется там, где нет строки для переноса. '
                         'Проверьте её после списка, формулы или пустой строки; '
                         'для нового абзаца используйте пустую строку или \\par.')
        if self.line is not None:
            # Prefer task text if a template shares its final generated line.
            candidates = [s for s in spans if s.start <= self.line <= s.end]
            candidates.sort(key=lambda s: not s.path.startswith('TR/'))
            if candidates:
                span = candidates[0]
                local_line = self.line - span.start + 1
                lines = span.text.splitlines()
                self.source = {
                    'path': span.path, 'line': local_line,
                    'context': [{'line': i + 1, 'text': lines[i]}
                                for i in range(max(0, local_line - 4), min(len(lines), local_line + 2))],
                }
        location = f', строка {self.line}' if self.line else ''
        if self.source:
            location = f'{self.source["path"]}, строка {self.source["line"]}'
            super().__init__(f'{location}: {self.detail}')
        else:
            super().__init__(f'{document}{location}: {self.detail}')


def compile_documents(root: Path, project: Path, documents: dict[str, str], output: Path,
                      logs: Path, *, compiler=None, progress=None, source_maps=None) -> str:
    if compiler is None:
        if shutil.which('latexmk') is None:
            raise ToolError('Для PDF нужен контейнер web с TeX Live. Запустите docker compose up --build.')
        timeout = int(os.environ.get('BUILD_TIMEOUT_SECONDS', '600'))
        if not 1 <= timeout <= 7200:
            raise ToolError('BUILD_TIMEOUT_SECONDS должен быть от 1 до 7200 секунд на документ.')
        compiler = LatexCompiler(timeout, font_cache=confined(root, 'work/build/preview/fonts'))
    environment = compiler.fingerprint()
    output.mkdir()
    logs.mkdir()
    assets = {p.relative_to(project).as_posix(): p.read_bytes()
              for p in project.rglob('*') if p.is_file() and p.relative_to(project).parts[0] == 'pic'}
    for index, (name, text) in enumerate(documents.items(), 1):
        spans = (source_maps or {}).get(name, ())
        if progress:
            progress(f'Компиляция {name}: документ {index} из {len(documents)}…')
        snapshot = Snapshot('', name, '', text, 0, max(1, len(text.splitlines())), assets)
        with tempfile.TemporaryDirectory(prefix='.tex-', dir=project.parent) as temporary:
            directory = Path(temporary)
            try:
                result = compiler(snapshot, directory)
            except OSError as exc:
                result = {'status':'error', 'error':str(exc), 'log':str(exc)}
            (logs / Path(name).with_suffix('.log')).write_text(result.get('log', ''), encoding='utf-8')
            pdf = directory / 'preview.pdf'
            if result.get('status') != 'success':
                raise CompilationError(name, result, spans)
            data = pdf.read_bytes() if pdf.is_file() else b''
            if not data.startswith(b'%PDF-') or not data.rstrip().endswith(b'%%EOF'):
                raise CompilationError(name, {**result, 'error':'Компилятор не создал полный PDF.'}, spans)
            (output / Path(name).with_suffix('.pdf')).write_bytes(data)
    return environment
