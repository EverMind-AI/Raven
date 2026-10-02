"""Process-local SQLite authority, bounded transactions, and mutation receipts."""

import hashlib
import json
import os
import sqlite3
import stat
import time
from contextlib import contextmanager
from pathlib import Path
from uuid import UUID, uuid4

from raven.contracts.mailbox import MailboxError
from raven.mailbox.codec import canonical_bytes
from raven.utils.portable_lock import LockTimeoutError, file_lock

SCHEMA_VERSION = 1
MESSAGE_BUDGET = 262144
RECEIPT_LIMIT = 16384
RESULT_LIMIT = 8192
DEFAULT_QUOTAS = {
    "record_limit": 10000,
    "record_bytes": 268435456,
    "blob_bytes": 1073741824,
    "quarantine_bytes": 16777216,
}
_SCHEMA = """
CREATE TABLE metadata (key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TRIGGER immutable_root_update BEFORE UPDATE ON metadata
 WHEN OLD.key IN ('authority_id','tenant_id','storage_profile')
 BEGIN SELECT RAISE(ABORT,'immutable root identity'); END;
CREATE TRIGGER immutable_root_delete BEFORE DELETE ON metadata
 WHEN OLD.key IN ('authority_id','tenant_id','storage_profile')
 BEGIN SELECT RAISE(ABORT,'immutable root identity'); END;
CREATE TABLE cards (
 authority_id TEXT NOT NULL, tenant_id TEXT NOT NULL, agent_id TEXT NOT NULL,
 instance_id TEXT NOT NULL, generation INTEGER NOT NULL CHECK(generation>=1),
 card_json TEXT NOT NULL, allowed_scopes TEXT NOT NULL, allowed_kinds TEXT NOT NULL,
 max_in_flight INTEGER NOT NULL CHECK(max_in_flight>=1),
 PRIMARY KEY(authority_id,tenant_id,agent_id));
CREATE TABLE messages (
 authority_id TEXT NOT NULL, tenant_id TEXT NOT NULL, recipient TEXT NOT NULL,
 message_id TEXT NOT NULL, sender_agent TEXT NOT NULL, target_instance TEXT,
 envelope_bytes BLOB NOT NULL CHECK(length(envelope_bytes)<=65536), digest TEXT NOT NULL, created_at INTEGER NOT NULL,
 expires_at INTEGER NOT NULL CHECK(expires_at>created_at),
 phase TEXT NOT NULL DEFAULT 'pending' CHECK(phase IN
 ('pending','in_progress','completed','dead_letter')),
 revision INTEGER NOT NULL DEFAULT 0 CHECK(revision>=0),
 attempt INTEGER NOT NULL DEFAULT 0 CHECK(attempt BETWEEN 0 AND 5),
 not_before INTEGER NOT NULL DEFAULT 0,
 consumer_instance TEXT, consumer_generation INTEGER, claim_token TEXT, lease_until INTEGER,
 final_claim TEXT, result_json TEXT CHECK(result_json IS NULL OR length(CAST(result_json AS BLOB))<=8192),
 result_hash TEXT, terminal_at INTEGER, terminal_reason TEXT, outcome TEXT
 CHECK(outcome IS NULL OR outcome IN ('succeeded','failed','blocked')),
 reserved_bytes INTEGER NOT NULL CHECK(reserved_bytes BETWEEN 0 AND 262144),
 PRIMARY KEY(authority_id,tenant_id,recipient,message_id),
 FOREIGN KEY(authority_id,tenant_id,recipient) REFERENCES cards(authority_id,tenant_id,agent_id),
 FOREIGN KEY(authority_id,tenant_id,sender_agent) REFERENCES cards(authority_id,tenant_id,agent_id),
 CHECK(phase!='in_progress' OR (consumer_instance IS NOT NULL AND consumer_generation IS NOT NULL AND consumer_generation>=1
 AND claim_token IS NOT NULL AND lease_until IS NOT NULL AND attempt>=1)),
 CHECK(phase IN ('pending','in_progress') OR terminal_at IS NOT NULL));
CREATE INDEX messages_eligible ON messages(authority_id,tenant_id,recipient,phase,not_before,created_at,message_id);
CREATE TABLE receipt_outbox (
 authority_id TEXT NOT NULL, tenant_id TEXT NOT NULL, recipient TEXT NOT NULL,
 message_id TEXT NOT NULL, event_key TEXT NOT NULL,
 receipt_id TEXT, envelope_bytes BLOB, digest TEXT, created_at INTEGER, expires_at INTEGER,
 status TEXT NOT NULL DEFAULT 'unmaterialized' CHECK(status IN
 ('unmaterialized','pending','published','delivery_unknown')),
 event_json TEXT NOT NULL, artifact_refs TEXT NOT NULL DEFAULT '[]',
 PRIMARY KEY(authority_id,tenant_id,recipient,message_id,event_key),
 FOREIGN KEY(authority_id,tenant_id,recipient,message_id)
 REFERENCES messages(authority_id,tenant_id,recipient,message_id),
 CHECK(event_key IN ('terminal','received:1','received:2','received:3','received:4','received:5')),
 CHECK(envelope_bytes IS NULL OR length(envelope_bytes)<=16384),
 CHECK(status='unmaterialized' OR (receipt_id IS NOT NULL AND envelope_bytes IS NOT NULL
 AND digest IS NOT NULL AND created_at IS NOT NULL AND expires_at IS NOT NULL)));
CREATE TABLE mutation_receipts (
 authority_id TEXT NOT NULL, tenant_id TEXT NOT NULL, caller_agent_id TEXT NOT NULL,
 request_id TEXT NOT NULL, method TEXT NOT NULL, input_hash TEXT NOT NULL, result TEXT NOT NULL,
 PRIMARY KEY(authority_id,tenant_id,caller_agent_id,request_id),
 FOREIGN KEY(authority_id,tenant_id,caller_agent_id) REFERENCES cards(authority_id,tenant_id,agent_id));
CREATE TABLE quarantine (
 id INTEGER PRIMARY KEY, raw_bytes BLOB NOT NULL CHECK(length(raw_bytes)<=65536),
 reason TEXT NOT NULL, created_at INTEGER NOT NULL);
CREATE TABLE tombstones (
 authority_id TEXT NOT NULL, tenant_id TEXT NOT NULL, recipient TEXT NOT NULL,
 message_id TEXT NOT NULL, digest TEXT NOT NULL, phase TEXT NOT NULL
 CHECK(phase IN ('completed','dead_letter')),
 result_hash TEXT, terminal_at INTEGER NOT NULL, retain_until INTEGER NOT NULL,
 terminal_reason TEXT, outcome TEXT,
 artifact_refs TEXT NOT NULL DEFAULT '[]',
 receipt_links TEXT NOT NULL DEFAULT '[]',
 PRIMARY KEY(authority_id,tenant_id,recipient,message_id),
 FOREIGN KEY(authority_id,tenant_id,recipient) REFERENCES cards(authority_id,tenant_id,agent_id));
"""


_RECEIVER_SCHEMA = (
    """CREATE TABLE receiver_bindings (
        binding_id TEXT PRIMARY KEY,
        agent_id TEXT NOT NULL, instance_id TEXT NOT NULL, generation INTEGER NOT NULL CHECK(generation>=1),
        agent_name TEXT NOT NULL, registry_generation INTEGER NOT NULL CHECK(registry_generation>=1),
        task_id TEXT NOT NULL, workspace_id TEXT NOT NULL,
        terminal_handle TEXT, terminal_incarnation TEXT, session_key TEXT,
        capability_json TEXT NOT NULL, credential_hash TEXT NOT NULL UNIQUE,
        revoked_at INTEGER, created_at INTEGER NOT NULL)""",
    """CREATE TABLE notifications (
        request_id TEXT PRIMARY KEY, binding_id TEXT NOT NULL REFERENCES receiver_bindings(binding_id),
        message_ids TEXT NOT NULL, input_hash TEXT NOT NULL, stage TEXT NOT NULL,
        terminal_incarnation TEXT, bytes_written INTEGER NOT NULL DEFAULT 0 CHECK(bytes_written>=0),
        turn_id TEXT, detail TEXT, created_at INTEGER NOT NULL, updated_at INTEGER NOT NULL)""",
    """CREATE TABLE handoff_reads (
        offer_message_id TEXT NOT NULL, agent_id TEXT NOT NULL, instance_id TEXT NOT NULL,
        generation INTEGER NOT NULL CHECK(generation>=1), artifact_hash TEXT NOT NULL,
        bytes_read INTEGER NOT NULL CHECK(bytes_read>=0), read_at INTEGER NOT NULL,
        PRIMARY KEY(offer_message_id,agent_id,instance_id,generation,artifact_hash))""",
    """CREATE TABLE task_authority (
        task_id TEXT NOT NULL, workspace_id TEXT NOT NULL, owner_agent_id TEXT NOT NULL,
        assignment_epoch INTEGER NOT NULL CHECK(assignment_epoch>=1), confirmed_handoff_id TEXT,
        offer_message_id TEXT, accept_message_id TEXT, updated_at INTEGER NOT NULL,
        PRIMARY KEY(task_id,workspace_id))""",
)


def canonical_id(value: str) -> str:
    """Use UUID semantic identity without rewriting immutable envelope bytes."""
    try:
        return str(UUID(value))
    except (ValueError, TypeError, AttributeError):
        raise MailboxError("invalid_identity") from None


def _storage_error(exc: sqlite3.Error) -> MailboxError:
    code = getattr(exc, "sqlite_errorcode", 0) & 255
    codes = {
        sqlite3.SQLITE_BUSY: "storage_busy",
        sqlite3.SQLITE_LOCKED: "storage_busy",
        sqlite3.SQLITE_FULL: "storage_full",
        sqlite3.SQLITE_PERM: "storage_permission",
        sqlite3.SQLITE_READONLY: "storage_permission",
        sqlite3.SQLITE_AUTH: "storage_permission",
    }
    return MailboxError(
        codes.get(code, "storage_error"), retryable=code in (sqlite3.SQLITE_BUSY, sqlite3.SQLITE_LOCKED)
    )


class Database:
    """Open one connection per operation; only explicit initialization creates files."""

    def __init__(self, root: Path, *, authority_id: str | None, tenant_id: str | None, busy_timeout_ms: int = 2000):
        if type(busy_timeout_ms) is not int or not 0 <= busy_timeout_ms <= 2000:
            raise MailboxError("invalid_busy_timeout")
        self.root = Path(root).absolute()
        self.path = self.root / "mailbox.sqlite3"
        self.authority_id = canonical_id(authority_id) if authority_id is not None else None
        self.tenant_id = tenant_id
        self.busy_timeout_ms = busy_timeout_ms

    def _safe_path(self, *, create: bool = False) -> None:
        for path in (self.root, *self.root.parents):
            if path.is_symlink():
                raise MailboxError("unsafe_storage_path")
        if not self.root.exists():
            if not create:
                raise MailboxError("mailbox_not_initialized")
            self.root.mkdir(parents=True, mode=0o700, exist_ok=True)
        info = self.root.stat()
        if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077:
            raise MailboxError("unsafe_storage_path")
        if self.path.is_symlink():
            raise MailboxError("unsafe_storage_path")
        if self.path.exists():
            info = self.path.stat()
            if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077:
                raise MailboxError("unsafe_storage_path")
        elif not create:
            raise MailboxError("mailbox_not_initialized")

    def _open(self, path: Path | None = None) -> sqlite3.Connection:
        conn = sqlite3.connect(
            (path or self.path).as_uri() + "?mode=rw",
            uri=True,
            timeout=self.busy_timeout_ms / 1000,
            isolation_level=None,
        )
        conn.row_factory = sqlite3.Row
        try:
            conn.execute("PRAGMA foreign_keys=ON")
            conn.execute("PRAGMA synchronous=FULL")
            conn.execute(f"PRAGMA busy_timeout={self.busy_timeout_ms}")
        except BaseException:
            conn.close()
            raise
        return conn

    def _validate(self, conn: sqlite3.Connection) -> None:
        version = conn.execute("PRAGMA user_version").fetchone()[0]
        if version not in (SCHEMA_VERSION, 2):
            raise MailboxError("unsupported_schema")
        if version == 2:
            tables = {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            if not {"receiver_bindings", "notifications", "handoff_reads", "task_authority"} <= tables:
                raise MailboxError("unsupported_schema")
        metadata = dict(
            conn.execute("SELECT key,value FROM metadata WHERE key IN ('authority_id','tenant_id','storage_profile')")
        )
        if (
            self.authority_id is not None
            and self.authority_id != metadata.get("authority_id")
            or self.tenant_id is not None
            and self.tenant_id != metadata.get("tenant_id")
        ):
            raise MailboxError("root_identity_mismatch")
        if metadata.get("storage_profile") != "sqlite-local-v1":
            raise MailboxError("unsupported_schema")
        if not {"authority_id", "tenant_id"} <= metadata.keys():
            raise MailboxError("storage_error")
        canonical_id(metadata["authority_id"])
        self.authority_id = metadata["authority_id"]
        self.tenant_id = metadata["tenant_id"]

    @contextmanager
    def _initialization_lock(self):
        anchor = self.root / ".mailbox-init.lock"
        descriptor = os.open(anchor, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
        try:
            info = os.fstat(descriptor)
            if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077:
                raise MailboxError("unsafe_storage_path")
        finally:
            os.close(descriptor)
        deadline = time.monotonic() + self.busy_timeout_ms / 1000
        while True:
            try:
                with file_lock(anchor, blocking=False):
                    yield
                return
            except LockTimeoutError:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise MailboxError("lock_busy", retryable=True) from None
                time.sleep(min(0.01, remaining))

    def initialize(self) -> None:
        """Serialize bootstrap under a permanent bounded initialization anchor."""
        try:
            self._safe_path(create=True)
            if self.path.exists():
                with self.connection():
                    return
            with self._initialization_lock():
                self._initialize_locked()
        except PermissionError:
            raise MailboxError("storage_permission") from None
        except OSError:
            raise MailboxError("storage_error") from None

    def _initialize_locked(self) -> None:
        """Publish a complete bootstrap database without replacing existing storage."""
        temporary = self.root / (".mailbox-init-" + str(uuid4()) + ".sqlite3")
        conn = None
        published = False
        try:
            if self.path.exists():
                with self.connection():
                    return
            fd = os.open(temporary, os.O_CREAT | os.O_EXCL | os.O_WRONLY | os.O_NOFOLLOW, 0o600)
            os.close(fd)
            conn = self._open(temporary)
            conn.execute("PRAGMA journal_mode=WAL")
            conn.executescript("BEGIN IMMEDIATE;\n" + _SCHEMA)
            values = {
                "authority_id": self.authority_id or str(uuid4()),
                "tenant_id": self.tenant_id or "local",
                "storage_profile": "sqlite-local-v1",
                **{key: str(value) for key, value in DEFAULT_QUOTAS.items()},
            }
            conn.executemany("INSERT INTO metadata VALUES (?,?)", values.items())
            conn.execute(f"PRAGMA user_version={SCHEMA_VERSION}")
            conn.commit()
            checkpoint = conn.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchone()
            if checkpoint[0] != 0 or checkpoint[1] != checkpoint[2]:
                raise MailboxError("storage_busy", retryable=True)
            conn.close()
            conn = None
            database_fd = os.open(temporary, os.O_RDONLY | os.O_NOFOLLOW)
            try:
                os.fsync(database_fd)
            finally:
                os.close(database_fd)
            try:
                os.link(temporary, self.path, follow_symlinks=False)
                published = True
            except FileExistsError:
                pass
            directory_fd = os.open(self.root, os.O_RDONLY | os.O_DIRECTORY)
            try:
                os.fsync(directory_fd)
            finally:
                os.close(directory_fd)
            with self.connection():
                pass
        except sqlite3.Error as exc:
            raise _storage_error(exc) from None
        except PermissionError:
            raise MailboxError("commit_unknown" if published else "storage_permission", retryable=published) from None
        except OSError:
            raise MailboxError("commit_unknown" if published else "storage_error", retryable=published) from None
        finally:
            if conn is not None:
                conn.close()
            for path in (temporary, Path(str(temporary) + "-wal"), Path(str(temporary) + "-shm")):
                path.unlink(missing_ok=True)

    @contextmanager
    def connection(self, *, write: bool = False):
        """Use a short snapshot or IMMEDIATE transaction and distinguish uncertain commit."""
        conn = None
        try:
            self._safe_path()
            conn = self._open()
            conn.execute("BEGIN IMMEDIATE" if write else "BEGIN")
            self._validate(conn)
            yield conn
        except BaseException as exc:
            if conn is not None:
                try:
                    conn.rollback()
                except sqlite3.Error:
                    raise MailboxError("storage_error") from None
            if isinstance(exc, sqlite3.Error):
                raise _storage_error(exc) from None
            if isinstance(exc, PermissionError):
                raise MailboxError("storage_permission") from None
            if isinstance(exc, OSError):
                raise MailboxError("storage_error") from None
            raise
        else:
            try:
                conn.commit()
            except sqlite3.Error as exc:
                if conn.in_transaction:
                    try:
                        conn.rollback()
                    except sqlite3.Error:
                        raise MailboxError("commit_unknown", retryable=True) from None
                    raise _storage_error(exc) from None
                raise MailboxError("commit_unknown", retryable=True) from None
        finally:
            if conn is not None:
                conn.close()

    def upgrade_receivers(self) -> None:
        """Explicitly add receiver and handoff records without changing R1 data."""
        with self.connection(write=True) as conn:
            if conn.execute("PRAGMA user_version").fetchone()[0] == 2:
                return
            for statement in _RECEIVER_SCHEMA:
                conn.execute(statement)
            conn.execute("PRAGMA user_version=2")

    def replay(self, conn, agent_id: str, request_id: str, method: str, inputs: dict):
        request_id = canonical_id(request_id)
        input_hash = hashlib.sha256(canonical_bytes(inputs)).hexdigest()
        key = (self.authority_id, self.tenant_id, canonical_id(agent_id), request_id)
        row = conn.execute(
            "SELECT method,input_hash,result FROM mutation_receipts "
            "WHERE authority_id=? AND tenant_id=? AND caller_agent_id=? AND request_id=?",
            key,
        ).fetchone()
        if row is not None:
            if row["method"] != method or row["input_hash"] != input_hash:
                raise MailboxError("request_conflict")
            return json.loads(row["result"]), input_hash
        return None, input_hash

    def save_receipt(self, conn, agent_id: str, request_id: str, method: str, input_hash: str, result: dict):
        conn.execute(
            "INSERT INTO mutation_receipts VALUES (?,?,?,?,?,?,?)",
            (
                self.authority_id,
                self.tenant_id,
                canonical_id(agent_id),
                canonical_id(request_id),
                method,
                input_hash,
                canonical_bytes(result).decode(),
            ),
        )
