from .configuration import zone
import json
import sqlite3
import uuid
import os
from pathlib import Path
from datetime import datetime
from contextlib import contextmanager
from .model import DEFAULT_DB, OPEN, CAL, dumps, digest, now

CURRENT_SCHEMA=9

RETENTION_SCHEMA = """
CREATE TABLE message_versions(id TEXT PRIMARY KEY,details TEXT NOT NULL);
CREATE TABLE retention_activity(kind TEXT,id TEXT,at TEXT NOT NULL,PRIMARY KEY(kind,id));
CREATE TABLE maintenance_files(path TEXT PRIMARY KEY,details TEXT NOT NULL);
"""

SCHEMA = """
CREATE TABLE meta(key TEXT PRIMARY KEY,value TEXT NOT NULL);
CREATE TABLE cases(id TEXT PRIMARY KEY,title TEXT NOT NULL,position TEXT NOT NULL,status TEXT NOT NULL,revision INTEGER NOT NULL,updated_at TEXT NOT NULL,details TEXT NOT NULL);
CREATE TABLE actions(id TEXT PRIMARY KEY,case_id TEXT NOT NULL REFERENCES cases(id),status TEXT NOT NULL,next_review TEXT,details TEXT NOT NULL);
CREATE TABLE decisions(id TEXT PRIMARY KEY,case_id TEXT REFERENCES cases(id),details TEXT NOT NULL);
CREATE TABLE evidence(id TEXT PRIMARY KEY,metadata TEXT NOT NULL,body TEXT NOT NULL);
CREATE TABLE context(id TEXT PRIMARY KEY,entity TEXT NOT NULL,details TEXT NOT NULL);
CREATE TABLE approvals(id TEXT PRIMARY KEY,case_id TEXT REFERENCES cases(id),details TEXT NOT NULL);
CREATE TABLE standing_approvals(id TEXT PRIMARY KEY,case_id TEXT REFERENCES cases(id) ON DELETE CASCADE,details TEXT NOT NULL);
CREATE TABLE mutations(key TEXT PRIMARY KEY,hash TEXT NOT NULL,result TEXT NOT NULL);
CREATE TABLE runs(id TEXT PRIMARY KEY,started_at TEXT NOT NULL,status TEXT NOT NULL,details TEXT NOT NULL);
CREATE TABLE sources(run_id TEXT REFERENCES runs(id),source TEXT,details TEXT NOT NULL,PRIMARY KEY(run_id,source));
CREATE TABLE candidates(run_id TEXT REFERENCES runs(id),id TEXT,details TEXT NOT NULL,PRIMARY KEY(run_id,id));
CREATE TABLE checkpoints(source TEXT PRIMARY KEY,discovered TEXT,reviewed TEXT,reported TEXT);
CREATE TABLE issues(id TEXT PRIMARY KEY,case_id TEXT,details TEXT NOT NULL,resolved INTEGER NOT NULL DEFAULT 0);
CREATE TABLE roundup(id TEXT PRIMARY KEY,details TEXT NOT NULL,reported_run TEXT);
CREATE TABLE reported_cases(case_id TEXT PRIMARY KEY,revision INTEGER NOT NULL);
CREATE TABLE calendar_state(source TEXT PRIMARY KEY,run_id TEXT NOT NULL,details TEXT NOT NULL);
CREATE TABLE metrics(run_id TEXT PRIMARY KEY,details TEXT NOT NULL);
CREATE TABLE message_reviews(id TEXT PRIMARY KEY,source TEXT NOT NULL,details TEXT NOT NULL);
CREATE TABLE improvements(id TEXT PRIMARY KEY,status TEXT NOT NULL,revision INTEGER NOT NULL,details TEXT NOT NULL);
CREATE TABLE reflections(run_id TEXT PRIMARY KEY REFERENCES runs(id),details TEXT NOT NULL);
CREATE TABLE calendar_reports(source TEXT PRIMARY KEY,run_id TEXT NOT NULL,details TEXT NOT NULL);
""" + RETENTION_SCHEMA + f"PRAGMA user_version={CURRENT_SCHEMA};"


class Storage:
    def __init__(self, path=DEFAULT_DB, create=False):
        self.path = Path(path)
        if not create and not self.path.is_file():
            raise ValueError("Monitor database unavailable. Do not fall back to legacy diaries.")
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(self.path, timeout=15)
        try:
            self._initialize(create)
        except BaseException:
            self.db.close()
            raise

    def _initialize(self, create):
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA foreign_keys=ON")
        version = self.db.execute("PRAGMA user_version").fetchone()[0]
        if version!=CURRENT_SCHEMA and not (version==0 and create):
            raise ValueError(f"Unsupported database schema {version}; only current format {CURRENT_SCHEMA} is supported. Legacy backups are not imported.")
        self.db.execute("PRAGMA journal_mode=WAL")
        if version == 0 and create:
            self.db.executescript(SCHEMA)
            self.db.execute("INSERT INTO meta VALUES('mode','active')")
            self.db.commit()
        from . import live
        live.initialize(self)
        from .threadcache import initialize, startup

        initialize(self)
        from .approvals import lifecycle as approval_lifecycle
        with self.write():
            approval_lifecycle(self)
        startup(self)
        self.backup()
        with self.write():live.prune(self)

    def touch(self, kind, item_id, at=None):
        self.db.execute(
            "INSERT INTO retention_activity VALUES(?,?,?) ON CONFLICT(kind,id) DO UPDATE SET at=max(at,excluded.at)",
            (kind, item_id, at or now()),
        )

    def decode_message(self, raw):
        from .messages import decode

        return decode(self, raw)

    def encode_message(self, message):
        from .messages import encode

        return encode(self, message)

    def close(self):
        self.db.close()

    def backup(self, label=None):
        from .backups import capture_profile, embedded_profile, include_profile, capture_configuration, include_configuration
        profile = capture_profile(self)
        configuration=capture_configuration(self)
        folder = self.path.parent / "backups"
        folder.mkdir(exist_ok=True)
        if folder.is_symlink() or (hasattr(folder, 'is_junction') and folder.is_junction()):
            raise ValueError('Backup directory cannot be a link/junction')
        day = datetime.now(zone())
        names = [label] if label else [f"daily-{day:%Y-%m-%d}", f"weekly-{day:%G-%V}"]
        for name in names:
            if not name or any(
                c not in "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789-_"
                for c in name
            ):
                raise ValueError("Invalid backup label")
            target = folder / f"{name}.db"
            current_format=False
            if target.exists():
                try:
                    check=sqlite3.connect(target.resolve().as_uri()+'?mode=ro&immutable=1',uri=True)
                    try:current_format=check.execute('PRAGMA user_version').fetchone()[0]==CURRENT_SCHEMA
                    finally:check.close()
                    if current_format and profile is not None:
                        current_format = embedded_profile(target) is not None
                except (sqlite3.Error, ValueError):current_format=False
            if not current_format:
                temporary = folder / f".{name}-{uuid.uuid4().hex}.tmp"
                out = sqlite3.connect(temporary)
                source = self.db
                try:
                    if self.db.in_transaction:
                        # The writer lock freezes committed state; a separate read
                        # connection avoids SQLite's same-connection backup deadlock.
                        source = sqlite3.connect(
                            self.path.resolve().as_uri() + "?mode=ro", uri=True
                        )
                    source.backup(out)
                    from .threadcache import sanitize_backup

                    sanitize_backup(out)
                    include_profile(out, profile, now())
                    include_configuration(out, configuration)
                    if out.execute('PRAGMA quick_check').fetchone()[0]!='ok' or out.execute('PRAGMA foreign_key_check').fetchall():
                        raise ValueError('Replacement backup validation failed; existing slot preserved')
                except BaseException:
                    out.close()
                    temporary.unlink(missing_ok=True)
                    raise
                finally:
                    out.close()
                    if source is not self.db:
                        source.close()
                os.replace(temporary, target)
        from .backups import rotate

        rotation = rotate(self.path.parent)
        prior = self.db.execute("SELECT value FROM meta WHERE key='backup_rotation'").fetchone()
        if rotation['deleted'] or rotation['failures'] or (prior and json.loads(prior[0]).get('failures')):
            nested = self.db.in_transaction
            self.db.execute("INSERT INTO meta VALUES('backup_rotation',?) ON CONFLICT(key) DO UPDATE SET value=excluded.value", (dumps(dict(rotation, at=now())),))
            if not nested:
                self.db.commit()
        return [str(folder / f"{n}.db") for n in names]

    @contextmanager
    def write(self):
        self.backup()
        self.db.execute("BEGIN IMMEDIATE")
        try:
            yield
            self.db.commit()
        except BaseException:
            self.db.rollback()
            raise
        if getattr(self, "_thread_dirty", False) and not getattr(self, "_thread_sweeping", False):
            from .threadcache import sweep

            sweep(self)

    @contextmanager
    def read(self):
        nested = self.db.in_transaction
        if not nested:
            self.db.execute("BEGIN")
        try:
            yield
        finally:
            if not nested:
                self.db.rollback()

    def evidence_exists(self, ids):
        for eid in ids:
            if not self.db.execute("SELECT 1 FROM evidence WHERE id=?", (eid,)).fetchone():
                raise ValueError(f"Unknown evidence {eid}")

    def put_evidence(self, item):
        from .threadcache import mail_nodes, BODY_KINDS

        try:
            content = json.loads(item.get("body", ""))
        except (ValueError, TypeError):
            content = None
        plain_mail=(content is None or isinstance(content,str)) and (item.get('entry_id') or item.get('attachment_path')) and str(item.get('source','')).startswith('mail:') and item.get('kind')!='mail-source-reference'
        if plain_mail or item.get("kind") in BODY_KINDS or list(mail_nodes(content)) or list(mail_nodes({k:v for k,v in item.items() if k!='body'})):
            raise ValueError("Email bodies belong in a thread cache: use thread put --input - with case_id and message")
        from .evidence import has_card
        if has_card(content) or has_card({k:v for k,v in item.items() if k!='body'}):
            raise ValueError('Do not store card snapshots as evidence; update the single current card')
        eid = item.get("id") or digest(item)
        metadata = {k: v for k, v in item.items() if k not in {"body", "id"}}
        values = (dumps(metadata), item.get("body", ""))
        with self.write():
            old = self.db.execute(
                "SELECT metadata,body FROM evidence WHERE id=?", (eid,)
            ).fetchone()
            if old:
                original = json.loads(old[0])
                for internal in ('owner_case_id', 'source_evidence_id', 'scope'):
                    if internal not in metadata:
                        original.pop(internal, None)
                if (dumps(original), old[1]) != values:
                    raise ValueError("Evidence is immutable; use a new ID")
            self.db.execute("INSERT OR IGNORE INTO evidence VALUES(?,?,?)", (eid, *values))
            self.touch("evidence", eid)
        return {"id": eid}

    def doctor(self):
        problems = []
        if self.db.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
            problems.append("Database integrity failure")
        problems.extend(
            "Foreign-key failure: " + str(tuple(r))
            for r in self.db.execute("PRAGMA foreign_key_check")
        )
        from .evidence import references
        from .records import validate_match_keys
        for row in self.db.execute('SELECT id,details FROM cases'):
            try:validate_match_keys(json.loads(row[1]).get('match_keys', {}))
            except ValueError as error:problems.append('Invalid matching keys in ' + row[0] + ': ' + str(error))
        available = {r[0] for r in self.db.execute('SELECT id FROM evidence')}
        for table in ('cases', 'actions', 'decisions', 'context', 'approvals', 'standing_approvals'):
            for row in self.db.execute(f'SELECT id,details FROM {table}'):
                missing = set(references(json.loads(row[1]))) - available
                if missing:
                    problems.append('Broken evidence in ' + table + ':' + row[0] + ': ' + ','.join(sorted(missing)))
        for r in self.db.execute("SELECT id,details FROM actions"):
            a = json.loads(r[1])
            if a["status"] in OPEN and not a.get("next_review"):
                problems.append("Missing review date: " + r[0])
            for eid in a.get("completion_evidence", []):
                if not self.db.execute("SELECT 1 FROM evidence WHERE id=?", (eid,)).fetchone():
                    problems.append("Broken evidence: " + eid)
        for r in self.db.execute("SELECT id FROM issues WHERE resolved=0"):
            problems.append("Unresolved issue: " + r[0])
        for table in ("candidates", "message_reviews"):
            for r in self.db.execute(f"SELECT id,details FROM {table}"):
                try:
                    self.decode_message(r["details"])
                except ValueError:
                    problems.append("Broken message metadata: " + r["id"])
        return {
            "ok": not problems,
            "problems": problems,
            "mode": self.db.execute("SELECT value FROM meta WHERE key='mode'").fetchone()[0],
        }
