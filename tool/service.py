from __future__ import annotations

from collections import Counter
import csv
from dataclasses import asdict, dataclass
from pathlib import Path
import secrets
import shutil
import subprocess
import tempfile
import zipfile

from . import VERSION
from .distribution import ALGORITHM, reconcile
from .inputs import ToolError, read_bank, read_settings, read_students, select_students
from .render import read_templates, render_documents
from .storage import atomic_json, read_state, reserve_name, sha256, timestamp, work_lock, write_json


@dataclass(frozen=True)
class Result:
    directory: Path
    archive: Path | None
    students: int
    flows: dict[str, int]
    changes: tuple[str, ...]
    pdfs: tuple[Path, ...] = ()
    pdf_archive: Path | None = None


def _verify_project(project: Path, documents: dict[str, str], bank, students) -> None:
    expected = set(documents) | set(bank.images) | {'README.txt'}
    actual = {p.relative_to(project).as_posix() for p in project.rglob('*') if p.is_file()}
    if actual != expected:
        raise ToolError('Проверка проекта: состав файлов отличается от ожидаемого.')
    for name, text in documents.items():
        if (project / name).read_text(encoding='utf-8') != text:
            raise ToolError(f'Проверка проекта: изменился файл {name}.')
        if text.count('\\begin{document}') != 1 or text.count('\\end{document}') != 1:
            raise ToolError(f'Проверка проекта: неверная структура документа {name}.')
        if students is None:
            count = sum(bank.counts)
        else:
            flow = name[len('flow_'):-len('.tex')]
            count = sum(s.flow == flow for s in students) * len(bank.tasks)
        # Исходные фрагменты уже проверены, поэтому здесь считаем команды в результате.
        import re
        from .inputs import without_comments
        if len(re.findall(r'\\question\b', without_comments(text))) != count:
            raise ToolError(f'Проверка проекта: неверное число заданий в {name}.')
    for image in bank.images:
        if (project / image).read_bytes() != (bank.path / image).read_bytes():
            raise ToolError(f'Проверка проекта: рисунок {image} отличается от банка.')


def verify_archive(path: Path, project: Path) -> None:
    expected = {p.relative_to(project).as_posix() for p in project.rglob('*') if p.is_file()}
    with zipfile.ZipFile(path) as archive:
        names = archive.namelist()
        if len(names) != len(set(names)) or set(names) != expected or archive.testzip() is not None:
            raise ToolError('Проверка ZIP: неверный состав или повреждённые данные.')
        for name in names:
            if archive.read(name) != (project / name).read_bytes():
                raise ToolError(f'Проверка ZIP: файл {name} не совпал с подготовленным проектом.')


def _git_info(root: Path) -> dict:
    try:
        commit = subprocess.run(['git', '-C', str(root), 'rev-parse', 'HEAD'], capture_output=True, text=True, timeout=3)
        if commit.returncode:
            return {'commit': None, 'dirty': None}
        status = subprocess.run(['git', '-C', str(root), 'status', '--porcelain'], capture_output=True, text=True, timeout=3)
        return {'commit': commit.stdout.strip(), 'dirty': bool(status.stdout) if status.returncode == 0 else None}
    except (OSError, subprocess.TimeoutExpired):
        return {'commit': None, 'dirty': None}


def _assignments_csv(path: Path, records: dict, students, count: int) -> None:
    with path.open('w', encoding='utf-8-sig', newline='') as file:
        writer = csv.writer(file)
        writer.writerow(['ИСУ ID', 'ФИО', 'Поток', 'Группа', *[f'Задание {i}' for i in range(1, count + 1)]])
        for s in students:
            writer.writerow([s.isu_id, s.name, s.flow, s.group, *[records[s.isu_id]['variants'][str(i)] for i in range(1, count + 1)]])


def run(
    root: Path, bank_name: str, *, kind: str = 'review', csv_path: Path | None = None,
    selection: str = 'all', values: tuple[str, ...] = (), seed: int | None = None,
    pdf: bool = False, compiler=None, progress=None,
) -> Result:
    """kind: review / students / redistribute. Не обращается к терминальному вводу."""
    root = root.resolve()
    if kind not in ('review', 'students', 'redistribute'):
        raise ToolError(f'Неизвестное действие: {kind}.')
    if type(pdf) is not bool or (pdf and kind == 'redistribute'):
        raise ToolError('PDF можно собрать только для обзора или именных работ.')
    if progress:
        progress('Подготовка исходников…')
    settings = read_settings(root)
    bank = read_bank(root, bank_name)
    templates = read_templates(root)
    source_files = [p for p in bank.path.glob('*/*.tex') if p.parent.name.isdigit() and p.stem.isdigit()]
    task_directories = {p.name for p in bank.path.iterdir() if p.is_dir() and p.name.isdigit()}
    task_files = {p.relative_to(bank.path).as_posix() for p in source_files}
    expected_files = {f'{n}/{k}.tex' for n, variants in enumerate(bank.tasks, 1)
                      for k in range(1, len(variants) + 1)}
    if task_files != expected_files or task_directories != {str(n) for n in range(1, len(bank.tasks) + 1)}:
        raise ToolError('Состав заданий изменился во время чтения. Повторите сборку.')
    source_files += [bank.path / image for image in bank.images]
    source_files += [root / 'templates' / name for name in templates]
    source_files += [root / 'config/project.toml']
    source_hashes = {p.relative_to(root).as_posix(): sha256(p) for p in source_files}
    if read_templates(root) != templates or read_settings(root) != settings:
        raise ToolError('Шаблоны или настройки изменились во время чтения. Повторите сборку.')
    # Проверить, что прочитанный банк соответствует снимку файлов, а не устарел во время чтения.
    for n, variants in enumerate(bank.tasks, 1):
        for k, text in enumerate(variants, 1):
            raw = (bank.path / str(n) / f'{k}.tex').read_text(encoding='utf-8')
            if (raw if raw.endswith('\n') else raw + '\n') != text:
                raise ToolError('Условия изменились во время чтения. Повторите сборку.')
    students = None
    csv_digest = None
    if kind != 'review':
        if csv_path is None:
            raise ToolError('Для именных работ нужен CSV студентов.')
        csv_path = csv_path.resolve()
        csv_digest = sha256(csv_path)
        students = select_students(read_students(csv_path), selection, values)
    work = root / 'work'
    with work_lock(work):
        state_path = work / 'assignments' / settings.period / bank.name / 'current.json'
        old = read_state(state_path, settings.period, bank.name) if students is not None else None
        current_seed = seed if seed is not None else (
            secrets.randbits(63) if kind == 'redistribute' else (old['seed'] if old else settings.seed)
        )
        changes = []
        state = None
        strict = False
        if students is not None:
            records, changes, strict = reconcile(
                students, bank.counts, current_seed, old['records'] if old else {}, fresh=kind == 'redistribute'
            )
            state = {
                'schema_version': 1, 'period': settings.period, 'bank': bank.name,
                'algorithm': ALGORITHM, 'seed': current_seed, 'records': records,
                'updated_utc': timestamp(),
            }
        export_parent = work / 'exports' / bank.name
        export_parent.mkdir(parents=True, exist_ok=True)
        version = reserve_name(export_parent, f'{bank.name}-{kind}-{timestamp()}')
        build_parent = work / 'build'
        build_parent.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(prefix='.prepare-', dir=build_parent) as temp_name:
            temp = Path(temp_name)
            release = temp / 'release'
            release.mkdir()
            project = temp / 'project'
            project.mkdir()
            documents = {}
            archive_name = None
            pdf_names = ()
            pdf_archive_name = None
            compiler_environment = None
            if kind != 'redistribute':
                source_maps = {}
                documents = render_documents(templates, settings, bank, students, state['records'] if state else None,
                                             source_maps=source_maps)
                for name, text in documents.items():
                    (project / name).write_text(text, encoding='utf-8')
                for image in bank.images:
                    target = project / image
                    target.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copy2(bank.path / image, target)
                readme = (
                    f'Типовой расчёт {bank.name[2:]}\n{settings.term}\n\n'
                    'Загрузите ZIP в Overleaf через New Project → Upload Project.\n'
                    f'Главный файл: {", ".join(sorted(documents))}.\n'
                    'Для каждого потока по очереди выбирайте соответствующий главный файл.\n'
                    if students is not None else
                    f'Типовой расчёт {bank.name[2:]}: все варианты для проверки.\n\n'
                    'Загрузите ZIP в Overleaf через New Project → Upload Project.\nГлавный файл: dump.tex.\n'
                )
                (project / 'README.txt').write_text(readme, encoding='utf-8')
                _verify_project(project, documents, bank, students)
                archive_name = version + '.zip'
                with zipfile.ZipFile(release / archive_name, 'w', compression=zipfile.ZIP_DEFLATED) as archive:
                    for file in sorted(project.rglob('*')):
                        if file.is_file():
                            archive.write(file, file.relative_to(project).as_posix())
                verify_archive(release / archive_name, project)
                if pdf:
                    from .compilation import CompilationError, compile_documents
                    try:
                        compiler_environment = compile_documents(
                            root, project, documents, release / 'pdf', release / 'logs',
                            compiler=compiler, progress=progress, source_maps=source_maps,
                        )
                    except CompilationError as exc:
                        failure_parent = build_parent / bank.name
                        failure_parent.mkdir(parents=True, exist_ok=True)
                        failure = failure_parent / reserve_name(failure_parent, version + '-failed')
                        failure.mkdir()
                        project.rename(failure / 'sources')
                        (release / 'logs').rename(failure / 'logs')
                        write_json(failure / 'error.json', {
                            'document': exc.document, 'line': exc.line, 'source': exc.source,
                            'error': str(exc), 'detail': exc.detail, 'hint': exc.hint,
                        })
                        exc.directory = failure
                        raise
                    pdf_names = tuple(f'pdf/{Path(name).with_suffix(".pdf")}' for name in documents)
                    pdf_archive_name = version + '-pdf.zip'
                    with zipfile.ZipFile(release / pdf_archive_name, 'w', compression=zipfile.ZIP_DEFLATED) as archive:
                        for name in pdf_names:
                            archive.write(release / name, Path(name).name)
                    verify_archive(release / pdf_archive_name, release / 'pdf')
            flows = dict(sorted(Counter(s.flow for s in students or []).items()))
            if state:
                write_json(release / 'assignments.json', state)
                _assignments_csv(release / 'assignments.csv', state['records'], students, len(bank.tasks))
            current_directories = {p.name for p in bank.path.iterdir() if p.is_dir() and p.name.isdigit()}
            current_files = {p.relative_to(bank.path).as_posix() for p in bank.path.glob('*/*.tex')
                             if p.parent.name.isdigit() and p.stem.isdigit()}
            if current_directories != task_directories or current_files != task_files or any(sha256(root / name) != value for name, value in source_hashes.items()):
                raise ToolError('Исходники изменились во время сборки. Архив не опубликован; повторите действие.')
            if students is not None and sha256(csv_path) != csv_digest:
                raise ToolError('CSV студентов изменился во время сборки. Повторите действие.')
            manifest = {
                'schema_version': 1, 'tool_version': VERSION, 'version': version,
                'created_utc': timestamp(), 'kind': kind, 'bank': bank.name, 'settings': asdict(settings),
                'selection': {'kind': selection, 'values': list(values)} if students is not None else None,
                'students': len(students or []), 'flows': flows, 'variant_counts': list(bank.counts),
                'strict_balance': strict, 'seed': current_seed if state else None,
                'input_sha256': dict(sorted(source_hashes.items())),
                'csv_sha256': csv_digest,
                'archive_sha256': sha256(release / archive_name) if archive_name else None,
                'pdf_sha256': {name: sha256(release / name) for name in pdf_names},
                'pdf_archive_sha256': sha256(release / pdf_archive_name) if pdf_archive_name else None,
                'compiler_environment': compiler_environment,
                'changes': changes, 'git': _git_info(root),
            }
            write_json(release / 'manifest.json', manifest)
            final = export_parent / version
            # Сначала неизменяемая готовая версия. Сбой записи current откатывает только её.
            release.rename(final)
            try:
                if state:
                    atomic_json(state_path, state)
            except BaseException:
                shutil.rmtree(final)
                raise
            # Промежуточный проект остаётся отдельно от выдаваемого архива.
            if documents:
                preliminary = build_parent / bank.name / version
                preliminary.parent.mkdir(parents=True, exist_ok=True)
                try:
                    project.rename(preliminary)
                except OSError:
                    changes.append('Промежуточная папка не сохранена; готовый ZIP и назначения сохранены.')
            return Result(final, final / archive_name if archive_name else None, len(students or []), flows,
                          tuple(changes), tuple(final / name for name in pdf_names),
                          final / pdf_archive_name if pdf_archive_name else None)
