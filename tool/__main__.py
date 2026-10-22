from __future__ import annotations

import argparse
from collections import Counter
from pathlib import Path
import sys

from .inputs import ToolError, bank_names, read_students, select_students
from .service import run


def choose(title: str, options: list[str], *, multiple: bool = False) -> list[str]:
    if not options:
        raise ToolError(f'{title}: нет доступных значений.')
    while True:
        print('\n' + title)
        for i, option in enumerate(options, 1):
            print(f'  {i}. {option}')
        print('  0. Назад')
        answer = input('Номера через запятую: ' if multiple else 'Номер: ').strip()
        if answer == '0':
            return []
        try:
            indices = [int(i.strip()) - 1 for i in answer.split(',')]
            if not multiple and len(indices) != 1:
                raise ValueError
            if any(i < 0 or i >= len(options) for i in indices):
                raise ValueError
            return [options[i] for i in dict.fromkeys(indices)]
        except ValueError:
            print('Введите номер из списка.' if not multiple else 'Введите номера из списка через запятую.')


def print_result(result) -> None:
    def display(path: Path) -> Path:
        try:
            return path.relative_to(Path.cwd())
        except ValueError:
            return path

    print('\nГотово.')
    if result.archive:
        print(f'Загрузите этот ZIP в Overleaf:\n  {display(result.archive)}')
    else:
        print(f'Новое распределение сохранено:\n  {display(result.directory)}')
        print('Чтобы получить ZIP, выберите «Собрать работы для студентов».')
    if result.students:
        print(f'Студентов: {result.students}. Потоки: ' + ', '.join(f'{f} — {n}' for f, n in result.flows.items()))
    for change in result.changes:
        print('  ' + change)


def menu(root: Path) -> None:
    (root / 'work/students').mkdir(parents=True, exist_ok=True)
    while True:
        print('\nТиповые расчёты\n'
              '  1. Подготовить типовик для проверки в Overleaf\n'
              '  2. Собрать работы для студентов\n'
              '  3. Создать новое распределение\n'
              '  0. Выход')
        action = input('Действие: ').strip()
        if action == '0':
            return
        if action not in ('1', '2', '3'):
            print('Введите 0, 1, 2 или 3.')
            continue
        try:
            bank = choose('Выберите типовик', bank_names(root))
            if not bank:
                continue
            kwargs = {'kind': {'1': 'review', '2': 'students', '3': 'redistribute'}[action]}
            if action != '1':
                files = sorted((root / 'work/students').glob('*.csv'))
                if not files:
                    raise ToolError('Поместите CSV студентов в work/students/ и повторите действие.')
                names = choose('Выберите CSV студентов', [p.name for p in files])
                if not names:
                    continue
                path = root / 'work/students' / names[0]
                students = read_students(path)
                selection = choose('Кого включить?', ['Все студенты', 'Выбрать потоки', 'Выбрать группы'])
                if not selection:
                    continue
                kind = {'Все студенты': 'all', 'Выбрать потоки': 'flows', 'Выбрать группы': 'groups'}[selection[0]]
                values = []
                if kind != 'all':
                    values = choose(
                        'Выберите значения', sorted({s.flow if kind == 'flows' else s.group for s in students}), multiple=True
                    )
                    if not values:
                        continue
                chosen = select_students(students, kind, tuple(values))
                print(f'Выбрано студентов: {len(chosen)}. По потокам: {dict(sorted(Counter(s.flow for s in chosen).items()))}')
                if action == '3':
                    if input('Заменить назначения выбранных студентов новым распределением? [да/нет]: ').strip().lower() != 'да':
                        continue
                kwargs.update(csv_path=path, selection=kind, values=tuple(values))
            print_result(run(root, bank[0], **kwargs))
        except (ToolError, OSError, UnicodeError) as exc:
            print(f'\nНе выполнено: {exc}')


def main() -> int:
    parser = argparse.ArgumentParser(description='Типовые расчёты: подготовка ZIP для Overleaf')
    parser.add_argument('--root', type=Path, default=Path.cwd(), help='Корень проекта (по умолчанию текущая папка)')
    sub = parser.add_subparsers(dest='action')
    for action in ('review', 'students', 'redistribute'):
        command = sub.add_parser(action)
        command.add_argument('bank', help='Например, TR2')
        if action != 'review':
            command.add_argument('--csv', type=Path, required=True)
            selection = command.add_mutually_exclusive_group()
            selection.add_argument('--flows', nargs='+')
            selection.add_argument('--groups', nargs='+')
            command.add_argument('--seed', type=int)
    args = parser.parse_args()
    try:
        if not args.action:
            if not sys.stdin.isatty():
                raise ToolError('Для меню нужен интерактивный терминал. Запустите docker compose run --rm utility.')
            menu(args.root.resolve())
        else:
            kwargs = {'kind': args.action}
            if args.action != 'review':
                kind = 'flows' if args.flows else 'groups' if args.groups else 'all'
                kwargs.update(csv_path=args.csv, selection=kind, values=tuple(args.flows or args.groups or ()), seed=args.seed)
            print_result(run(args.root, args.bank, **kwargs))
    except (ToolError, OSError, UnicodeError) as exc:
        print(f'Не выполнено: {exc}', file=sys.stderr)
        return 1
    except (KeyboardInterrupt, EOFError):
        print('\nОперация отменена.', file=sys.stderr)
        return 130
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
