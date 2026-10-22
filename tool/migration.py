"""Однократный перенос прежней рабочей папки ТР1; не пункт обычного меню."""
from __future__ import annotations

import csv
import json
from pathlib import Path
import shutil
import tempfile

from .inputs import ToolError, read_bank, read_settings, read_students
from .storage import atomic_json, reserve_name, sha256, timestamp, work_lock, write_json


def migrate(root: Path) -> dict:
    root = root.resolve()
    legacy = root / 'work/TR1'
    settings = read_settings(root)
    state_path = root / 'work/assignments' / settings.period / 'TR1/current.json'
    if not legacy.exists():
        if state_path.is_file():
            return {'already_migrated': True}
        raise ToolError('Прежняя work/TR1 не найдена. Нечего переносить.')
    with work_lock(root / 'work'):
        if state_path.exists():
            raise ToolError('Назначения TR1 уже существуют; автоматическое перезаписывание миграцией запрещено.')
        bank = read_bank(root, 'TR1')
        source_tasks = {p.relative_to(legacy / 'tasks').as_posix(): sha256(p) for p in (legacy / 'tasks').glob('*/*.tex') if p.parent.name.isdigit() and p.stem.isdigit()}
        target_tasks = {p.relative_to(bank.path).as_posix(): sha256(p) for p in bank.path.glob('*/*.tex') if p.parent.name.isdigit() and p.stem.isdigit()}
        if source_tasks != target_tasks:
            raise ToolError('TR/TR1 и work/TR1/tasks различаются. Миграция остановлена; все файлы сохранены.')
        for image in bank.images:
            if (bank.path / image).read_bytes() != (legacy / image).read_bytes():
                raise ToolError(f'Рабочий рисунок {image} отличается от TR/TR1. Миграция остановлена.')
        students = read_students(legacy / 'students.csv')
        by_id = {s.isu_id: s for s in students}
        with (legacy / 'latex/assignments.csv').open(encoding='utf-8-sig', newline='') as file:
            rows = list(csv.reader(file))
        if len(rows) < 4 or rows[0][:1] != ['Seed']:
            raise ToolError('Не распознан старый формат assignments.csv.')
        records = {}
        for row in rows[3:]:
            if len(row) != 4 + len(bank.tasks) or row[0] not in by_id or row[0] in records:
                raise ToolError('Старые назначения не соответствуют списку студентов.')
            s = by_id[row[0]]
            if row[1:4] != [s.name, s.flow, s.group]:
                raise ToolError(f'Старые назначения: не совпали данные ИСУ {s.isu_id}.')
            variants = tuple(map(int, row[4:]))
            if not all(1 <= v <= k for v, k in zip(variants, bank.counts)):
                raise ToolError(f'Старые назначения: неверные номера ИСУ {s.isu_id}.')
            records[s.isu_id] = {'name': s.name, 'flow': s.flow, 'group': s.group, 'variants': {str(i): v for i, v in enumerate(variants, 1)}}
        if set(records) != set(by_id):
            raise ToolError('В старых назначениях присутствуют не все студенты.')
        combinations = {tuple(rec['variants'].values()) for rec in records.values()}
        if len(combinations) != len(records):
            raise ToolError('В старой выдаче повторяются полные комбинации. Автоматический импорт остановлен.')
        state = {
            'schema_version': 1, 'period': settings.period, 'bank': 'TR1', 'algorithm': 'legacy-random-v1',
            'seed': int(rows[0][1]), 'records': records, 'updated_utc': timestamp(),
        }
        exports = root / 'work/exports/TR1'
        exports.mkdir(parents=True, exist_ok=True)
        version = reserve_name(exports, 'TR1-legacy-' + timestamp())
        destination = exports / version
        destination.mkdir()
        csv_target = root / 'work/students/students-2026.csv'
        build_target = root / 'work/build/legacy-TR1'
        if csv_target.exists() or build_target.exists():
            destination.rmdir()
            raise ToolError('Папки назначения миграции уже заняты; данные не перезаписаны.')
        try:
            shutil.copy2(legacy / 'tr1_overleaf.zip', destination / 'tr1_overleaf.zip')
            shutil.copy2(legacy / 'latex/assignments.csv', destination / 'assignments-original.csv')
            write_json(destination / 'assignments.json', state)
            write_json(destination / 'manifest.json', {
                'schema_version': 1, 'kind': 'legacy', 'created_utc': timestamp(), 'bank': 'TR1',
                'students': len(students), 'archive_sha256': sha256(destination / 'tr1_overleaf.zip'),
                'note': 'Сохранённая выдача сентября 2026 года. Назначения импортированы без выравнивания.',
            })
            csv_target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(legacy / 'students.csv', csv_target)
            build_target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copytree(legacy / 'latex', build_target)
            assert sha256(csv_target) == sha256(legacy / 'students.csv')
            assert sha256(destination / 'tr1_overleaf.zip') == sha256(legacy / 'tr1_overleaf.zip')
            for source in (legacy / 'latex').rglob('*'):
                if source.is_file():
                    assert sha256(source) == sha256(build_target / source.relative_to(legacy / 'latex'))
            assert json.loads((destination / 'assignments.json').read_text(encoding='utf-8')) == state
            atomic_json(state_path, state)
        except BaseException:
            shutil.rmtree(destination)
            if csv_target.exists():
                csv_target.unlink()
            if build_target.exists():
                shutil.rmtree(build_target)
            raise
        # Старые копии проверены; вне проекта оставлена временная страховочная копия.
        recovery = Path(tempfile.mkdtemp(prefix='dm-legacy-TR1-'))
        shutil.move(str(legacy), str(recovery / 'TR1'))
        return {'students': len(students), 'state': str(state_path), 'legacy_export': str(destination), 'temporary_recovery': str(recovery)}


if __name__ == '__main__':
    try:
        print(json.dumps(migrate(Path.cwd()), ensure_ascii=False, indent=2))
    except (ToolError, OSError, ValueError) as exc:
        raise SystemExit(f'Миграция не выполнена: {exc}')
