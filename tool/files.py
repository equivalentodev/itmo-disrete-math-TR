"""Editable files. Deliberately independent of whole-bank validation."""
from __future__ import annotations

from contextlib import contextmanager
import hashlib
import json
import logging
import os
from pathlib import Path
import re
import shutil
import tempfile
import threading

from .inputs import ToolError
from .storage import atomic_json, work_lock


class Conflict(ToolError):
    pass


def digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def confined(root: Path, relative: str) -> Path:
    """Reject symlinks, traversal, absolute paths (including Windows spellings)."""
    if not relative or '\\' in relative or relative.startswith('/'):
        raise ToolError('Недопустимый путь.')
    parts = relative.split('/')
    if any(p in ('', '.', '..') or ':' in p for p in parts):
        raise ToolError('Недопустимый путь.')
    path = root
    for part in parts:
        path = path / part
        if path.is_symlink():
            raise ToolError('Символические ссылки не поддерживаются.')
    if not path.resolve().is_relative_to(root.resolve()):
        raise ToolError('Путь выходит за разрешённую папку.')
    return path


class FileStore:
    def __init__(self, root: Path):
        self.root = root.resolve()
        self.lock = threading.RLock()

    @contextmanager
    def editing(self):
        with self.lock, work_lock(self.root / 'work/build/editor', label='work/build/editor/.tool.lock'):
            yield

    def path(self, name: str) -> Path:
        if not re.fullmatch(r'TR[1-9]\d*/[1-9]\d*/[1-9]\d*\.tex', name):
            raise ToolError('Выберите файл варианта в банке TR/.')
        return confined(self.root, 'TR/' + name)

    def clear_deleted_history(self):
        """Discard undo backups from the previous server session only."""
        with self.editing():
            folder = confined(self.root, 'work/build/editor/deleted')
            if folder.exists():
                shutil.rmtree(folder)

    def read(self, name: str) -> dict:
        path = self.path(name)
        data = path.read_bytes()
        return {'path': name, 'text': data.decode('utf-8'), 'revision': digest(data)}

    def tree(self) -> list[dict]:
        banks = []
        base = confined(self.root, 'TR')
        for bank in sorted(base.iterdir(), key=lambda p: (len(p.name), p.name)):
            if bank.is_symlink() or not bank.is_dir() or not re.fullmatch(r'TR[1-9]\d*', bank.name):
                continue
            tasks = []
            for task in sorted(bank.iterdir(), key=lambda p: (len(p.name), p.name)):
                if task.is_symlink() or not task.is_dir() or not re.fullmatch(r'[1-9]\d*', task.name):
                    continue
                files = [p for p in task.glob('*.tex') if not p.is_symlink() and re.fullmatch(r'[1-9]\d*', p.stem)]
                tasks.append({'number': int(task.name), 'variants': [int(p.stem) for p in sorted(files, key=lambda p: int(p.stem))]})
            banks.append({'name': bank.name, 'tasks': tasks})
        return banks

    def save(self, name: str, text: str, revision: str) -> dict:
        if not isinstance(text, str) or len(text.encode('utf-8')) > 1024 * 1024:
            raise ToolError('Условие должно быть текстом UTF-8 размером до 1 МБ.')
        with self.editing():
            path = self.path(name)
            if digest(path.read_bytes()) != revision:
                raise Conflict('Файл изменён вне редактора. Выберите, какую версию сохранить.')
            fd, temporary = tempfile.mkstemp(prefix='.edit-', dir=path.parent)
            temp = Path(temporary)
            try:
                mode = path.stat().st_mode & 0o777
                with os.fdopen(fd, 'wb') as stream:
                    stream.write(text.encode('utf-8'))
                    stream.flush()
                    os.fsync(stream.fileno())
                os.chmod(temp, mode)
                # Recheck after preparing the replacement, including a symlink check.
                if self.path(name) != path or digest(path.read_bytes()) != revision:
                    raise Conflict('Файл изменился во время сохранения.')
                os.replace(temp, path)
            finally:
                temp.unlink(missing_ok=True)
            return {'path': name, 'revision': digest(text.encode('utf-8'))}

    def create(self, bank: str, number: int) -> dict:
        if not re.fullmatch(r'TR[1-9]\d*', bank) or type(number) is not int or number < 1:
            raise ToolError('Выберите номер задания.')
        with self.editing():
            folder = confined(self.root, f'TR/{bank}/{number}')
            if not folder.is_dir():
                raise ToolError('Не найдена папка задания.')
            files = list(folder.glob('*.tex'))
            if any(p.is_symlink() or not re.fullmatch(r'[1-9]\d*', p.stem) for p in files):
                raise ToolError('В папке есть ненумерованные файлы или символические ссылки.')
            numbers = sorted(int(p.stem) for p in files)
            if numbers != list(range(1, len(files) + 1)):
                raise ToolError('В нумерации есть пропуски. Исправьте их перед добавлением варианта.')
            name = f'{bank}/{number}/{len(files) + 1}.tex'
            text = confined(self.root, 'templates/new-task/task.tex').read_text(encoding='utf-8')
            with self.path(name).open('x', encoding='utf-8', newline='') as stream:
                stream.write(text)
            return self.read(name)

    def delete(self, name: str, revision: str) -> dict:
        with self.editing():
            path = self.path(name)
            folder = path.parent
            files = list(folder.glob('*.tex'))
            if any(p.is_symlink() or not re.fullmatch(r'[1-9]\d*', p.stem) for p in files):
                raise ToolError('В папке есть ненумерованные файлы или символические ссылки.')
            files.sort(key=lambda p: int(p.stem))
            if [int(p.stem) for p in files] != list(range(1, len(files) + 1)):
                raise ToolError('В нумерации есть пропуски. Исправьте их перед удалением варианта.')
            if not path.is_file() or digest(path.read_bytes()) != revision:
                raise Conflict('Вариант изменён или удалён вне редактора. Обновите его перед удалением.')

            number = int(path.stem)
            affected = files[number - 1:]
            backups = confined(self.root, 'work/build/editor/deleted')
            backups.mkdir(parents=True, exist_ok=True)
            backup = Path(tempfile.mkdtemp(prefix='variant-', dir=backups))
            snapshots = {}
            for source in affected:
                data = self.path(f'{name.rsplit("/", 1)[0]}/{source.name}').read_bytes()
                snapshots[source.name] = digest(data)
                target = backup / source.name
                target.write_bytes(data)
                target.chmod(source.stat().st_mode & 0o777)
            if snapshots[path.name] != revision:
                raise Conflict('Вариант изменился во время подготовки удаления.')
            after = {p.name: digest(p.read_bytes()) for p in files[:number - 1]}
            after.update({f'{int(p.stem) - 1}.tex': snapshots[p.name] for p in affected[1:]})
            atomic_json(backup / 'manifest.json', {'deleted': name, 'revision': revision,
                                                  'files': snapshots, 'after': after})
            # Validate the entire affected range before changing any of its names.
            for source in affected:
                self.path(f'{name.rsplit("/", 1)[0]}/{source.name}')
                if digest(source.read_bytes()) != snapshots[source.name]:
                    raise Conflict('Задание изменилось во время подготовки удаления. Повторите операцию.')

            moved = []
            removed = False
            try:
                path.unlink()
                removed = True
                for source in affected[1:]:
                    target = source.with_name(f'{int(source.stem) - 1}.tex')
                    os.replace(source, target)
                    moved.append((source, target))
            except OSError as exc:
                try:
                    for source, target in reversed(moved):
                        os.replace(target, source)
                    if removed:
                        shutil.copy2(backup / path.name, path)
                except OSError as rollback:
                    raise ToolError(f'Не удалось завершить удаление и восстановление. Копии: {backup.relative_to(self.root)}') from rollback
                raise ToolError('Не удалось удалить вариант. Исходные файлы восстановлены.') from exc

            remaining = len(files) - 1
            prefix = name.rsplit('/', 1)[0]
            return {'deleted': name, 'remaining': remaining, 'renamed': len(moved),
                    'next': f'{prefix}/{min(number, remaining)}.tex' if remaining else None,
                    'backup': backup.relative_to(self.root).as_posix()}

    def restore(self, name: str) -> dict:
        if not isinstance(name, str) or not re.fullmatch(r'work/build/editor/deleted/variant-[a-z0-9_]+', name):
            raise ToolError('Недопустимая запись истории удаления.')
        with self.editing():
            backup = confined(self.root, name)
            manifest = json.loads(confined(self.root, name + '/manifest.json').read_text(encoding='utf-8'))
            path = self.path(manifest['deleted'])
            if manifest.get('restored') or not isinstance(manifest.get('after'), dict):
                raise Conflict('Эта запись удаления уже восстановлена или не поддерживает отмену.')
            hashes = manifest['files']
            if not isinstance(hashes, dict):
                raise ToolError('Повреждена запись истории удаления.')
            number = int(path.stem)
            expected = [f'{i}.tex' for i in range(number, number + len(hashes))]
            if not hashes or set(hashes) != set(expected):
                raise ToolError('Повреждена запись истории удаления.')
            for filename, checksum in hashes.items():
                source = confined(self.root, name + '/' + filename)
                if digest(source.read_bytes()) != checksum:
                    raise ToolError('Повреждена копия удалённого варианта.')
            files = list(path.parent.glob('*.tex'))
            if any(p.is_symlink() or not p.is_file() or not re.fullmatch(r'[1-9]\d*', p.stem) for p in files):
                raise Conflict('Состав задания изменился вне редактора. Отмена не выполнена.')
            actual = {p.name: digest(p.read_bytes()) for p in files}
            if actual != manifest['after']:
                raise Conflict('Задание изменилось после удаления. Отмена не выполнена, чтобы сохранить эти изменения.')

            fd, temporary = tempfile.mkstemp(prefix='.restore-', dir=path.parent)
            os.close(fd)
            temp = Path(temporary)
            moved = []
            inserted = False
            try:
                shutil.copy2(backup / path.name, temp)
                for source in sorted(files, key=lambda p: int(p.stem), reverse=True):
                    if int(source.stem) < number:
                        continue
                    target = source.with_name(f'{int(source.stem) + 1}.tex')
                    os.replace(source, target)
                    moved.append((source, target))
                os.replace(temp, path)
                inserted = True
                atomic_json(backup / 'manifest.json', {**manifest, 'restored': True})
            except OSError as exc:
                try:
                    if inserted:
                        path.unlink()
                    for source, target in reversed(moved):
                        os.replace(target, source)
                except OSError as rollback:
                    raise ToolError('Не удалось завершить восстановление. Проверьте файлы задания.') from rollback
                raise ToolError('Не удалось восстановить вариант. Файлы задания не изменены.') from exc
            finally:
                temp.unlink(missing_ok=True)
            warning = None
            try:
                shutil.rmtree(backup)
            except OSError:
                logging.getLogger(__name__).exception('Не удалось удалить использованную копию %s', backup)
                warning = 'Вариант восстановлен, но не удалось очистить его временную копию.'
            return {'path': manifest['deleted'], 'warning': warning}
