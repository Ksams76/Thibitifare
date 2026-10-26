import os
import sqlite3
from contextlib import closing
from concurrent.futures import ThreadPoolExecutor
from tempfile import TemporaryDirectory
import threading
import unittest

from app.storage import StorageConflict, TransactionStore


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
        self.assertEqual(versions, [(1,), (2,), (3,)])

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
        self.assertEqual(versions, [(1,), (2,), (3,)])
        self.assertEqual(stores[0].get_verification("ABC123XYZ"), None)

    def test_idempotency_key_cannot_be_reused_for_different_payment(self):
        key = "12345678-1234-5678-1234-567812345678"
        self.store.reserve_stk_submission(key, PHONE, 250, "Town")

        with self.assertRaises(StorageConflict):
            self.store.reserve_stk_submission(key, PHONE, 300, "Town")

    def test_encryption_key_must_be_32_bytes(self):
        with self.assertRaises(ValueError):
            TransactionStore(self.database_path, "not-a-valid-key")


if __name__ == "__main__":
    unittest.main()
