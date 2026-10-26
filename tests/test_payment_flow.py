import os
import sqlite3
from contextlib import closing
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import AsyncMock, Mock, patch

from fastapi.testclient import TestClient

os.environ["DARAJA_CONSUMER_KEY"] = "unit-test-key"
os.environ["DARAJA_CONSUMER_SECRET"] = "unit-test-secret"
os.environ["DARAJA_PASSKEY"] = "unit-test-passkey"
os.environ["DARAJA_CALLBACK_URL"] = "https://unit-test.invalid"
os.environ["DARAJA_ENV"] = "sandbox"
os.environ["DARAJA_VERIFY_STUB"] = "true"
os.environ["DARAJA_INITIATOR_NAME"] = "unit-test-initiator"
os.environ["DARAJA_INITIATOR_PASSWORD"] = "unit-test-password"
os.environ["THIBITIFARE_PHONE_ENCRYPTION_KEY"] = "AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA="

from app import main
from app.auth import (
    FirebaseAuthUnavailable,
    InvalidFirebaseToken,
    set_firebase_user_role,
    verify_firebase_id_token,
)
from app.storage import TransactionStore

IDEMPOTENCY_HEADERS = {"Idempotency-Key": "12345678-1234-5678-1234-567812345678"}


class PaymentFlowTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = TemporaryDirectory()
        self.original_store = main.store
        main.store = TransactionStore(
            os.path.join(self.temp_dir.name, "transactions.sqlite3"),
            os.environ["THIBITIFARE_PHONE_ENCRYPTION_KEY"],
        )
        main.store.initialize()
        auth_patch = patch(
            "app.auth.verify_firebase_id_token",
            return_value={"uid": "test-conductor", "role": "conductor"},
        )
        auth_patch.start()
        self.addCleanup(auth_patch.stop)
        self.client = TestClient(
            main.app,
            headers={"Authorization": "Bearer test-id-token"},
        )

    def tearDown(self):
        main.store = self.original_store
        self.temp_dir.cleanup()

    def _callback(self, checkout_id, result_code=0, items=None):
        return self.client.post(
            "/daraja/callback",
            json={
                "Body": {
                    "stkCallback": {
                        "CheckoutRequestID": checkout_id,
                        "ResultCode": result_code,
                        "CallbackMetadata": {"Item": items or []},
                    }
                }
            },
        )

    def _submit_stk(self, payload):
        return self.client.post(
            "/stk-push",
            json=payload,
            headers=IDEMPOTENCY_HEADERS,
        )

    def _pending_stk(self, checkout_id="checkout-1", amount=250, phone="0712345678"):
        main.store.create_stk_transaction(checkout_id, phone, amount, "Town", None)

    def _successful_items(self, receipt="ABC123XYZ", amount=250, phone=254712345678):
        return [
            {"Name": "MpesaReceiptNumber", "Value": receipt},
            {"Name": "Amount", "Value": amount},
            {"Name": "PhoneNumber", "Value": phone},
        ]

    def test_stk_rejects_invalid_phone_amount_and_blank_route(self):
        with patch.object(main.daraja, "stk_push", new_callable=AsyncMock) as push:
            for payload in (
                {"phone_number": "0212345678", "amount": 100, "route": "Town"},
                {"phone_number": "0712345678", "amount": 0, "route": "Town"},
                {"phone_number": "0712345678", "amount": 100, "route": "   "},
            ):
                response = self._submit_stk(payload)
                self.assertEqual(response.status_code, 422)
            push.assert_not_awaited()

    def test_stk_submission_stays_pending_until_provider_confirms(self):
        with patch.object(main.daraja, "stk_push", new_callable=AsyncMock) as push:
            push.return_value = {
                "ResponseCode": "0",
                "CheckoutRequestID": "checkout-1",
            }
            response = self._submit_stk(
                {"phone_number": "0712345678", "amount": 250, "route": " Town "}
            )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["status"], "pending")
        transaction = main.store.get_stk_transaction("checkout-1")
        self.assertEqual(transaction["route"], "Town")

    def test_stk_requires_idempotency_key(self):
        with patch.object(main.daraja, "stk_push", new_callable=AsyncMock) as push:
            response = self.client.post(
                "/stk-push",
                json={"phone_number": "0712345678", "amount": 250, "route": "Town"},
            )

        self.assertEqual(response.status_code, 422)
        push.assert_not_awaited()

    def test_application_lifespan_initializes_the_database(self):
        uninitialized_store = TransactionStore(
            os.path.join(self.temp_dir.name, "startup.sqlite3"),
            os.environ["THIBITIFARE_PHONE_ENCRYPTION_KEY"],
        )
        main.store = uninitialized_store

        with TestClient(main.app) as client:
            response = client.get("/")

        self.assertEqual(response.status_code, 200)
        self.assertTrue(os.path.exists(uninitialized_store.database_path))

    def test_payment_routes_reject_requests_without_bearer_token(self):
        response = self.client.post(
            "/verify",
            json={"transaction_code": "ABC123XYZ", "amount": 250},
            headers={"Authorization": ""},
        )

        self.assertEqual(response.status_code, 401)

    def test_firebase_identity_without_a_trusted_role_is_forbidden(self):
        with patch(
            "app.auth.verify_firebase_id_token",
            return_value={"uid": "unassigned-user"},
        ):
            response = self.client.get(
                "/stk-push/checkout-1/status",
                headers={"Authorization": "Bearer unassigned-token"},
            )

        self.assertEqual(response.status_code, 403)

    def test_invalid_firebase_token_is_rejected(self):
        with patch(
            "app.auth.verify_firebase_id_token",
            side_effect=InvalidFirebaseToken,
        ):
            response = self.client.get(
                "/stk-push/checkout-1/status",
                headers={"Authorization": "Bearer invalid-token"},
            )

        self.assertEqual(response.status_code, 401)

    def test_firebase_verification_outage_fails_closed(self):
        with patch(
            "app.auth.verify_firebase_id_token",
            side_effect=FirebaseAuthUnavailable,
        ):
            response = self.client.get(
                "/stk-push/checkout-1/status",
                headers={"Authorization": "Bearer test-token"},
            )

        self.assertEqual(response.status_code, 503)

    def test_firebase_verification_checks_revocation(self):
        with (
            patch("app.auth.get_firebase_app") as firebase_app,
            patch(
                "app.auth.auth.verify_id_token",
                return_value={"uid": "test-conductor"},
            ) as verify_token,
        ):
            claims = verify_firebase_id_token("test-token")

        self.assertEqual(claims["uid"], "test-conductor")
        verify_token.assert_called_once_with(
            "test-token",
            app=firebase_app.return_value,
            check_revoked=True,
        )

    def test_firebase_role_updates_preserve_claims_and_revoke_tokens(self):
        firebase_user = Mock(
            uid="firebase-user-1",
            custom_claims={"department": "north"},
        )
        with (
            patch("app.auth.get_firebase_app") as firebase_app,
            patch("app.auth.auth.get_user", return_value=firebase_user),
            patch("app.auth.auth.set_custom_user_claims") as set_claims,
            patch("app.auth.auth.revoke_refresh_tokens") as revoke_tokens,
        ):
            set_firebase_user_role("firebase-user-1", "conductor")

        set_claims.assert_called_once_with(
            "firebase-user-1",
            {"department": "north", "role": "conductor"},
            app=firebase_app.return_value,
        )
        revoke_tokens.assert_called_once_with(
            "firebase-user-1",
            app=firebase_app.return_value,
        )

    def test_firebase_role_removal_clears_only_the_role_claim(self):
        firebase_user = Mock(
            uid="firebase-user-1",
            custom_claims={"department": "north", "role": "admin"},
        )
        with (
            patch("app.auth.get_firebase_app") as firebase_app,
            patch("app.auth.auth.get_user", return_value=firebase_user),
            patch("app.auth.auth.set_custom_user_claims") as set_claims,
            patch("app.auth.auth.revoke_refresh_tokens"),
        ):
            set_firebase_user_role("firebase-user-1", "none")

        set_claims.assert_called_once_with(
            "firebase-user-1",
            {"department": "north"},
            app=firebase_app.return_value,
        )

    def test_auth_config_does_not_expose_server_credentials(self):
        response = self.client.get("/auth/config")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(set(response.json()), {"enabled", "firebase"})
        self.assertEqual(
            set(response.json()["firebase"]),
            {"apiKey", "authDomain", "projectId", "appId"},
        )

    def test_authenticated_verification_request_is_audited_without_payload_data(self):
        response = self.client.post(
            "/verify",
            json={"transaction_code": "ABC123XYZ", "amount": 250},
        )

        self.assertEqual(response.status_code, 200)
        with closing(sqlite3.connect(main.store.database_path)) as connection:
            event = connection.execute(
                """
                SELECT actor_uid, actor_role, http_method, action, response_status
                FROM audit_events
                """
            ).fetchone()
            columns = {
                row[1]
                for row in connection.execute("PRAGMA table_info(audit_events)")
            }
        self.assertEqual(
            event,
            ("test-conductor", "conductor", "POST", "payment.verify", 200),
        )
        self.assertNotIn("transaction_code", columns)

    def test_audit_log_is_admin_only(self):
        response = self.client.get("/admin/audit-events")
        self.assertEqual(response.status_code, 403)

        with patch(
            "app.auth.verify_firebase_id_token",
            return_value={"uid": "test-admin", "role": "admin"},
        ):
            admin_response = self.client.get("/admin/audit-events")

        self.assertEqual(admin_response.status_code, 200)
        self.assertEqual(
            admin_response.json()["events"][0]["response_status"],
            403,
        )

    def test_admin_role_assignment_is_authorized_and_audited(self):
        with (
            patch(
                "app.auth.verify_firebase_id_token",
                return_value={"uid": "test-admin", "role": "admin"},
            ),
            patch("app.main.set_firebase_user_role") as set_role,
        ):
            response = self.client.post(
                "/admin/users/firebase-user-2/role",
                json={"role": "conductor"},
            )

        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.json()["requires_sign_in"])
        set_role.assert_called_once_with("firebase-user-2", "conductor")
        with closing(sqlite3.connect(main.store.database_path)) as connection:
            event = connection.execute(
                """
                SELECT actor_uid, actor_role, action, subject_uid, response_status
                FROM audit_events
                """
            ).fetchone()
        self.assertEqual(
            event,
            (
                "test-admin",
                "admin",
                "identity.role.assign",
                "firebase-user-2",
                200,
            ),
        )

    def test_daraja_callback_remains_public_and_is_audited_as_provider_event(self):
        response = self._callback("unknown-checkout")

        self.assertEqual(response.status_code, 200)
        with closing(sqlite3.connect(main.store.database_path)) as connection:
            event = connection.execute(
                """
                SELECT actor_uid, actor_role, action, response_status
                FROM audit_events
                """
            ).fetchone()
        self.assertEqual(
            event,
            (
                "system:daraja-callback",
                "unverified-callback",
                "daraja.stk_callback",
                200,
            ),
        )

    def test_stk_provider_rejection_is_not_reported_as_pending(self):
        with patch.object(main.daraja, "stk_push", new_callable=AsyncMock) as push:
            push.return_value = {
                "ResponseCode": "1",
                "ResponseDescription": "Request rejected.",
            }
            response = self._submit_stk(
                {"phone_number": "0712345678", "amount": 250, "route": "Town"}
            )

        self.assertEqual(response.status_code, 502)
        self.assertIsNone(main.store.get_stk_transaction("checkout-1"))

    def test_stk_retry_with_same_key_reuses_checkout_request(self):
        headers = {"Idempotency-Key": "12345678-1234-5678-1234-567812345678"}
        payload = {"phone_number": "0712345678", "amount": 250, "route": "Town"}
        with patch.object(main.daraja, "stk_push", new_callable=AsyncMock) as push:
            push.return_value = {
                "ResponseCode": "0",
                "CheckoutRequestID": "checkout-1",
            }
            first = self.client.post("/stk-push", json=payload, headers=headers)
            second = self.client.post("/stk-push", json=payload, headers=headers)

        self.assertEqual(first.json(), second.json())
        push.assert_awaited_once()

    def test_stk_idempotency_key_cannot_be_reused_for_different_payment(self):
        headers = {"Idempotency-Key": "12345678-1234-5678-1234-567812345678"}
        with patch.object(main.daraja, "stk_push", new_callable=AsyncMock) as push:
            push.return_value = {
                "ResponseCode": "0",
                "CheckoutRequestID": "checkout-1",
            }
            self.client.post(
                "/stk-push",
                json={"phone_number": "0712345678", "amount": 250, "route": "Town"},
                headers=headers,
            )
            response = self.client.post(
                "/stk-push",
                json={"phone_number": "0712345678", "amount": 300, "route": "Town"},
                headers=headers,
            )

        self.assertEqual(response.status_code, 409)
        push.assert_awaited_once()

    def test_stk_timeout_retry_does_not_send_a_second_prompt(self):
        headers = {"Idempotency-Key": "12345678-1234-5678-1234-567812345678"}
        payload = {"phone_number": "0712345678", "amount": 250, "route": "Town"}
        with patch.object(
            main.daraja,
            "stk_push",
            new_callable=AsyncMock,
            side_effect=main.daraja.DarajaOutcomeUnknown("Outcome unknown."),
        ) as push:
            first = self.client.post("/stk-push", json=payload, headers=headers)
            second = self.client.post("/stk-push", json=payload, headers=headers)

        self.assertEqual(first.status_code, 504)
        self.assertEqual(second.status_code, 504)
        push.assert_awaited_once()

    def test_stk_timeout_warns_that_the_outcome_is_unknown(self):
        with patch.object(
            main.daraja,
            "stk_push",
            new_callable=AsyncMock,
            side_effect=main.daraja.DarajaOutcomeUnknown("Outcome unknown."),
        ):
            response = self._submit_stk(
                {"phone_number": "0712345678", "amount": 250, "route": "Town"}
            )

        self.assertEqual(response.status_code, 504)
        self.assertIn("Outcome unknown", response.json()["detail"])

    def test_success_callback_requires_matching_amount_and_phone(self):
        self._pending_stk()

        response = self._callback(
            "checkout-1",
            items=self._successful_items(amount=200),
        )

        self.assertEqual(response.status_code, 200)
        transaction = main.store.get_stk_transaction("checkout-1")
        self.assertEqual(transaction["status"], "failed")
        self.assertIsNone(transaction["receipt_number"])

    def test_success_callback_records_receipt_once_and_is_idempotent(self):
        self._pending_stk()

        first = self._callback("checkout-1", items=self._successful_items())
        second = self._callback("checkout-1", items=self._successful_items())
        status = self.client.get("/stk-push/checkout-1/status")

        self.assertEqual(first.status_code, 200)
        self.assertEqual(second.status_code, 200)
        transaction = main.store.get_stk_transaction("checkout-1")
        self.assertEqual(transaction["status"], "success")
        self.assertEqual(transaction["receipt_number"], "ABC123XYZ")
        self.assertEqual(status.json(), {"status": "success"})

    def test_duplicate_receipt_cannot_mark_another_request_successful(self):
        self._pending_stk("checkout-1")
        self._pending_stk("checkout-2", phone="0711111111")
        self._callback("checkout-1", items=self._successful_items())

        self._callback(
            "checkout-2",
            items=self._successful_items(phone=254711111111),
        )

        transaction = main.store.get_stk_transaction("checkout-2")
        self.assertEqual(transaction["status"], "failed")
        self.assertEqual(
            main.store.claim_known_receipt("ABC123XYZ", 250)[0],
            "verified",
        )

    def test_status_query_success_must_match_checkout_request(self):
        self._pending_stk()
        with patch.object(main.daraja, "query_stk_status", new_callable=AsyncMock) as query:
            query.return_value = {
                "CheckoutRequestID": "another-checkout",
                "ResultCode": 0,
            }
            response = self.client.get("/stk-push/checkout-1/status")

        self.assertEqual(response.status_code, 502)
        self.assertEqual(main.store.get_stk_transaction("checkout-1")["status"], "pending")

    def test_matching_status_query_can_confirm_payment(self):
        self._pending_stk()
        with patch.object(main.daraja, "query_stk_status", new_callable=AsyncMock) as query:
            query.return_value = {
                "CheckoutRequestID": "checkout-1",
                "ResultCode": "0",
            }
            response = self.client.get("/stk-push/checkout-1/status")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["status"], "success")

    def test_status_polling_is_throttled_per_checkout(self):
        self._pending_stk()
        with patch.object(main.daraja, "query_stk_status", new_callable=AsyncMock) as query:
            query.return_value = {
                "CheckoutRequestID": "checkout-1",
                "ResultCode": None,
            }
            first = self.client.get("/stk-push/checkout-1/status")
            second = self.client.get("/stk-push/checkout-1/status")

        self.assertEqual(first.json()["status"], "pending")
        self.assertEqual(second.json()["status"], "pending")
        query.assert_awaited_once_with("checkout-1")

    def test_stub_verification_normalizes_code_and_blocks_reuse(self):
        self._pending_stk()
        self._callback("checkout-1", items=self._successful_items())

        first = self.client.post(
            "/verify",
            json={"transaction_code": "abc123xyz", "amount": 250},
        )
        second = self.client.post(
            "/verify",
            json={"transaction_code": "ABC123XYZ", "amount": 250},
        )

        self.assertEqual(first.json(), {"result": "verified"})
        self.assertEqual(second.json(), {"result": "already_used"})

    def test_real_verification_deduplicates_pending_queries(self):
        with (
            patch.object(main.settings, "VERIFY_STUB_MODE", False),
            patch.object(main.daraja, "verify_c2b_transaction", new_callable=AsyncMock) as verify,
        ):
            verify.return_value = {"ResponseCode": "0"}
            payload = {"transaction_code": "ABC123XYZ", "amount": 250}
            first = self.client.post("/verify", json=payload)
            second = self.client.post("/verify", json=payload)

        self.assertEqual(first.json()["result"], "pending")
        self.assertEqual(second.json()["result"], "pending")
        verify.assert_awaited_once_with("ABC123XYZ")

    def test_real_verification_callback_without_amount_fails_closed(self):
        main.store.begin_verification("ABC123XYZ", 250)

        response = self.client.post(
            "/daraja/transaction-status-result",
            json={
                "Result": {
                    "TransactionID": "ABC123XYZ",
                    "ResultCode": "0",
                    "ResultParameters": {"ResultParameter": []},
                }
            },
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(
            main.store.get_verification("ABC123XYZ")["status"],
            "failed",
        )

    def test_real_verification_provider_rejection_fails_closed(self):
        with (
            patch.object(main.settings, "VERIFY_STUB_MODE", False),
            patch.object(main.daraja, "verify_c2b_transaction", new_callable=AsyncMock) as verify,
        ):
            verify.return_value = {"ResponseCode": "1"}
            response = self.client.post(
                "/verify",
                json={"transaction_code": "ABC123XYZ", "amount": 250},
            )

        self.assertEqual(response.status_code, 502)
        self.assertEqual(
            main.store.get_verification("ABC123XYZ")["status"],
            "failed",
        )


if __name__ == "__main__":
    unittest.main()
