import os
import sqlite3
from contextlib import closing
from concurrent.futures import ThreadPoolExecutor
from tempfile import TemporaryDirectory
import threading
import unittest

from app.storage import StorageConflict, StorageError, TransactionStore


TEST_KEY = "AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA="
PHONE = "0712345678"


class TransactionStoreTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = TemporaryDirectory()
        self.database_path = os.path.join(self.temp_dir.name, "transactions.sqlite3")
        self.store = TransactionStore(self.database_path, TEST_KEY)
        self.store.initialize()

    def tearDown(self):
        self.temp_dir.cleanup()

    def _create_stk(self, checkout_id="checkout-1", phone=PHONE, amount=250):
        self.store.create_stk_transaction(checkout_id, phone, amount, "Town", None)

    def _complete_stk(self, checkout_id="checkout-1", receipt="ABC123XYZ", phone_matches=True):
        return self.store.apply_stk_callback(
            checkout_id,
            successful=True,
            receipt_number=receipt,
            paid_amount=250,
            phone_matches=phone_matches,
        )

    def test_transaction_and_idempotency_survive_store_reopen(self):
        key = "12345678-1234-5678-1234-567812345678"
        reservation, created = self.store.reserve_stk_submission(
            key,
            PHONE,
            250,
            "Town",
        )
        self.assertTrue(created)
        self.store.create_stk_transaction(
            "checkout-1",
            PHONE,
            250,
            "Town",
            key,
        )

        reopened = TransactionStore(self.database_path, TEST_KEY)
        reopened.initialize()
        transaction = reopened.get_stk_transaction("checkout-1")
        repeated, was_created = reopened.reserve_stk_submission(
            key,
            PHONE,
            250,
            "Town",
        )

        self.assertEqual(transaction["phone_number"], PHONE)
        self.assertEqual(transaction["status"], "pending")
        self.assertFalse(was_created)
        self.assertEqual(repeated["checkout_request_id"], "checkout-1")

    def test_passenger_phone_is_encrypted_and_removed_after_callback(self):
        self._create_stk()
        with closing(sqlite3.connect(self.database_path)) as connection:
            connection.execute("BEGIN")
            encrypted = connection.execute(
                "SELECT phone_ciphertext FROM transactions WHERE provider_reference = ?",
                ("checkout-1",),
            ).fetchone()[0]
            connection.commit()
        self.assertNotIn(PHONE.encode(), encrypted)

        self._complete_stk()
        with closing(sqlite3.connect(self.database_path)) as connection:
            cleared = connection.execute(
                "SELECT phone_ciphertext FROM transactions WHERE provider_reference = ?",
                ("checkout-1",),
            ).fetchone()[0]
        self.assertIsNone(cleared)
        self.assertEqual(self.store.get_stk_transaction("checkout-1")["status"], "success")

    def test_duplicate_callbacks_and_receipt_claim_are_idempotent(self):
        self._create_stk()

        first = self._complete_stk()
        duplicate = self._complete_stk()
        first_claim = self.store.claim_known_receipt("ABC123XYZ", 250)
        second_claim = self.store.claim_known_receipt("ABC123XYZ", 250)

        self.assertEqual(first["status"], "success")
        self.assertEqual(duplicate["status"], "success")
        self.assertEqual(first_claim, ("verified", None))
        self.assertEqual(second_claim, ("already_used", None))

    def test_duplicate_receipt_does_not_complete_another_transaction(self):
        self._create_stk("checkout-1")
        self._create_stk("checkout-2", phone="0711111111")
        self._complete_stk("checkout-1")

        duplicate = self._complete_stk(
            "checkout-2",
            phone_matches=True,
        )

        self.assertEqual(duplicate["status"], "failed")
        self.assertIsNone(duplicate["receipt_number"])

    def test_provider_query_success_can_be_enriched_by_later_callback(self):
        self._create_stk()
        self.store.update_stk_status_from_query("checkout-1", "success")

        completed = self._complete_stk()

        self.assertEqual(completed["status"], "success")
        self.assertEqual(completed["receipt_number"], "ABC123XYZ")

    def test_failed_terminal_state_cannot_be_replaced_by_late_success(self):
        self._create_stk()
        self.store.update_stk_status_from_query(
            "checkout-1",
            "failed",
            "Payment was not completed.",
        )

        result = self._complete_stk()

        self.assertEqual(result["status"], "failed")
        self.assertIsNone(result["receipt_number"])

    def test_verification_result_and_used_code_survive_reopen(self):
        self.store.begin_verification("ABC123XYZ", 250)
        result = self.store.complete_verification(
            "ABC123XYZ",
            "verified",
            actual_amount=250,
        )

        reopened = TransactionStore(self.database_path, TEST_KEY)
        reopened.initialize()
        verification = reopened.get_verification("ABC123XYZ")
        _, _, already_used = reopened.begin_verification("ABC123XYZ", 250)

        self.assertEqual(result["status"], "verified")
        self.assertEqual(verification["status"], "verified")
        self.assertTrue(already_used)

    def test_verification_amount_mismatch_is_persisted(self):
        self.store.begin_verification("ABC123XYZ", 250)

        result = self.store.complete_verification(
            "ABC123XYZ",
            "verified",
            actual_amount=200,
        )

        self.assertEqual(result["status"], "amount_mismatch")
        self.assertEqual(result["expected"], 250)
        self.assertEqual(result["actual"], 200)

    def test_concurrent_receipt_claims_only_allow_one_success(self):
        self._create_stk()
        self._complete_stk()
        barrier = threading.Barrier(2)

        def claim():
            barrier.wait()
            return self.store.claim_known_receipt("ABC123XYZ", 250)[0]

        with ThreadPoolExecutor(max_workers=2) as executor:
            results = list(executor.map(lambda _: claim(), range(2)))

        self.assertCountEqual(results, ["verified", "already_used"])

    def test_concurrent_verification_requests_reserve_only_once(self):
        reopened = TransactionStore(self.database_path, TEST_KEY)
        reopened.initialize()
        barrier = threading.Barrier(2)
        stores = (self.store, reopened)

        def begin(store):
            barrier.wait()
            _, created, already_used = store.begin_verification("ABC123XYZ", 250)
            self.assertFalse(already_used)
            return created

        with ThreadPoolExecutor(max_workers=2) as executor:
            results = list(executor.map(begin, stores))

        self.assertCountEqual(results, [True, False])

    def test_database_migration_preserves_existing_rows(self):
        legacy_path = os.path.join(self.temp_dir.name, "legacy.sqlite3")
        with closing(sqlite3.connect(legacy_path)) as connection:
            connection.execute("BEGIN")
            connection.execute(
                """
                CREATE TABLE schema_migrations (
                    version INTEGER PRIMARY KEY,
                    applied_at TEXT NOT NULL
                )
                """
            )
            connection.commit()
            connection.execute(
                "INSERT INTO schema_migrations (version, applied_at) VALUES (1, 'legacy')"
            )
            connection.execute(
                """
                CREATE TABLE idempotency_keys (
                    idempotency_key TEXT PRIMARY KEY,
                    fingerprint TEXT NOT NULL,
                    state TEXT NOT NULL,
                    checkout_request_id TEXT,
                    reason TEXT,
                    updated_at TEXT NOT NULL,
                    updated_at_epoch REAL NOT NULL
                )
                """
            )
            connection.execute(
                """
                CREATE TABLE transactions (
                    transaction_id TEXT PRIMARY KEY,
                    flow TEXT NOT NULL,
                    provider_reference TEXT NOT NULL,
                    receipt_number TEXT UNIQUE,
                    amount INTEGER NOT NULL,
                    phone_ciphertext BLOB,
                    route TEXT,
                    status TEXT NOT NULL,
                    reason TEXT,
                    idempotency_key TEXT,
                    last_status_query_at REAL,
                    actual_amount INTEGER,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    updated_at_epoch REAL NOT NULL,
                    completed_at TEXT,
                    UNIQUE (flow, provider_reference)
                )
                """
            )
            connection.execute(
                """
                CREATE TABLE used_codes (
                    transaction_code TEXT PRIMARY KEY,
                    transaction_id TEXT NOT NULL,
                    claimed_at TEXT NOT NULL
                )
                """
            )
            connection.execute(
                """
                INSERT INTO transactions (
                    transaction_id, flow, provider_reference, amount, status,
                    created_at, updated_at, updated_at_epoch
                ) VALUES ('legacy-id', 'stk', 'legacy-checkout', 250, 'pending',
                          'legacy-created', 'legacy-updated', 1)
                """
            )
            connection.commit()

        migrated = TransactionStore(legacy_path, TEST_KEY)
        migrated.initialize()
        transaction = migrated.get_stk_transaction("legacy-checkout")

        self.assertEqual(transaction["transaction_id"], "legacy-id")
        self.assertEqual(transaction["amount"], 250)
        self.assertEqual(transaction["currency"], "KES")
        with closing(sqlite3.connect(legacy_path)) as connection:
            versions = connection.execute(
                "SELECT version FROM schema_migrations ORDER BY version"
            ).fetchall()
        self.assertEqual(versions, [(1,), (2,), (3,), (4,), (5,)])

    def test_concurrent_initialization_applies_migrations_once(self):
        database_path = os.path.join(self.temp_dir.name, "concurrent.sqlite3")
        barrier = threading.Barrier(2)

        def initialize_store():
            store = TransactionStore(database_path, TEST_KEY)
            barrier.wait()
            store.initialize()
            return store

        with ThreadPoolExecutor(max_workers=2) as executor:
            stores = list(executor.map(lambda _: initialize_store(), range(2)))

        with closing(sqlite3.connect(database_path)) as connection:
            versions = connection.execute(
                "SELECT version FROM schema_migrations ORDER BY version"
            ).fetchall()
        self.assertEqual(versions, [(1,), (2,), (3,), (4,), (5,)])
        self.assertEqual(stores[0].get_verification("ABC123XYZ"), None)

    def test_idempotency_key_cannot_be_reused_for_different_payment(self):
        key = "12345678-1234-5678-1234-567812345678"
        self.store.reserve_stk_submission(key, PHONE, 250, "Town")

        with self.assertRaises(StorageConflict):
            self.store.reserve_stk_submission(key, PHONE, 300, "Town")

    def test_encryption_key_must_be_32_bytes(self):
        with self.assertRaises(ValueError):
            TransactionStore(self.database_path, "not-a-valid-key")


_V1_DDL = (
    """
    CREATE TABLE idempotency_keys (
        idempotency_key TEXT PRIMARY KEY,
        fingerprint TEXT NOT NULL,
        state TEXT NOT NULL CHECK (state IN ('submitting', 'unknown', 'accepted')),
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
            status IN ('pending', 'success', 'failed', 'verified',
                       'amount_mismatch', 'already_used', 'unknown')
        ),
        reason TEXT,
        idempotency_key TEXT UNIQUE REFERENCES idempotency_keys(idempotency_key),
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
        transaction_id TEXT NOT NULL REFERENCES transactions(transaction_id),
        claimed_at TEXT NOT NULL
    )
    """,
)
_V2_DDL = (
    "ALTER TABLE transactions ADD COLUMN currency TEXT NOT NULL DEFAULT 'KES'",
    "CREATE INDEX transactions_status_created_idx ON transactions(status, created_at)",
    "CREATE INDEX transactions_receipt_status_idx ON transactions(receipt_number, status)",
)
_V3_DDL = (
    """
    CREATE TABLE audit_events (
        event_id TEXT PRIMARY KEY,
        actor_uid TEXT NOT NULL,
        actor_role TEXT,
        http_method TEXT NOT NULL,
        action TEXT NOT NULL CHECK (
            action IN ('stk.create', 'stk.status', 'payment.verify',
                       'verification.status', 'daraja.stk_callback',
                       'daraja.verification_callback', 'daraja.timeout_callback',
                       'audit.read', 'identity.role.assign')
        ),
        subject_uid TEXT,
        response_status INTEGER NOT NULL,
        created_at TEXT NOT NULL
    )
    """,
    "CREATE INDEX audit_events_actor_created_idx ON audit_events(actor_uid, created_at)",
)
_V4_DDL = ("ALTER TABLE idempotency_keys ADD COLUMN owner_uid TEXT",)
_HISTORY = (_V1_DDL, _V2_DDL, _V3_DDL, _V4_DDL)

_KEY_OWNED = "11111111-1111-1111-1111-111111111111"
_KEY_LEGACY = "22222222-2222-2222-2222-222222222222"


class MigrationTests(unittest.TestCase):
    """Runs the real migration code against databases built from older schemas."""

    def setUp(self):
        self.temp_dir = TemporaryDirectory()
        self.addCleanup(self.temp_dir.cleanup)
        self.path = os.path.join(self.temp_dir.name, "legacy.sqlite3")

    def _build(self, version, seed=True):
        with closing(sqlite3.connect(self.path)) as connection:
            connection.execute(
                "CREATE TABLE schema_migrations (version INTEGER PRIMARY KEY, applied_at TEXT NOT NULL)"
            )
            for number, statements in enumerate(_HISTORY[:version], start=1):
                for statement in statements:
                    connection.execute(statement)
                connection.execute(
                    "INSERT INTO schema_migrations VALUES (?, ?)", (number, f"applied-{number}")
                )
            if seed:
                self._seed(connection, version)
            connection.commit()

    def _seed(self, connection, version):
        owner_column = version >= 4
        connection.execute(
            "INSERT INTO idempotency_keys (idempotency_key, fingerprint, state,"
            " checkout_request_id, reason, updated_at, updated_at_epoch"
            + (", owner_uid" if owner_column else "")
            + ") VALUES (?, 'fp-owned', 'accepted', 'ck-success', 'r1', 'u1', 11.5"
            + (", 'conductor-a')" if owner_column else ")"),
            (_KEY_OWNED,),
        )
        connection.execute(
            "INSERT INTO idempotency_keys (idempotency_key, fingerprint, state, reason,"
            " updated_at, updated_at_epoch) VALUES (?, 'fp-legacy', 'unknown', 'lost', 'u2', 12.5)",
            (_KEY_LEGACY,),
        )
        connection.execute(
            "INSERT INTO transactions (transaction_id, flow, provider_reference, receipt_number,"
            " amount, phone_ciphertext, route, status, reason, idempotency_key,"
            " last_status_query_at, actual_amount, created_at, updated_at, updated_at_epoch,"
            " completed_at) VALUES ('tx-stk', 'stk', 'ck-success', 'RCPT1', 250, x'0102',"
            " 'Town', 'success', NULL, ?, 5.5, NULL, 'c1', 'u1', 11.5, 'done1')",
            (_KEY_OWNED,),
        )
        connection.execute(
            "INSERT INTO transactions (transaction_id, flow, provider_reference, amount,"
            " status, reason, actual_amount, created_at, updated_at, updated_at_epoch)"
            " VALUES ('tx-ver', 'verification', 'RCPT1', 250, 'amount_mismatch', 'why', 300,"
            " 'c2', 'u2', 12.5)"
        )
        connection.execute(
            "INSERT INTO used_codes VALUES ('RCPT1', 'tx-stk', 'claimed-1')"
        )
        if version >= 3:
            connection.execute(
                "INSERT INTO audit_events VALUES ('ev-1', 'admin-1', 'admin', 'GET',"
                " 'audit.read', 'subject-1', 200, 'at-1')"
            )
            connection.execute(
                "INSERT INTO audit_events VALUES ('ev-2', 'system:daraja-callback',"
                " 'unverified-callback', 'POST', 'daraja.stk_callback', NULL, 403, 'at-2')"
            )

    def _migrate(self):
        store = TransactionStore(self.path, TEST_KEY)
        store.initialize()
        return store

    def _query(self, sql, params=()):
        with closing(sqlite3.connect(self.path)) as connection:
            return connection.execute(sql, params).fetchall()

    def test_every_historical_version_migrates_to_current(self):
        for version in (1, 2, 3, 4):
            with self.subTest(from_version=version):
                if os.path.exists(self.path):
                    os.remove(self.path)
                self._build(version)
                self._migrate()
                self.assertEqual(
                    self._query("SELECT version FROM schema_migrations ORDER BY version"),
                    [(n,) for n in range(1, 6)],
                )

    def test_existing_transactions_are_preserved_and_ownerless(self):
        self._build(4)
        before = self._query("SELECT * FROM transactions ORDER BY transaction_id")
        columns_before = [r[1] for r in self._query("PRAGMA table_info(transactions)")]

        self._migrate()

        after = self._query(
            "SELECT " + ", ".join(columns_before) + " FROM transactions ORDER BY transaction_id"
        )
        self.assertEqual(after, before)
        self.assertEqual(
            self._query("SELECT transaction_id, owner_uid FROM transactions ORDER BY 1"),
            [("tx-stk", None), ("tx-ver", None)],
        )
        self.assertEqual(self._query("SELECT * FROM used_codes"), [("RCPT1", "tx-stk", "claimed-1")])

    def test_legacy_ownerless_records_are_not_readable_or_inferred(self):
        self._build(4)
        with closing(sqlite3.connect(self.path)) as connection:
            connection.execute(
                "INSERT INTO transactions (transaction_id, flow, provider_reference, amount,"
                " status, created_at, updated_at, updated_at_epoch)"
                " VALUES ('tx-pending', 'verification', 'PENDING1', 250, 'pending', 'c', 'u', 1)"
            )
            connection.execute(
                "INSERT INTO transactions (transaction_id, flow, provider_reference, amount,"
                " status, idempotency_key, created_at, updated_at, updated_at_epoch)"
                " VALUES ('tx-stk-legacy', 'stk', 'ck-legacy', 100, 'pending', ?, 'c', 'u', 1)",
                (_KEY_LEGACY,),
            )
            connection.execute(
                "INSERT INTO transactions (transaction_id, flow, provider_reference, amount,"
                " status, created_at, updated_at, updated_at_epoch)"
                " VALUES ('tx-stk-nokey', 'stk', 'ck-nokey', 100, 'pending', 'c', 'u', 1)"
            )
            connection.commit()
        store = self._migrate()

        # Ownership recorded before the migration is preserved...
        self.assertTrue(store.stk_checkout_owned_by("ck-success", "conductor-a"))
        self.assertFalse(store.stk_checkout_owned_by("ck-success", "conductor-b"))
        # ...but ownerless records stay unowned whatever identifier is supplied.
        for checkout in ("ck-legacy", "ck-nokey"):
            for claimant in ("conductor-a", "", checkout, "tx-stk-legacy", "RCPT1"):
                self.assertFalse(store.stk_checkout_owned_by(checkout, claimant), (checkout, claimant))
        self.assertIsNone(store.get_verification_for_owner("RCPT1", "conductor-a"))
        self.assertIsNone(store.get_verification_for_owner("PENDING1", "conductor-a"))
        self.assertIsNone(store.get_verification_for_owner("RCPT1", ""))
        # A new request for the same code cannot adopt the ownerless record.
        with self.assertRaises(StorageConflict):
            store.begin_verification("PENDING1", 250, "conductor-a")
        self.assertEqual(
            self._query("SELECT owner_uid FROM transactions WHERE transaction_id = 'tx-pending'"),
            [(None,)],
        )

    def test_idempotency_records_and_owners_survive(self):
        self._build(4)
        before = self._query("SELECT * FROM idempotency_keys ORDER BY idempotency_key")

        store = self._migrate()

        self.assertEqual(
            self._query("SELECT * FROM idempotency_keys ORDER BY idempotency_key"), before
        )
        self.assertEqual(store.get_stk_attempt(_KEY_OWNED, "conductor-a")["state"], "accepted")
        self.assertIsNone(store.get_stk_attempt(_KEY_OWNED, "conductor-b"))
        self.assertIsNone(store.get_stk_attempt(_KEY_LEGACY, "conductor-a"))

    def test_pre_v4_idempotency_records_stay_ownerless(self):
        self._build(3)

        self._migrate()

        self.assertEqual(
            self._query("SELECT idempotency_key, owner_uid FROM idempotency_keys ORDER BY 1"),
            [(_KEY_OWNED, None), (_KEY_LEGACY, None)],
        )

    def test_audit_events_survive_with_all_fields(self):
        self._build(3)
        before = self._query("SELECT * FROM audit_events ORDER BY event_id")

        store = self._migrate()

        self.assertEqual(self._query("SELECT * FROM audit_events ORDER BY event_id"), before)
        self.assertEqual(len(before), 2)
        self.assertEqual(len(store.list_audit_events(10, 0)), 2)
        store.record_audit_event("a", "admin", "POST", "stk.attempt.review", 200)
        self.assertEqual(len(self._query("SELECT * FROM audit_events")), 3)

    def test_audit_table_indexes_and_actions_are_rebuilt(self):
        self._build(4)
        self._migrate()

        indexes = {r[0] for r in self._query("SELECT name FROM sqlite_master WHERE type = 'index'")}
        self.assertIn("audit_events_actor_created_idx", indexes)
        tables = {r[0] for r in self._query("SELECT name FROM sqlite_master WHERE type = 'table'")}
        self.assertNotIn("audit_events_v4", tables)
        with closing(sqlite3.connect(self.path)) as connection:
            for action in ("stk.attempt.list", "stk.attempt.review"):
                connection.execute(
                    "INSERT INTO audit_events VALUES (?, 'a', 'admin', 'GET', ?, NULL, 200, 't')",
                    (action, action),
                )
            with self.assertRaises(sqlite3.IntegrityError):
                connection.execute(
                    "INSERT INTO audit_events VALUES ('bad', 'a', NULL, 'GET', 'not.an.action',"
                    " NULL, 200, 't')"
                )

    def test_review_table_has_expected_constraints(self):
        self._build(4)
        self._migrate()

        columns = {r[1] for r in self._query("PRAGMA table_info(stk_attempt_reviews)")}
        self.assertEqual(
            columns,
            {"review_id", "idempotency_key", "admin_uid", "previous_state", "new_state",
             "outcome", "evidence_type", "evidence_reference", "reason", "created_at"},
        )
        insert = (
            "INSERT INTO stk_attempt_reviews VALUES (?, ?, 'admin-1', 'unknown', 'unknown',"
            " ?, 'other', ?, 'a reason', 't')"
        )
        with closing(sqlite3.connect(self.path)) as connection:
            connection.execute("PRAGMA foreign_keys = ON")
            connection.execute(insert, ("r1", _KEY_LEGACY, "no_change_review", "REF-1"))
            with self.assertRaises(sqlite3.IntegrityError):  # duplicate evidence
                connection.execute(insert, ("r2", _KEY_LEGACY, "no_change_review", "REF-1"))
            with self.assertRaises(sqlite3.IntegrityError):  # only review outcomes allowed
                connection.execute(insert, ("r3", _KEY_LEGACY, "success", "REF-2"))
            with self.assertRaises(sqlite3.IntegrityError):  # must reference a real attempt
                connection.execute(
                    insert, ("r4", "99999999-9999-9999-9999-999999999999", "no_change_review", "R")
                )

    def test_reinitialization_is_idempotent(self):
        self._build(4)
        self._migrate()
        snapshot = {
            table: self._query(f"SELECT * FROM {table} ORDER BY 1")
            for table in ("transactions", "idempotency_keys", "used_codes", "schema_migrations")
        }
        schema = self._query("SELECT type, name, sql FROM sqlite_master ORDER BY name")

        self._migrate()
        self._migrate()

        for table, rows in snapshot.items():
            self.assertEqual(self._query(f"SELECT * FROM {table} ORDER BY 1"), rows, table)
        self.assertEqual(self._query("SELECT type, name, sql FROM sqlite_master ORDER BY name"), schema)

    def test_fresh_database_matches_migrated_database_schema(self):
        self._build(4)
        self._migrate()
        fresh_path = os.path.join(self.temp_dir.name, "fresh.sqlite3")
        TransactionStore(fresh_path, TEST_KEY).initialize()

        def shape(path, table):
            with closing(sqlite3.connect(path)) as connection:
                return sorted(
                    (r[1], r[2], r[3]) for r in connection.execute(f"PRAGMA table_info({table})")
                )

        for table in ("transactions", "idempotency_keys", "audit_events", "stk_attempt_reviews"):
            self.assertEqual(shape(self.path, table), shape(fresh_path, table), table)

    def _assert_failed_safely(self, expected_version, expected_tables_before):
        before = {t: self._query(f"SELECT * FROM {t} ORDER BY 1") for t in expected_tables_before}
        with self.assertRaises(sqlite3.Error):
            self._migrate()
        self.assertEqual(
            self._query("SELECT MAX(version) FROM schema_migrations"), [(expected_version,)]
        )
        for table, rows in before.items():
            self.assertEqual(self._query(f"SELECT * FROM {table} ORDER BY 1"), rows, table)

    def test_unexpected_existing_review_table_fails_without_changing_data(self):
        self._build(4)
        with closing(sqlite3.connect(self.path)) as connection:
            connection.execute("CREATE TABLE stk_attempt_reviews (unexpected TEXT)")
            connection.commit()

        self._assert_failed_safely(4, ("transactions", "idempotency_keys", "audit_events", "used_codes"))

        columns = [r[1] for r in self._query("PRAGMA table_info(transactions)")]
        self.assertNotIn("owner_uid", columns)  # the partial ALTER was rolled back
        self.assertEqual(len(self._query("SELECT * FROM audit_events")), 2)

    def test_missing_audit_table_fails_without_changing_data(self):
        self._build(4)
        with closing(sqlite3.connect(self.path)) as connection:
            connection.execute("DROP TABLE audit_events")
            connection.commit()

        self._assert_failed_safely(4, ("transactions", "idempotency_keys", "used_codes"))

        self.assertNotIn("owner_uid", [r[1] for r in self._query("PRAGMA table_info(transactions)")])

    def test_malformed_legacy_audit_rows_fail_without_data_loss(self):
        self._build(4)
        with closing(sqlite3.connect(self.path)) as connection:
            connection.execute("DROP TABLE audit_events")
            connection.execute(
                "CREATE TABLE audit_events (event_id TEXT PRIMARY KEY, actor_uid TEXT)"
            )
            connection.execute("INSERT INTO audit_events VALUES ('ev-x', 'admin-1')")
            connection.commit()

        self._assert_failed_safely(4, ("transactions", "audit_events"))

        self.assertEqual(self._query("SELECT * FROM audit_events"), [("ev-x", "admin-1")])

    def test_database_from_a_newer_schema_is_refused_untouched(self):
        self._build(4)
        with closing(sqlite3.connect(self.path)) as connection:
            connection.execute("INSERT INTO schema_migrations VALUES (99, 'future')")
            connection.commit()
        before = self._query("SELECT * FROM transactions ORDER BY 1")

        with self.assertRaises(StorageError):
            self._migrate()

        self.assertEqual(self._query("SELECT * FROM transactions ORDER BY 1"), before)
        self.assertNotIn("owner_uid", [r[1] for r in self._query("PRAGMA table_info(transactions)")])

    def test_failed_migration_can_be_retried_after_the_cause_is_fixed(self):
        self._build(4)
        with closing(sqlite3.connect(self.path)) as connection:
            connection.execute("CREATE TABLE stk_attempt_reviews (unexpected TEXT)")
            connection.commit()
        with self.assertRaises(sqlite3.Error):
            self._migrate()
        with closing(sqlite3.connect(self.path)) as connection:
            connection.execute("DROP TABLE stk_attempt_reviews")
            connection.commit()

        store = self._migrate()

        self.assertEqual(self._query("SELECT MAX(version) FROM schema_migrations"), [(5,)])
        self.assertEqual(len(store.list_audit_events(10, 0)), 2)


if __name__ == "__main__":
    unittest.main()
