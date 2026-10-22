from __future__ import annotations

import csv
import re
import tomllib
from dataclasses import dataclass
from pathlib import Path


class ToolError(Exception):
    """Ошибка, которую можно показать пользователю без traceback."""


@dataclass(frozen=True)
class Settings:
    course: str
    term: str
    period: str
    seed: int


@dataclass(frozen=True)
class Student:
    isu_id: str
    name: str
    flow: str
    group: str


@dataclass(frozen=True)
class Bank:
    name: str
    path: Path
    tasks: tuple[tuple[str, ...], ...]
    images: tuple[str, ...]

    @property
    def counts(self) -> tuple[int, ...]:
        return tuple(map(len, self.tasks))


def read_settings(root: Path) -> Settings:
    path = root / 'config/project.toml'
    try:
        data = tomllib.loads(path.read_text(encoding='utf-8'))
        result = Settings(data['course'], data['term'], data['period'], data['seed'])
    except (OSError, ValueError, KeyError, TypeError) as exc:
        raise ToolError(f'Не удалось прочитать {path}: {exc}') from exc
    if not all(isinstance(v, str) and v.strip() for v in (result.course, result.term, result.period)):
        raise ToolError('В config/project.toml нужны непустые course, term и period.')
    if not re.fullmatch(r'[A-Za-z0-9_-]+', result.period):
        raise ToolError('period должен состоять из латинских букв, цифр, _ и - (например, 2026-autumn).')
    if type(result.seed) is not int:
        raise ToolError('seed в config/project.toml должен быть целым числом.')
    return result


def bank_names(root: Path) -> list[str]:
    return sorted(
        [p.name for p in (root / 'TR').iterdir() if p.is_dir() and re.fullmatch(r'TR[1-9]\d*', p.name)],
        key=lambda name: int(name[2:]),
    )


def without_comments(text: str) -> str:
    return '\n'.join(_uncomment(line) for line in text.splitlines())


def _uncomment(line: str) -> str:
    for i, char in enumerate(line):
        if char == '%':
            slashes = len(line[:i]) - len(line[:i].rstrip('\\'))
            if slashes % 2 == 0:
                return line[:i]
    return line


def check_fragment(text: str, label: str) -> None:
    clean = without_comments(text)
    if not clean.strip() or not clean.lstrip().startswith('\\question'):
        raise ToolError(f'{label}: условие должно начинаться с \\question.')
    if len(re.findall(r'\\question\b', clean)) != 1:
        raise ToolError(f'{label}: требуется ровно одна команда \\question.')
    stack: list[str] = []
    for kind, env in re.findall(r'\\(begin|end)\{([^}]+)\}', clean):
        if kind == 'begin':
            stack.append(env)
        elif not stack or stack.pop() != env:
            raise ToolError(f'{label}: неверно закрыто окружение {env}.')
    if stack:
        raise ToolError(f'{label}: не закрыто окружение {stack[-1]}.')
    depth = 0
    dollars = 0
    for i, char in enumerate(clean):
        slashes = len(clean[:i]) - len(clean[:i].rstrip('\\')) if char in '{}$' else 0
        if slashes % 2:
            continue
        if char == '{':
            depth += 1
        elif char == '}':
            depth -= 1
            if depth < 0:
                raise ToolError(f'{label}: лишняя закрывающая фигурная скобка.')
        elif char == '$':
            dollars += 1
    if depth or dollars % 2:
        raise ToolError(f'{label}: не сбалансированы фигурные скобки или разделители $.')


def _consecutive(paths: list[Path], label: str, stems: bool = False) -> list[Path]:
    paths = sorted(paths, key=lambda p: int(p.stem if stems else p.name))
    names = [p.stem if stems else p.name for p in paths]
    if not paths or names != [str(i) for i in range(1, len(paths) + 1)]:
        raise ToolError(f'{label}: номера должны идти подряд от 1; найдены {", ".join(names) or "нет файлов"}.')
    return paths


def read_bank(root: Path, name: str) -> Bank:
    if not re.fullmatch(r'TR[1-9]\d*', name):
        raise ToolError(f'Неверное имя типовика: {name}.')
    path = (root / 'TR' / name).resolve()
    if not path.is_dir() or not path.is_relative_to((root / 'TR').resolve()):
        raise ToolError(f'Не найден банк {name}.')
    folders = _consecutive([p for p in path.iterdir() if p.is_dir() and p.name.isdigit()], name)
    tasks = []
    images = set()
    for folder in folders:
        files = _consecutive(
            [p for p in folder.glob('*.tex') if p.stem.isdigit()], f'{name}/{folder.name}', True
        )
        variants = []
        for file in files:
            label = f'{name}/{folder.name}/{file.name}'
            if not file.resolve().is_relative_to(path):
                raise ToolError(f'{label}: файл находится за пределами банка.')
            text = file.read_text(encoding='utf-8')
            check_fragment(text, label)
            clean = without_comments(text)
            if re.search(r'\\(?:input|include)\b', clean):
                raise ToolError(f'{label}: внешние \\input/\\include в условии не поддерживаются; нужен самостоятельный фрагмент.')
            refs = re.findall(r'\\includegraphics\*?(?:\s*\[[^]]*\])?\s*\{([^}]+)\}', clean)
            for ref in refs:
                relative = Path(ref)
                image = (path / relative).resolve()
                if relative.is_absolute() or not image.is_relative_to(path / 'pic'):
                    raise ToolError(f'{label}: рисунок должен находиться в pic/ этого банка: {ref}.')
                if not image.is_file():
                    raise ToolError(f'{label}: отсутствует рисунок {ref}. Условие не изменялось.')
                images.add(relative.as_posix())
            variants.append(text if text.endswith('\n') else text + '\n')
        tasks.append(tuple(variants))
    return Bank(name, path, tuple(tasks), tuple(sorted(images)))


def read_students(path: Path) -> list[Student]:
    try:
        with path.open(encoding='utf-8-sig', newline='') as file:
            reader = csv.DictReader(file)
            columns = ('ИСУ ID', 'ФИО', 'Поток', 'Группа')
            missing = set(columns) - set(reader.fieldnames or [])
            if missing:
                raise ToolError(f'{path.name}: отсутствуют колонки {", ".join(sorted(missing))}.')
            result = []
            seen = set()
            for row in reader:
                if not any(value for value in row.values()):
                    continue
                values = [(row.get(c) or '').strip() for c in columns]
                if not all(values):
                    raise ToolError(f'{path.name}, строка {reader.line_num}: пустое обязательное поле.')
                if values[0] in seen:
                    raise ToolError(f'{path.name}, строка {reader.line_num}: повтор ИСУ ID {values[0]}.')
                if not re.fullmatch(r'[\w-]+', values[2]):
                    raise ToolError(f'{path.name}, строка {reader.line_num}: недопустимое имя потока {values[2]}.')
                seen.add(values[0])
                result.append(Student(*values))
    except (OSError, UnicodeError, csv.Error) as exc:
        raise ToolError(f'Не удалось прочитать список {path}: {exc}') from exc
    if not result:
        raise ToolError(f'{path.name}: список студентов пуст.')
    return sorted(result, key=lambda s: s.isu_id)


def select_students(students: list[Student], kind: str = 'all', values: tuple[str, ...] = ()) -> list[Student]:
    if kind not in ('all', 'flows', 'groups'):
        raise ToolError(f'Неизвестный способ отбора: {kind}.')
    result = [s for s in students if kind == 'all' or (s.flow if kind == 'flows' else s.group) in values]
    if not result:
        raise ToolError('Не выбрано ни одного студента.')
    return result
