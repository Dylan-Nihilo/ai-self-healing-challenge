import hashlib
import json
import secrets
import sqlite3
import threading
import time
import zipfile
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4


def stamp():
    return datetime.now(timezone.utc).isoformat()


def digest(data):
    return hashlib.sha256(data).hexdigest()


class WorkstationError(Exception):
    def __init__(self, code, message, status=409):
        self.code, self.message, self.status = code, message, status
        super().__init__(message)


class Workstation:
    """The product's real catalog, search index, export worker and persistent jobs."""

    def __init__(self, root: Path, seed: Path):
        self.root, self.seed = root, seed
        root.mkdir(parents=True, exist_ok=True)
        self.lock = threading.RLock()
        self.check_lock = threading.Lock()
        self.database = root / 'workstation.sqlite3'
        self.spec = json.loads((seed / 'library.json').read_text())
        self.loaded = {}
        with closing(self.connect()) as db, db:
            db.executescript('''
                CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS jobs (id TEXT PRIMARY KEY, title TEXT NOT NULL,
                    state TEXT NOT NULL, probe INTEGER NOT NULL, created_at TEXT NOT NULL,
                    error TEXT, output TEXT);
                CREATE TABLE IF NOT EXISTS events (seq INTEGER PRIMARY KEY AUTOINCREMENT,
                    created_at TEXT NOT NULL, actor TEXT NOT NULL, title TEXT NOT NULL, details TEXT NOT NULL);
            ''')
        if not self.meta('installed'):
            self._install()
        self.loaded = self.settings()
        with closing(self.connect()) as db, db:
            db.execute("UPDATE jobs SET state='failed', error='处理进程中断，任务已保留，可重试。' WHERE state='working'")
            db.execute('DELETE FROM jobs WHERE probe=1')
        self.checks = []
        self.checked_at = None

    def connect(self):
        connection = sqlite3.connect(self.database, timeout=10)
        connection.row_factory = sqlite3.Row
        return connection

    def meta(self, key, value=None):
        with closing(self.connect()) as db, db:
            if value is not None:
                db.execute('INSERT OR REPLACE INTO meta VALUES (?,?)', (key, json.dumps(value)))
                return value
            row = db.execute('SELECT value FROM meta WHERE key=?', (key,)).fetchone()
            return json.loads(row['value']) if row else None

    def event(self, actor, title, details=None):
        with closing(self.connect()) as db, db:
            db.execute('INSERT INTO events(created_at,actor,title,details) VALUES (?,?,?,?)',
                       (stamp(), actor, title, json.dumps(details or {}, ensure_ascii=False)))
            db.execute('DELETE FROM events WHERE seq < (SELECT COALESCE(MAX(seq),0)-300 FROM events)')

    def events(self):
        with closing(self.connect()) as db:
            return [{**dict(row), 'details': json.loads(row['details'])} for row in
                    db.execute('SELECT * FROM events ORDER BY seq DESC LIMIT 80')]

    def write_json(self, filename, value):
        target = self.root / filename
        temporary = target.with_suffix('.tmp')
        temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding='utf-8')
        temporary.replace(target)

    def _install(self):
        (self.root / 'documents').mkdir(exist_ok=True)
        (self.root / 'snapshots').mkdir(exist_ok=True)
        storage = 'exports-' + secrets.token_hex(3)
        (self.root / storage).mkdir()
        self.meta('storage', [storage])
        for item in self.spec['documents']:
            (self.root / 'documents' / item['file']).write_bytes((self.seed / 'documents' / item['file']).read_bytes())
        current = {'schema_version': 2, 'documents': self.spec['documents']}
        old = {'schema_version': 1, 'documents': self.spec['documents'][:1]}
        snapshots = []
        for version, catalog in [('1.x', old), ('2.x', current)]:
            identifier = 'snapshot-' + secrets.token_hex(4)
            raw = json.dumps(catalog, ensure_ascii=False).encode()
            (self.root / 'snapshots' / f'{identifier}.json').write_bytes(raw)
            snapshots.append({'id': identifier, 'resource': 'catalog', 'version': version, 'sha256': digest(raw)})
        self.meta('snapshots', snapshots)
        self.write_json('catalog.json', current)
        self.write_json('settings.json', {'revision': 1, 'export_directory': storage})
        self.loaded = self.settings()
        self.rebuild_index(digest((self.root / 'catalog.json').read_bytes()))
        self.inject('mixed')
        self.meta('installed', True)

    def settings(self):
        return json.loads((self.root / 'settings.json').read_text())

    def library(self):
        with self.lock:
            try:
                catalog = json.loads((self.root / 'catalog.json').read_text())
            except (ValueError, OSError):
                raise WorkstationError('catalog_unreadable', '资料目录无法解析，资料列表暂不可用。', 503)
            if catalog.get('schema_version') != 2 or not isinstance(catalog.get('documents'), list):
                raise WorkstationError('catalog_version', '当前工作站需要 2.x 目录格式。', 503)
            return catalog['documents']

    def document(self, identifier):
        with self.lock:
            item = next((item for item in self.library() if item['id'] == identifier), None)
            if not item:
                raise WorkstationError('document_not_found', '资料不存在。', 404)
            path = (self.root / 'documents' / item['file']).resolve()
            if path.parent != (self.root / 'documents').resolve():
                raise WorkstationError('invalid_document', '资料路径不合法。')
            try:
                return {**item, 'text': path.read_text(encoding='utf-8')}
            except (OSError, UnicodeError):
                raise WorkstationError('document_unreadable', '资料原件无法读取。', 503)

    def search(self, query):
        with self.lock:
            try:
                with closing(sqlite3.connect(self.root / 'search.sqlite3')) as db:
                    db.row_factory = sqlite3.Row
                    return [dict(row) for row in db.execute(
                        'SELECT id,title,substr(body,1,180) AS excerpt FROM documents WHERE title LIKE ? OR body LIKE ? LIMIT 20',
                        (f'%{query}%', f'%{query}%'))]
            except sqlite3.Error:
                raise WorkstationError('index_incompatible', '检索索引不可用，请检查索引格式与资料目录。', 503)

    def rebuild_index(self, expected_catalog_sha):
        with self.lock:
            if digest((self.root / 'catalog.json').read_bytes()) != expected_catalog_sha:
                raise WorkstationError('resource_changed', '目录已变化，请重新读取后再处理。')
            documents = [self.document(item['id']) for item in self.library()]
            temporary = self.root / 'search-building.sqlite3'
            temporary.unlink(missing_ok=True)
            with closing(sqlite3.connect(temporary)) as db, db:
                db.execute('CREATE TABLE documents (id TEXT PRIMARY KEY,title TEXT NOT NULL,body TEXT NOT NULL)')
                db.executemany('INSERT INTO documents VALUES (?,?,?)', [(d['id'], d['title'], d['text']) for d in documents])
            temporary.replace(self.root / 'search.sqlite3')
            return {'indexed_documents': len(documents)}

    def create_job(self, probe=False):
        identifier = str(uuid4())
        with closing(self.connect()) as db, db:
            db.execute('INSERT INTO jobs(id,title,state,probe,created_at) VALUES (?,?,?,?,?)',
                       (identifier, '功能自检' if probe else '资料完整性归档', 'queued', int(probe), stamp()))
        return self.job(identifier)

    def job(self, identifier):
        with closing(self.connect()) as db:
            row = db.execute('SELECT * FROM jobs WHERE id=?', (identifier,)).fetchone()
            if not row:
                raise WorkstationError('job_not_found', '归档任务不存在。', 404)
            value = dict(row)
            value['output'] = json.loads(value['output']) if value['output'] else None
            return value

    def jobs(self):
        with closing(self.connect()) as db:
            identifiers = [row['id'] for row in db.execute('SELECT id FROM jobs WHERE probe=0 ORDER BY created_at DESC')]
        return [self.job(identifier) for identifier in identifiers]

    def process_next(self):
        with self.lock, closing(self.connect()) as db, db:
            row = db.execute("SELECT * FROM jobs WHERE state='queued' ORDER BY created_at LIMIT 1").fetchone()
            if not row:
                return False
            identifier = row['id']
            db.execute("UPDATE jobs SET state='working' WHERE id=?", (identifier,))
            settings = dict(self.loaded)
        temporary = None
        target = None
        try:
            documents = [self.document(item['id']) for item in self.library()]
            directory = settings['export_directory']
            if directory not in self.meta('storage'):
                raise WorkstationError('export_destination_missing', '导出目的地不可用，当前工作进程尚未加载可用的存储配置。')
            target = self.root / directory / f'{identifier}.zip'
            temporary = target.with_suffix('.part')
            with zipfile.ZipFile(temporary, 'w', compression=zipfile.ZIP_DEFLATED) as archive:
                for item in documents:
                    archive.writestr(item['file'], (self.root / 'documents' / item['file']).read_bytes())
            temporary.replace(target)
            output = {'directory': directory, 'file': target.name, 'sha256': digest(target.read_bytes())}
            with self.lock, closing(self.connect()) as db, db:
                exists = db.execute('SELECT id FROM jobs WHERE id=?', (identifier,)).fetchone()
                if exists:
                    db.execute("UPDATE jobs SET state='done',error=NULL,output=? WHERE id=?", (json.dumps(output), identifier))
                else:
                    target.unlink(missing_ok=True)
        except Exception as error:
            message = error.message if isinstance(error, WorkstationError) else '归档处理失败，任务与资料已保留。'
            with closing(self.connect()) as db, db:
                db.execute("UPDATE jobs SET state='failed',error=? WHERE id=?", (message, identifier))
            if not row['probe']:
                self.event('worker', '归档任务失败', {'job_id': identifier, 'error': message, 'loaded_revision': settings['revision']})
        finally:
            if temporary:
                temporary.unlink(missing_ok=True)
        return True

    def artifact(self, identifier):
        item = self.job(identifier)
        output = item['output']
        if item['state'] != 'done' or not output:
            raise WorkstationError('artifact_not_ready', '资料包尚未生成。')
        if output['directory'] not in self.meta('storage') or output['file'] != f'{identifier}.zip':
            raise WorkstationError('invalid_artifact', '归档路径不合法。')
        path = self.root / output['directory'] / output['file']
        if not path.is_file() or digest(path.read_bytes()) != output['sha256']:
            raise WorkstationError('artifact_invalid', '资料包缺失或校验不一致。')
        return path

    def retry_job(self, identifier):
        with self.lock, closing(self.connect()) as db, db:
            item = self.job(identifier)
            if item['probe']:
                raise WorkstationError('probe_owned_by_checker', '自检任务由固定检查器管理。')
            if item['state'] == 'failed':
                db.execute("UPDATE jobs SET state='queued',error=NULL,output=NULL WHERE id=?", (identifier,))
            return {'job_id': identifier, 'state': 'queued' if item['state'] == 'failed' else item['state']}

    def run_checks(self):
        with self.check_lock:
            results = []
            def check(identifier, label, operation):
                try:
                    detail = operation()
                    results.append({'id': identifier, 'label': label, 'ok': True, 'detail': detail})
                except Exception as error:
                    detail = error.message if isinstance(error, WorkstationError) else str(error)[:200]
                    results.append({'id': identifier, 'label': label, 'ok': False, 'detail': detail})

            def catalog_probe():
                documents = self.library()
                if {d['id'] for d in documents} != {d['id'] for d in self.spec['documents']}:
                    raise WorkstationError('catalog_incomplete', '资料目录未覆盖原有资料。')
                for item in documents:
                    self.document(item['id'])
                    if (self.root / 'documents' / item['file']).read_bytes() != (self.seed / 'documents' / item['file']).read_bytes():
                        raise WorkstationError('source_changed', '原始资料内容发生变化。')
                return f'{len(documents)} 份原件均可读取，内容保持完整。'

            def search_probe():
                probe = self.spec['search_probe']
                found = self.search(probe['query'])
                if probe['document_id'] not in {item['id'] for item in found}:
                    raise WorkstationError('search_miss', '真实检索未返回应有资料，索引可能过期。')
                return f'检索“{probe["query"]}”返回了对应原件。'

            def export_probe():
                item = self.create_job(probe=True)
                try:
                    deadline = time.monotonic() + 2
                    while time.monotonic() < deadline:
                        state = self.job(item['id'])
                        if state['state'] in {'done', 'failed'}:
                            break
                        time.sleep(.05)
                    if state['state'] != 'done':
                        raise WorkstationError('export_probe_failed', state['error'] or '归档工作进程未在时限内完成请求。')
                    with zipfile.ZipFile(self.artifact(item['id'])) as archive:
                        for document in self.spec['documents']:
                            if archive.read(document['file']) != (self.seed / 'documents' / document['file']).read_bytes():
                                raise WorkstationError('export_content_mismatch', '归档内容与原始资料不一致。')
                    return '工作进程实际生成了 ZIP，解包和原件比对通过。'
                finally:
                    with self.lock, closing(self.connect()) as db, db:
                        state = self.job(item['id'])
                        if state['output']:
                            (self.root / state['output']['directory'] / state['output']['file']).unlink(missing_ok=True)
                        db.execute('DELETE FROM jobs WHERE id=?', (item['id'],))

            def backlog_probe():
                pending = [item for item in self.jobs() if item['state'] != 'done']
                if pending:
                    raise WorkstationError('unfinished_jobs', f'{len(pending)} 个原有归档任务仍未完成，需要保留并继续处理。')
                for item in self.jobs():
                    self.artifact(item['id'])
                return '原有任务均已完成，归档文件可下载。'

            check('catalog', '资料读取', catalog_probe)
            check('search', '全文检索', search_probe)
            check('export', '资料归档', export_probe)
            check('backlog', '原有任务', backlog_probe)
            self.checks, self.checked_at = results, stamp()
            return results

    def inject(self, kind='mixed'):
        with self.lock:
            if kind not in {'catalog', 'search', 'worker', 'mixed'}:
                raise WorkstationError('invalid_fault', '不支持的演练故障。')
            if kind in {'catalog', 'mixed'}:
                (self.root / 'catalog.json').write_text('{"schema_version":2,"documents":[', encoding='utf-8')
            if kind in {'search', 'mixed'}:
                with closing(sqlite3.connect(self.root / 'search.sqlite3')) as db, db:
                    db.executescript('DROP TABLE IF EXISTS documents; CREATE TABLE documents (legacy_title TEXT);')
            if kind in {'worker', 'mixed'}:
                settings = self.settings()
                self.write_json('settings.json', {'revision': settings['revision'] + 1, 'export_directory': 'offline-' + secrets.token_hex(3)})
                self.loaded = self.settings()
            self.create_job()
            self.event('system', '工作站进入恢复模式', {'kind': kind, 'original_documents_preserved': True})
            return {'injected': True}

    def tool(self, name, arguments):
        schemas = {
            'read_resource': {'resource'}, 'list_snapshots': set(), 'read_snapshot': {'snapshot_id'},
            'list_manuals': set(), 'read_manual': {'document_id'}, 'inspect_storage': set(),
            'restore_catalog': {'snapshot_id', 'expected_sha256'}, 'rebuild_index': {'expected_catalog_sha'},
            'update_settings': {'expected_revision', 'export_directory'}, 'reload_worker': {'expected_revision'},
            'retry_job': {'job_id'}, 'run_self_check': set(),
        }
        if name not in schemas or set(arguments) != schemas[name]:
            raise WorkstationError('invalid_tool_arguments', '工具名称或参数不符合固定接口。', 400)
        for key, value in arguments.items():
            if key == 'expected_revision':
                valid = type(value) is int and value > 0
            else:
                valid = isinstance(value, str) and 0 < len(value) <= 256
            if not valid:
                raise WorkstationError('invalid_tool_arguments', '工具参数类型或长度不正确。', 400)
        if name == 'run_self_check':
            return {'checks': self.run_checks()}
        with self.lock:
            if name == 'read_resource':
                resource = arguments['resource']
                if resource == 'catalog':
                    raw = (self.root / 'catalog.json').read_bytes()
                    return {'sha256': digest(raw), 'text': raw.decode('utf-8', errors='replace')}
                if resource == 'settings':
                    return self.settings()
                if resource == 'loaded_settings':
                    return dict(self.loaded)
                if resource == 'jobs':
                    return {'jobs': self.jobs()}
                if resource == 'logs':
                    return {'events': self.events()[:35]}
                raise WorkstationError('unknown_resource', '未知资源。', 404)
            if name == 'list_snapshots':
                return {'snapshots': self.meta('snapshots')}
            if name in {'read_snapshot', 'restore_catalog'}:
                snapshot = next((s for s in self.meta('snapshots') if s['id'] == arguments['snapshot_id']), None)
                if not snapshot:
                    raise WorkstationError('snapshot_missing', '快照不存在。', 404)
                raw = (self.root / 'snapshots' / f'{snapshot["id"]}.json').read_bytes()
                if digest(raw) != snapshot['sha256']:
                    raise WorkstationError('snapshot_corrupted', '快照校验不一致。')
                if name == 'read_snapshot':
                    return {**snapshot, 'text': raw.decode()}
                if digest((self.root / 'catalog.json').read_bytes()) != arguments['expected_sha256']:
                    raise WorkstationError('resource_changed', '目录已变化，请重新读取后再处理。')
                temporary = self.root / 'catalog-restoring.json'
                temporary.write_bytes(raw)
                temporary.replace(self.root / 'catalog.json')
                return {'restored': snapshot['id'], 'sha256': snapshot['sha256']}
            if name == 'list_manuals':
                return {'manuals': [{'id': p.stem, 'title': p.read_text().splitlines()[0].lstrip('# ')} for p in sorted((self.seed / 'manuals').glob('*.md'))]}
            if name == 'read_manual':
                paths = {p.stem: p for p in (self.seed / 'manuals').glob('*.md')}
                if arguments['document_id'] not in paths:
                    raise WorkstationError('manual_missing', '资料不存在。', 404)
                return {'text': paths[arguments['document_id']].read_text()}
            if name == 'inspect_storage':
                return {'directories': [{'id': name, 'purpose': '资料归档输出', 'available': (self.root / name).is_dir()} for name in self.meta('storage')]}
            if name == 'rebuild_index':
                return self.rebuild_index(arguments['expected_catalog_sha'])
            if name in {'update_settings', 'reload_worker'}:
                settings = self.settings()
                revision = arguments['expected_revision']
                if type(revision) is not int or settings['revision'] != revision:
                    raise WorkstationError('revision_conflict', '配置版本已变化，请重新查询。')
                if name == 'reload_worker':
                    self.loaded = settings
                    return {'loaded_revision': settings['revision']}
                if arguments['export_directory'] not in self.meta('storage'):
                    raise WorkstationError('storage_unavailable', '请选择当前实际存在的归档目的地。')
                self.write_json('settings.json', {'revision': revision + 1, 'export_directory': arguments['export_directory']})
                return self.settings()
            if name == 'retry_job':
                return self.retry_job(arguments['job_id'])
        raise WorkstationError('unknown_tool', '未知工具。', 404)
