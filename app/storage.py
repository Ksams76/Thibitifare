import base64
import binascii
import hashlib
import hmac
import json
import os
import sqlite3
import time
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Generator
from uuid import uuid4

from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.kdf.hkdf import HKDF


class StorageError(Exception):
    """Raised when transaction data cannot safely be read or written."""


class StorageConflict(StorageError):
    """Raised when a persistent transaction or receipt conflicts with existing data."""


class TransactionStore:
    _SCHEMA_VERSION = 3
    _PHONE_AAD = b"thibitifare:passenger-phone:v1"
    _STALE_SUBMISSION_SECONDS = 60

    def __init__(self, database_path: str, phone_encryption_key: str):
        self.database_path = Path(database_path)
        try:
            encoded_key = phone_encryption_key.encode("ascii")
            master_key = base64.b64decode(
                encoded_key + b"=" * (-len(encoded_key) % 4),
                altchars=b"-_",
                validate=True,
            )
        except (binascii.Error, ValueError, UnicodeEncodeError) as e:
            raise ValueError(
                "THIBITIFARE_PHONE_ENCRYPTION_KEY must be URL-safe base64 for a 32-byte key."
            ) from e
        if len(master_key) != 32:
            raise ValueError(
                "THIBITIFARE_PHONE_ENCRYPTION_KEY must decode to exactly 32 bytes."
            )

        self._phone_cipher = AESGCM(
            self._derive_key(master_key, b"passenger-phone-encryption")
        )
        self._fingerprint_key = self._derive_key(master_key, b"request-fingerprints")
        self._initialized = False

    def initialize(self) -> None:
        self.database_path.parent.mkdir(parents=True, exist_ok=True)
        connection = self._connect()
        try:
            self._enable_wal(connection)
            connection.execute("PRAGMA synchronous = FULL")
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS schema_migrations (
                    version INTEGER PRIMARY KEY,
                    applied_at TEXT NOT NULL
                )
                """
            )
            migrations = {
                1: (
                    """
                    CREATE TABLE idempotency_keys (
                        idempotency_key TEXT PRIMARY KEY,
                        fingerprint TEXT NOT NULL,
                        state TEXT NOT NULL CHECK (
                            state IN ('submitting', 'unknown', 'accepted')
                        ),
                        checkout_request_id TEXT,
                        reason TEXT,
                        updated_at TEXT NOT NULL,
                        updated_at_epoch REAL NOT NULL
                    )
                    """,
                    """
                    CREATE TABLE transactions (
                        transaction_id TEXT PRIMARY KEY,
                        flow TEXT NOT NULL CHECK (flow IN ('stk', 'verification')),
                        provider_reference TEXT NOT NULL,
                        receipt_number TEXT UNIQUE,
                        amount INTEGER NOT NULL CHECK (amount > 0),
                        phone_ciphertext BLOB,
                        route TEXT,
                        status TEXT NOT NULL CHECK (
                            status IN (
                                'pending', 'success', 'failed', 'verified',
                                'amount_mismatch', 'already_used', 'unknown'
                            )
                        ),
                        reason TEXT,
                        idempotency_key TEXT UNIQUE
                            REFERENCES idempotency_keys(idempotency_key),
                        last_status_query_at REAL,
                        actual_amount INTEGER,
                        created_at TEXT NOT NULL,
                        updated_at TEXT NOT NULL,
                        updated_at_epoch REAL NOT NULL,
                        completed_at TEXT,
                        UNIQUE (flow, provider_reference)
                    )
                    """,
                    """
                    CREATE TABLE used_codes (
                        transaction_code TEXT PRIMARY KEY,
                        transaction_id TEXT NOT NULL
                            REFERENCES transactions(transaction_id),
                        claimed_at TEXT NOT NULL
                    )
                    """,
                ),
                2: (
                    """
                    ALTER TABLE transactions
                    ADD COLUMN currency TEXT NOT NULL DEFAULT 'KES'
                    """,
                    """
                    CREATE INDEX transactions_status_created_idx
                    ON transactions(status, created_at)
                    """,
                    """
                    CREATE INDEX transactions_receipt_status_idx
                    ON transactions(receipt_number, status)
                    """,
                ),
                3: (
                    """
                    CREATE TABLE audit_events (
                        event_id TEXT PRIMARY KEY,
                        actor_uid TEXT NOT NULL,
                        actor_role TEXT,
                        http_method TEXT NOT NULL,
                        action TEXT NOT NULL CHECK (
                            action IN (
                                'stk.create', 'stk.status', 'payment.verify',
                                'verification.status', 'daraja.stk_callback',
                                'daraja.verification_callback', 'daraja.timeout_callback',
                                'audit.read', 'identity.role.assign'
                            )
                        ),
                        subject_uid TEXT,
                        response_status INTEGER NOT NULL,
                        created_at TEXT NOT NULL
                    )
                    """,
                    """
                    CREATE INDEX audit_events_actor_created_idx
                    ON audit_events(actor_uid, created_at)
                    """,
                ),
            }

            while True:
                connection.execute("BEGIN IMMEDIATE")
                try:
                    current_version = connection.execute(
                        "SELECT COALESCE(MAX(version), 0) FROM schema_migrations"
                    ).fetchone()[0]
                    if current_version > self._SCHEMA_VERSION:
                        raise StorageError(
                            "The transaction database schema is newer than this application."
                        )
                    if current_version == self._SCHEMA_VERSION:
                        connection.commit()
                        break

                    version = current_version + 1
                    for statement in migrations[version]:
                        connection.execute(statement)
                    connection.execute(
                        "INSERT INTO schema_migrations (version, applied_at) VALUES (?, ?)",
                        (version, self._now()),
                    )
                    connection.commit()
                except Exception:
                    connection.rollback()
                    raise

            self._initialized = True
        finally:
            connection.close()

        if os.name != "nt":
            os.chmod(self.database_path, 0o600)

    def record_audit_event(
        self,
        actor_uid: str,
        actor_role: str | None,
        http_method: str,
        action: str,
        response_status: int,
        subject_uid: str | None = None,
    ) -> None:
        with self._transaction() as connection:
            connection.execute(
                """
                INSERT INTO audit_events (
                    event_id, actor_uid, actor_role, http_method, action,
                    subject_uid, response_status, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    str(uuid4()),
                    actor_uid,
                    actor_role,
                    http_method,
                    action,
                    subject_uid,
                    response_status,
                    self._now(),
                ),
            )

    def list_audit_events(self, limit: int, offset: int) -> list[dict[str, Any]]:
        with self._connection() as connection:
            rows = connection.execute(
                """
                SELECT event_id, actor_uid, actor_role, http_method, action,
                       subject_uid, response_status, created_at
                FROM audit_events
                ORDER BY created_at DESC, event_id DESC
                LIMIT ? OFFSET ?
                """,
                (limit, offset),
            ).fetchall()
        return [dict(row) for row in rows]

    @staticmethod
    def _enable_wal(connection: sqlite3.Connection) -> None:
        deadline = time.monotonic() + 10
        while True:
            try:
                connection.execute("PRAGMA journal_mode = WAL")
                return
            except sqlite3.OperationalError as error:
                if "locked" not in str(error).lower() or time.monotonic() >= deadline:
                    raise StorageError(
                        "Could not enable WAL mode for the transaction database."
                    ) from error
                time.sleep(0.05)

    def reserve_stk_submission(
        self,
        idempotency_key: str,
        phone_number: str,
        amount: int,
        route: str,
    ) -> tuple[dict[str, Any], bool]:
        fingerprint_payload = json.dumps(
            [phone_number, amount, route],
            ensure_ascii=True,
            separators=(",", ":"),
        ).encode("ascii")
        fingerprint = hmac.new(
            self._fingerprint_key,
            fingerprint_payload,
            hashlib.sha256,
        ).hexdigest()

        with self._transaction() as connection:
            row = connection.execute(
                "SELECT * FROM idempotency_keys WHERE idempotency_key = ?",
                (idempotency_key,),
            ).fetchone()
            if row is not None:
                record = dict(row)
                if not hmac.compare_digest(record["fingerprint"], fingerprint):
                    raise StorageConflict(
                        "This request key was already used with different payment details."
                    )

                if (
                    record["state"] == "submitting"
                    and time.time() - record["updated_at_epoch"] > self._STALE_SUBMISSION_SECONDS
                ):
                    reason = (
                        "The payment request outcome is unknown. Check the passenger's phone before trying again."
                    )
                    now = time.time()
                    connection.execute(
                        """
                        UPDATE idempotency_keys
                        SET state = 'unknown', reason = ?, updated_at = ?, updated_at_epoch = ?
                        WHERE idempotency_key = ? AND state = 'submitting'
                        """,
                        (reason, self._now(), now, idempotency_key),
                    )
                    record["state"] = "unknown"
                    record["reason"] = reason
                return record, False

            now = time.time()
            connection.execute(
                """
                INSERT INTO idempotency_keys (
                    idempotency_key, fingerprint, state, updated_at, updated_at_epoch
                ) VALUES (?, ?, 'submitting', ?, ?)
                """,
                (idempotency_key, fingerprint, self._now(), now),
            )
            return {
                "idempotency_key": idempotency_key,
                "fingerprint": fingerprint,
                "state": "submitting",
                "checkout_request_id": None,
                "reason": None,
            }, True

    def release_stk_submission(self, idempotency_key: str) -> None:
        with self._transaction() as connection:
            connection.execute(
                """
                DELETE FROM idempotency_keys
                WHERE idempotency_key = ? AND state = 'submitting'
                """,
                (idempotency_key,),
            )

    def mark_stk_submission_unknown(self, idempotency_key: str, reason: str) -> None:
        with self._transaction() as connection:
            now = time.time()
            connection.execute(
                """
                UPDATE idempotency_keys
                SET state = 'unknown', reason = ?, updated_at = ?, updated_at_epoch = ?
                WHERE idempotency_key = ? AND state = 'submitting'
                """,
                (reason, self._now(), now, idempotency_key),
            )

    def create_stk_transaction(
        self,
        checkout_request_id: str,
        phone_number: str,
        amount: int,
        route: str,
        idempotency_key: str | None,
    ) -> None:
        now = time.time()
        timestamp = self._now()
        nonce = os.urandom(12)
        phone_ciphertext = nonce + self._phone_cipher.encrypt(
            nonce,
            phone_number.encode("utf-8"),
            self._PHONE_AAD,
        )

        try:
            with self._transaction() as connection:
                if idempotency_key:
                    reservation = connection.execute(
                        "SELECT state FROM idempotency_keys WHERE idempotency_key = ?",
                        (idempotency_key,),
                    ).fetchone()
                    if reservation is None or reservation["state"] != "submitting":
                        raise StorageConflict("The STK request reservation is no longer active.")

                connection.execute(
                    """
                    INSERT INTO transactions (
                        transaction_id, flow, provider_reference, amount,
                        phone_ciphertext, route, status, idempotency_key,
                        created_at, updated_at, updated_at_epoch
                    ) VALUES (?, 'stk', ?, ?, ?, ?, 'pending', ?, ?, ?, ?)
                    """,
                    (
                        uuid4().hex,
                        checkout_request_id,
                        amount,
                        phone_ciphertext,
                        route,
                        idempotency_key,
                        timestamp,
                        timestamp,
                        now,
                    ),
                )
                if idempotency_key:
                    connection.execute(
                        """
                        UPDATE idempotency_keys
                        SET state = 'accepted', checkout_request_id = ?,
                            reason = NULL, updated_at = ?, updated_at_epoch = ?
                        WHERE idempotency_key = ? AND state = 'submitting'
                        """,
                        (checkout_request_id, timestamp, now, idempotency_key),
                    )
        except sqlite3.IntegrityError as e:
            raise StorageConflict(
                "The provider checkout reference or idempotency key is already recorded."
            ) from e

    def get_stk_transaction(self, checkout_request_id: str) -> dict[str, Any] | None:
        with self._connection() as connection:
            row = connection.execute(
                """
                SELECT * FROM transactions
                WHERE flow = 'stk' AND provider_reference = ?
                """,
                (checkout_request_id,),
            ).fetchone()
            return self._transaction_record(row) if row is not None else None

    def claim_stk_status_query(self, checkout_request_id: str, interval: float) -> bool:
        now = time.time()
        with self._transaction() as connection:
            cursor = connection.execute(
                """
                UPDATE transactions
                SET last_status_query_at = ?, updated_at = ?, updated_at_epoch = ?
                WHERE flow = 'stk' AND provider_reference = ? AND status = 'pending'
                  AND (
                    last_status_query_at IS NULL
                    OR last_status_query_at <= ?
                  )
                """,
                (now, self._now(), now, checkout_request_id, now - interval),
            )
            return cursor.rowcount == 1

    def update_stk_status_from_query(
        self,
        checkout_request_id: str,
        status: str,
        reason: str | None = None,
    ) -> None:
        if status not in {"success", "failed"}:
            raise ValueError("STK provider query status must be terminal.")
        self._update_pending_stk(checkout_request_id, status, reason)

    def apply_stk_callback(
        self,
        checkout_request_id: str,
        successful: bool,
        receipt_number: str | None = None,
        paid_amount: int | None = None,
        phone_matches: bool = False,
    ) -> dict[str, Any] | None:
        with self._transaction() as connection:
            row = connection.execute(
                """
                SELECT * FROM transactions
                WHERE flow = 'stk' AND provider_reference = ?
                """,
                (checkout_request_id,),
            ).fetchone()
            if row is None:
                return None
            record = self._transaction_record(row)

            if record["status"] == "failed":
                return record
            if not successful:
                if record["status"] == "pending":
                    self._finish_stk_callback(
                        connection,
                        checkout_request_id,
                        "failed",
                        "Payment was not completed.",
                        None,
                    )
                return self._load_stk_record(connection, checkout_request_id)

            normalized_receipt = receipt_number.strip().upper() if receipt_number else None
            valid = (
                record["amount"] == paid_amount
                and phone_matches
                and normalized_receipt is not None
                and bool(normalized_receipt)
            )
            if not valid:
                if record["status"] == "pending":
                    self._finish_stk_callback(
                        connection,
                        checkout_request_id,
                        "failed",
                        "Payment details could not be confirmed.",
                        None,
                    )
                return self._load_stk_record(connection, checkout_request_id)

            existing_receipt = connection.execute(
                "SELECT transaction_id FROM transactions WHERE receipt_number = ?",
                (normalized_receipt,),
            ).fetchone()
            if existing_receipt and existing_receipt["transaction_id"] != record["transaction_id"]:
                if record["status"] == "pending":
                    self._finish_stk_callback(
                        connection,
                        checkout_request_id,
                        "failed",
                        "Payment receipt was already recorded.",
                        None,
                    )
                return self._load_stk_record(connection, checkout_request_id)

            if record["receipt_number"] is not None:
                return record

            self._finish_stk_callback(
                connection,
                checkout_request_id,
                "success",
                None,
                normalized_receipt,
            )
            return self._load_stk_record(connection, checkout_request_id)

    def claim_known_receipt(self, receipt_number: str, amount: int) -> tuple[str, int | None]:
        normalized_receipt = receipt_number.strip().upper()
        with self._transaction() as connection:
            transaction = connection.execute(
                """
                SELECT transaction_id, amount FROM transactions
                WHERE flow = 'stk' AND receipt_number = ? AND status = 'success'
                """,
                (normalized_receipt,),
            ).fetchone()
            if transaction is None:
                return "not_found", None
            if transaction["amount"] != amount:
                return "amount_mismatch", transaction["amount"]

            existing = connection.execute(
                "SELECT 1 FROM used_codes WHERE transaction_code = ?",
                (normalized_receipt,),
            ).fetchone()
            if existing is not None:
                return "already_used", None

            connection.execute(
                """
                INSERT INTO used_codes (transaction_code, transaction_id, claimed_at)
                VALUES (?, ?, ?)
                """,
                (normalized_receipt, transaction["transaction_id"], self._now()),
            )
            return "verified", None

    def begin_verification(
        self,
        transaction_code: str,
        expected_amount: int,
    ) -> tuple[dict[str, Any] | None, bool, bool]:
        normalized_code = transaction_code.strip().upper()
        with self._transaction() as connection:
            used = connection.execute(
                "SELECT 1 FROM used_codes WHERE transaction_code = ?",
                (normalized_code,),
            ).fetchone()
            if used is not None:
                return None, False, True

            row = connection.execute(
                """
                SELECT * FROM transactions
                WHERE flow = 'verification' AND provider_reference = ?
                """,
                (normalized_code,),
            ).fetchone()
            if row is not None:
                record = self._verification_status(row)
                if record["amount"] != expected_amount:
                    raise StorageConflict(
                        "A verification for this code is already recorded with a different amount."
                    )
                return record, False, False

            now = time.time()
            timestamp = self._now()
            transaction_id = uuid4().hex
            connection.execute(
                """
                INSERT INTO transactions (
                    transaction_id, flow, provider_reference, amount, status,
                    created_at, updated_at, updated_at_epoch
                ) VALUES (?, 'verification', ?, ?, 'pending', ?, ?, ?)
                """,
                (transaction_id, normalized_code, expected_amount, timestamp, timestamp, now),
            )
            row = connection.execute(
                "SELECT * FROM transactions WHERE transaction_id = ?",
                (transaction_id,),
            ).fetchone()
            return self._verification_status(row), True, False

    def get_verification(self, transaction_code: str) -> dict[str, Any] | None:
        normalized_code = transaction_code.strip().upper()
        with self._connection() as connection:
            row = connection.execute(
                """
                SELECT * FROM transactions
                WHERE flow = 'verification' AND provider_reference = ?
                """,
                (normalized_code,),
            ).fetchone()
            return self._verification_status(row) if row is not None else None

    def complete_verification(
        self,
        transaction_code: str,
        status: str,
        actual_amount: int | None = None,
        reason: str | None = None,
    ) -> dict[str, Any] | None:
        if status not in {"verified", "failed"}:
            raise ValueError("Verification completion status must be verified or failed.")
        normalized_code = transaction_code.strip().upper()
        with self._transaction() as connection:
            row = connection.execute(
                """
                SELECT * FROM transactions
                WHERE flow = 'verification' AND provider_reference = ?
                """,
                (normalized_code,),
            ).fetchone()
            if row is None:
                return None
            record = self._transaction_record(row)
            if record["status"] != "pending":
                return self._verification_status(row)

            final_status = status
            final_reason = reason
            expected = record["amount"]
            if status == "verified":
                if actual_amount is None:
                    final_status = "failed"
                    final_reason = "Safaricom's response did not include a verifiable amount."
                elif actual_amount != expected:
                    final_status = "amount_mismatch"
                else:
                    used = connection.execute(
                        "SELECT 1 FROM used_codes WHERE transaction_code = ?",
                        (normalized_code,),
                    ).fetchone()
                    if used is not None:
                        final_status = "already_used"
                    else:
                        connection.execute(
                            """
                            INSERT INTO used_codes (transaction_code, transaction_id, claimed_at)
                            VALUES (?, ?, ?)
                            """,
                            (normalized_code, record["transaction_id"], self._now()),
                        )

            completed_at = self._now()
            now = time.time()
            connection.execute(
                """
                UPDATE transactions
                SET status = ?, reason = ?, actual_amount = ?, updated_at = ?,
                    updated_at_epoch = ?, completed_at = ?
                WHERE transaction_id = ? AND status = 'pending'
                """,
                (
                    final_status,
                    final_reason,
                    actual_amount,
                    completed_at,
                    now,
                    completed_at,
                    record["transaction_id"],
                ),
            )
            updated = connection.execute(
                "SELECT * FROM transactions WHERE transaction_id = ?",
                (record["transaction_id"],),
            ).fetchone()
            return self._verification_status(updated)

    def _update_pending_stk(
        self,
        checkout_request_id: str,
        status: str,
        reason: str | None,
    ) -> None:
        with self._transaction() as connection:
            self._finish_stk_callback(
                connection,
                checkout_request_id,
                status,
                reason,
                None,
                clear_phone=status == "failed",
            )

    def _finish_stk_callback(
        self,
        connection: sqlite3.Connection,
        checkout_request_id: str,
        status: str,
        reason: str | None,
        receipt_number: str | None,
        clear_phone: bool = True,
    ) -> None:
        now = self._now()
        epoch = time.time()
        connection.execute(
            """
            UPDATE transactions
            SET status = ?, reason = ?, receipt_number = ?,
                phone_ciphertext = CASE WHEN ? THEN NULL ELSE phone_ciphertext END,
                updated_at = ?, updated_at_epoch = ?,
                completed_at = COALESCE(completed_at, ?)
            WHERE flow = 'stk' AND provider_reference = ?
              AND (
                status = 'pending'
                OR (? = 1 AND status = 'success' AND receipt_number IS NULL)
              )
            """,
            (
                status,
                reason,
                receipt_number,
                1 if clear_phone else 0,
                now,
                epoch,
                now,
                checkout_request_id,
                1 if status == "success" and receipt_number is not None else 0,
            ),
        )

    def _load_stk_record(
        self,
        connection: sqlite3.Connection,
        checkout_request_id: str,
    ) -> dict[str, Any] | None:
        row = connection.execute(
            """
            SELECT * FROM transactions
            WHERE flow = 'stk' AND provider_reference = ?
            """,
            (checkout_request_id,),
        ).fetchone()
        return self._transaction_record(row) if row is not None else None

    def _transaction_record(self, row: sqlite3.Row) -> dict[str, Any]:
        record = dict(row)
        encrypted_phone = record.pop("phone_ciphertext", None)
        record["phone_number"] = (
            self._decrypt_phone(encrypted_phone) if encrypted_phone is not None else None
        )
        return record

    def _verification_status(self, row: sqlite3.Row) -> dict[str, Any]:
        record = self._transaction_record(row)
        return {
            "status": record["status"],
            "amount": record["amount"],
            "expected_amount": record["amount"],
            "expected": record["amount"] if record["status"] == "amount_mismatch" else None,
            "actual": record["actual_amount"],
            "reason": record["reason"],
            "created_at": record["created_at"],
        }

    def _decrypt_phone(self, encrypted_phone: bytes) -> str:
        nonce, ciphertext = encrypted_phone[:12], encrypted_phone[12:]
        try:
            plaintext = self._phone_cipher.decrypt(nonce, ciphertext, self._PHONE_AAD)
        except (InvalidTag, UnicodeDecodeError) as e:
            raise StorageError(
                "Could not decrypt a stored phone number; verify the configured encryption key."
            ) from e
        return plaintext.decode("utf-8")

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.database_path, timeout=10, isolation_level=None)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("PRAGMA busy_timeout = 10000")
        connection.execute("PRAGMA synchronous = FULL")
        return connection

    @contextmanager
    def _connection(self) -> Generator[sqlite3.Connection, None, None]:
        if not self._initialized:
            raise StorageError("Transaction storage has not been initialized.")
        connection = self._connect()
        try:
            yield connection
        finally:
            connection.close()

    @contextmanager
    def _transaction(self) -> Generator[sqlite3.Connection, None, None]:
        with self._connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                yield connection
                connection.commit()
            except sqlite3.IntegrityError:
                connection.rollback()
                raise
            except sqlite3.Error as e:
                connection.rollback()
                raise StorageError("The transaction database operation failed.") from e
            except Exception:
                connection.rollback()
                raise

    @staticmethod
    def _now() -> str:
        return datetime.now(timezone.utc).isoformat()

    @staticmethod
    def _derive_key(master_key: bytes, purpose: bytes) -> bytes:
        return HKDF(
            algorithm=hashes.SHA256(),
            length=32,
            salt=b"thibitifare:storage:v1",
            info=purpose,
        ).derive(master_key)
