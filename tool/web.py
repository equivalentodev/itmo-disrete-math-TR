"""Local web adapter. Generation rules remain in service.run."""
from __future__ import annotations

import argparse
from concurrent.futures import ThreadPoolExecutor
import hmac
import os
from pathlib import Path
import re
import secrets
import tempfile
import threading

from flask import Flask, jsonify, request, send_file, session
from werkzeug.exceptions import HTTPException

from .files import Conflict, FileStore, confined
from .compilation import CompilationError
from .inputs import ToolError, read_settings, read_students, select_students
from .preview import PreviewManager
from .service import run
from .storage import read_state


def create_app(root: Path | None = None, *, compiler=None, preview_limit: int | None = None, build_compiler=None) -> Flask:
    root = (root or Path.cwd()).resolve()
    assets = Path(os.environ.get('DM_WEB_ASSETS', str(Path(__file__).parent / 'web_assets'))).resolve()
    app = Flask(__name__, static_folder=str(assets), static_url_path='/assets')
    app.config.update(SECRET_KEY=secrets.token_bytes(32), MAX_CONTENT_LENGTH=5 * 1024 * 1024,
                      SESSION_COOKIE_HTTPONLY=True, SESSION_COOKIE_SAMESITE='Strict')
    files = FileStore(root)
    limit = preview_limit if preview_limit is not None else int(os.environ.get('PREVIEW_CACHE_MB', '1024')) * 1024 ** 2
    if limit <= 0:
        raise ToolError('PREVIEW_CACHE_MB должен быть положительным.')
    files.clear_deleted_history()
    previews = PreviewManager(root, compiler=compiler, max_bytes=limit)
    executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix='export')
    jobs = {}
    job_lock = threading.RLock()
    app.extensions['dm'] = {'files': files, 'previews': previews, 'executor': executor, 'jobs': jobs,
                            'build_compiler': build_compiler if build_compiler is not None else compiler}

    @app.before_request
    def local_access():
        if not re.fullmatch(r'(?:localhost|127\.0\.0\.1)(?::\d+)?', request.host):
            return jsonify(error='Интерфейс доступен только через localhost.'), 403
        if request.method not in ('GET', 'HEAD', 'OPTIONS'):
            origin = request.headers.get('Origin')
            if origin and origin != request.host_url.rstrip('/'):
                return jsonify(error='Недопустимый источник запроса.'), 403
            token = request.headers.get('X-CSRF-Token', '')
            if not token or not hmac.compare_digest(token, session.get('csrf', '')):
                return jsonify(error='Обновите страницу: токен сессии отсутствует или устарел.'), 403

    @app.after_request
    def headers(response):
        response.headers['X-Content-Type-Options'] = 'nosniff'
        response.headers['Referrer-Policy'] = 'same-origin'
        response.headers['X-Frame-Options'] = 'SAMEORIGIN'
        if request.path.startswith('/api/'):
            response.headers['Cache-Control'] = 'no-store'
        return response

    @app.errorhandler(Exception)
    def error(exc):
        if isinstance(exc, Conflict):
            return jsonify(error=str(exc), conflict=True), 409
        if isinstance(exc, (ToolError, OSError, UnicodeError, ValueError, TypeError, KeyError)):
            return jsonify(error=str(exc)), 400
        if isinstance(exc, HTTPException):
            return jsonify(error=exc.description), exc.code
        app.logger.exception('Ошибка веб-утилиты')
        return jsonify(error='Внутренняя ошибка. Подробности в журнале сервера.'), 500

    def payload():
        data = request.get_json()
        if not isinstance(data, dict):
            raise ToolError('Ожидается JSON-объект.')
        return data

    def csv_path(name: str) -> Path:
        if not isinstance(name, str) or '/' in name or not name.endswith('.csv'):
            raise ToolError('Выберите CSV из work/students/.')
        return confined(root, 'work/students/' + name)

    def roster_info(name: str) -> dict:
        students = read_students(csv_path(name))
        return {'name': name, 'students': [vars(s) for s in students]}

    def assignments(bank: str) -> bool:
        if not re.fullmatch(r'TR[1-9]\d*', bank):
            raise ToolError('Выберите типовик.')
        settings = read_settings(root)
        state = confined(root, f'work/assignments/{settings.period}/{bank}/current.json')
        return bool(read_state(state, settings.period, bank))

    @app.get('/')
    def index():
        return send_file(assets / 'index.html')

    @app.get('/api/bootstrap')
    def bootstrap():
        session.setdefault('csrf', secrets.token_urlsafe(32))
        folder = confined(root, 'work/students')
        folder.mkdir(parents=True, exist_ok=True)
        return jsonify(csrf=session['csrf'], tree=files.tree(), csvs=sorted(p.name for p in folder.glob('*.csv') if not p.is_symlink()),
                       settings=vars(read_settings(root)))

    @app.get('/api/tree')
    def tree():
        return jsonify(tree=files.tree())

    @app.get('/api/file')
    def read_file():
        return jsonify(files.read(request.args.get('path', '')))

    @app.put('/api/file')
    def save_file():
        data = payload()
        return jsonify(files.save(data['path'], data['text'], data['revision']))

    @app.post('/api/variants')
    def create_variant():
        data = payload()
        return jsonify(files.create(data['bank'], data['number'])), 201

    @app.delete('/api/variants')
    def delete_variant():
        data = payload()
        return jsonify(files.delete(data['path'], data['revision']))

    @app.post('/api/variants/restore')
    def restore_variant():
        return jsonify(files.restore(payload()['backup']))

    @app.post('/api/previews')
    def preview():
        data = payload()
        return jsonify(previews.request(data['path'], data['revision']))

    @app.get('/api/previews/<key>')
    def preview_status(key):
        return jsonify(previews.status(key))

    @app.get('/api/previews/<key>/pdf')
    def preview_pdf(key):
        return send_file(previews.pdf(key), mimetype='application/pdf', conditional=True)

    @app.get('/api/roster')
    def roster():
        return jsonify(roster_info(request.args.get('name', '')))

    @app.post('/api/rosters')
    def upload():
        upload = request.files.get('file')
        if upload is None:
            raise ToolError('Выберите CSV студентов.')
        name = upload.filename or ''
        csv_path(name)  # Validate before writing anything.
        folder = confined(root, 'work/students')
        folder.mkdir(parents=True, exist_ok=True)
        fd, temporary = tempfile.mkstemp(prefix='.upload-', suffix='.csv', dir=folder)
        temp = Path(temporary)
        try:
            with os.fdopen(fd, 'wb') as stream:
                stream.write(upload.read())
            read_students(temp)
            with files.editing():
                candidate = name
                count = 1
                while csv_path(candidate).exists():
                    count += 1
                    candidate = f'{Path(name).stem}-{count}.csv'
                temp.rename(csv_path(candidate))
            return jsonify(roster_info(candidate)), 201
        finally:
            temp.unlink(missing_ok=True)

    @app.get('/api/assignments')
    def assignment_status():
        return jsonify(exists=assignments(request.args.get('bank', '')))

    def perform(identifier: str, bank: str, kwargs: dict):
        with job_lock:
            jobs[identifier] = {'status': 'running', 'message':'Подготовка исходников…'}
        def progress(message):
            with job_lock:
                jobs[identifier]['message'] = message
        def export_path(path):
            return path.relative_to(root / 'work/exports').as_posix() if path else None
        try:
            result = run(root, bank, progress=progress, compiler=app.extensions['dm']['build_compiler'], **kwargs)
            archive = result.archive.relative_to(root / 'work/exports').as_posix() if result.archive else None
            response = {'status': 'success', 'directory': result.directory.relative_to(root).as_posix(),
                        'archive': archive, 'students': result.students, 'flows': result.flows, 'changes': result.changes,
                        'pdfs': [export_path(path) for path in result.pdfs], 'pdf_archive': export_path(result.pdf_archive)}
        except CompilationError as exc:
            response = {'status':'error', 'error':str(exc), 'log':exc.log,
                        'document':exc.document, 'line':exc.line,
                        'source':exc.source, 'detail':exc.detail, 'hint':exc.hint,
                        'directory':exc.directory.relative_to(root).as_posix() if exc.directory else None}
        except Exception as exc:
            app.logger.info('Сборка не выполнена: %s', exc)
            response = {'status': 'error', 'error': str(exc)}
        with job_lock:
            jobs[identifier] = response

    @app.post('/api/builds')
    def build():
        data = payload()
        bank = data['bank']
        if bank not in [b['name'] for b in files.tree()]:
            raise ToolError('Выберите существующий типовик.')
        kind = data.get('kind', 'review')
        if kind not in ('review', 'students', 'redistribute'):
            raise ToolError('Неизвестное действие.')
        pdf = data.get('pdf', False)
        if type(pdf) is not bool:
            raise ToolError('Параметр PDF должен быть логическим значением.')
        kwargs = {'kind': kind, 'pdf': pdf and kind != 'redistribute'}
        if kind != 'review':
            path = csv_path(data['csv'])
            selection = data.get('selection', 'all')
            values = data.get('values', [])
            if not isinstance(values, list) or not all(isinstance(v, str) for v in values):
                raise ToolError('Некорректный выбор студентов.')
            select_students(read_students(path), selection, tuple(values))
            if kind == 'redistribute' and assignments(bank) and data.get('confirmed') is not True:
                raise ToolError('Подтвердите замену сохранённых назначений.')
            kwargs.update(csv_path=path, selection=selection, values=tuple(values))
        with job_lock:
            if any(j['status'] in ('queued', 'running') for j in jobs.values()):
                raise Conflict('Другая сборка ещё выполняется. Дождитесь её завершения.')
            if len(jobs) >= 100:
                del jobs[next(iter(jobs))]
            identifier = secrets.token_hex(16)
            jobs[identifier] = {'status': 'queued'}
            executor.submit(perform, identifier, bank, kwargs)
        return jsonify(id=identifier), 202

    @app.get('/api/builds/<identifier>')
    def build_status(identifier):
        with job_lock:
            if identifier not in jobs:
                raise ToolError('Сборка не найдена.')
            return jsonify(jobs[identifier])

    @app.get('/api/exports/<path:name>')
    def download(name):
        if Path(name).suffix not in ('.zip', '.pdf'):
            raise ToolError('Для скачивания выберите ZIP или PDF.')
        path = confined(root, 'work/exports/' + name)
        return send_file(path, as_attachment=True, download_name=path.name)

    return app


def main():
    parser = argparse.ArgumentParser(description='Локальная веб-утилита типовых расчётов')
    parser.add_argument('--root', type=Path, default=Path.cwd())
    parser.add_argument('--host', default='127.0.0.1')
    parser.add_argument('--port', type=int, default=8080)
    args = parser.parse_args()
    from waitress import serve
    app = create_app(args.root)
    print(f'Откройте http://localhost:{args.port}', flush=True)
    try:
        serve(app, host=args.host, port=args.port, threads=6)
    finally:
        app.extensions['dm']['previews'].close()
        app.extensions['dm']['executor'].shutdown(wait=False)


if __name__ == '__main__':
    main()
