from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import tempfile

from .inputs import ToolError


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def timestamp() -> str:
    return datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')


def write_json(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')


def atomic_json(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temp_name = tempfile.mkstemp(prefix='.state-', dir=path.parent)
    temp = Path(temp_name)
    try:
        with os.fdopen(fd, 'w', encoding='utf-8') as file:
            json.dump(data, file, ensure_ascii=False, indent=2)
            file.write('\n')
            file.flush()
            os.fsync(file.fileno())
        os.replace(temp, path)
    finally:
        temp.unlink(missing_ok=True)


@contextmanager
def work_lock(work: Path):
    work.mkdir(parents=True, exist_ok=True)
    path = work / '.tool.lock'
    try:
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError as exc:
        raise ToolError(
            'Другая операция уже использует work/.tool.lock. '
            'Дождитесь её завершения. Если прошлый запуск аварийно оборвался, '
            'убедитесь, что он не работает, и удалите этот файл блокировки.'
        ) from exc
    try:
        with os.fdopen(fd, 'w', encoding='utf-8') as file:
            json.dump({'pid': os.getpid(), 'started': timestamp()}, file)
        yield
    finally:
        path.unlink(missing_ok=True)


def read_state(path: Path, period: str, bank: str) -> dict | None:
    if not path.exists():
        return None
    try:
        data = json.loads(path.read_text(encoding='utf-8'))
        assert data['schema_version'] == 1
        assert data['period'] == period and data['bank'] == bank
        assert type(data['seed']) is int
        assert isinstance(data['records'], dict)
        for sid, rec in data['records'].items():
            assert isinstance(sid, str) and sid
            assert all(isinstance(rec[k], str) for k in ('name', 'flow', 'group'))
            assert isinstance(rec['variants'], dict)
            assert all(isinstance(k, str) and k.isdigit() and type(v) is int and v > 0 for k, v in rec['variants'].items())
        return data
    except (ValueError, KeyError, TypeError, AssertionError) as exc:
        raise ToolError(f'Повреждён или несовместим файл назначений {path}. Он не был перезаписан.') from exc


def reserve_name(parent: Path, prefix: str) -> str:
    name = prefix
    suffix = 1
    while (parent / name).exists():
        suffix += 1
        name = f'{prefix}-{suffix}'
    return name
