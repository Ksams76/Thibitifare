import asyncio
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
os.environ["DARAJA_CALLBACK_TOKEN"] = ""
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

    def _pending_stk(
        self, checkout_id="checkout-1", amount=250, phone="0712345678", owner="test-conductor"
    ):
        key = None
        if owner is not None:
            key = f"{abs(hash(checkout_id)):036d}"[-36:]
            main.store.reserve_stk_submission(key, phone, amount, "Town", owner)
        main.store.create_stk_transaction(checkout_id, phone, amount, "Town", key)

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

    def test_health_reports_non_secret_payment_environment(self):
        for env, expected in (("sandbox", "sandbox"), ("production", "production"), ("other", "sandbox")):
            with patch.object(main.settings, "ENV", env):
                response = self.client.get("/")
            body = response.json()
            self.assertEqual(response.status_code, 200)
            self.assertEqual(body["daraja_env"], expected)
            self.assertEqual(body["verify_mode"], "stub")
        with patch.object(main.settings, "VERIFY_STUB_MODE", False):
            self.assertEqual(self.client.get("/").json()["verify_mode"], "live")

        dump = self.client.get("/").text
        for forbidden in ("safaricom.co.ke", "unit-test", "token", "unit-test.invalid"):
            self.assertNotIn(forbidden, dump)
        self.assertEqual(
            set(self.client.get("/").json()),
            {"status", "service", "verify_mode", "daraja_env", "callback_auth"},
        )

    def test_frontend_environment_indicator_is_wired_and_never_assumes_sandbox(self):
        root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        with open(os.path.join(root, "index.html"), encoding="utf-8") as handle:
            html = handle.read()
        with open(os.path.join(root, "script.js"), encoding="utf-8") as handle:
            script = handle.read()

        self.assertIn('id="envBanner"', html)
        self.assertIn("Environment unconfirmed — do not initiate payment.", script)
        self.assertIn("not a real-money payment environment", script)
        self.assertIn("not confirmed by Safaricom", script)
        self.assertIn("daraja: null, verify: null", script)
        self.assertNotIn("matches a real payment", script)

    def test_payment_routes_reject_requests_without_bearer_token(self):
        response = self.client.post(
            "/verify",
            json={"transaction_code": "ABC123XYZ", "amount": 250},
            headers={"Authorization": ""},
        )

        self.assertEqual(response.status_code, 401)

    def test_status_requires_authentication(self):
        self._pending_stk()
        self.assertIn(TestClient(main.app).get("/stk-push/checkout-1/status").status_code, (401, 403))

    def test_status_owner_can_read_and_other_user_is_denied_without_daraja_query(self):
        self._pending_stk()
        self._callback("checkout-1", result_code=1)
        with patch.object(main.daraja, "query_stk_status", new_callable=AsyncMock) as query:
            owner = self.client.get("/stk-push/checkout-1/status")
            with patch(
                "app.auth.verify_firebase_id_token",
                return_value={"uid": "other-conductor", "role": "conductor"},
            ):
                other = self.client.get("/stk-push/checkout-1/status")
                missing = self.client.get("/stk-push/no-such-checkout/status")
        self.assertEqual(owner.status_code, 200)
        self.assertEqual(owner.json()["status"], "failed")
        self.assertEqual(other.status_code, 404)
        self.assertEqual(other.json(), missing.json())
        query.assert_not_awaited()

    def test_pending_status_of_foreign_checkout_never_queries_daraja(self):
        self._pending_stk(owner="someone-else")
        with patch.object(main.daraja, "query_stk_status", new_callable=AsyncMock) as query:
            response = self.client.get("/stk-push/checkout-1/status")
        self.assertEqual(response.status_code, 404)
        query.assert_not_awaited()

    def test_legacy_ownerless_checkout_fails_closed(self):
        self._pending_stk(owner=None)
        with patch.object(main.daraja, "query_stk_status", new_callable=AsyncMock) as query:
            response = self.client.get("/stk-push/checkout-1/status")
        self.assertEqual(response.status_code, 404)
        query.assert_not_awaited()
        self.assertFalse(main.store.stk_checkout_owned_by("checkout-1", "test-conductor"))
        self.assertFalse(main.store.stk_checkout_owned_by("checkout-1", ""))

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

    def test_admin_can_assign_role_by_email_and_it_is_audited(self):
        with (
            patch(
                "app.auth.verify_firebase_id_token",
                return_value={"uid": "test-admin", "role": "admin"},
            ),
            patch(
                "app.main.set_firebase_user_role_by_email",
                return_value="firebase-user-9",
            ) as set_role,
        ):
            response = self.client.post(
                "/admin/role-assignments",
                json={"email": "new.conductor@example.com", "role": "conductor"},
            )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["uid"], "firebase-user-9")
        set_role.assert_called_once_with("new.conductor@example.com", "conductor")
        with closing(sqlite3.connect(main.store.database_path)) as connection:
            event = connection.execute(
                "SELECT actor_uid, action, subject_uid, response_status FROM audit_events"
            ).fetchone()
        self.assertEqual(
            event, ("test-admin", "identity.role.assign", "firebase-user-9", 200)
        )

    def test_role_by_email_is_admin_only_and_reports_unknown_accounts(self):
        body = {"email": "someone@example.com", "role": "conductor"}
        with patch(
            "app.auth.verify_firebase_id_token",
            return_value={"uid": "a-conductor", "role": "conductor"},
        ):
            forbidden = self.client.post("/admin/role-assignments", json=body)
        self.assertEqual(forbidden.status_code, 403)

        with (
            patch(
                "app.auth.verify_firebase_id_token",
                return_value={"uid": "test-admin", "role": "admin"},
            ),
            patch(
                "app.main.set_firebase_user_role_by_email",
                side_effect=main.FirebaseUserNotFound,
            ),
        ):
            missing = self.client.post("/admin/role-assignments", json=body)
        self.assertEqual(missing.status_code, 404)

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

    def _stk_post_client(self, status_code=200, body=None, invalid_json=False):
        response = Mock(status_code=status_code)
        if invalid_json:
            response.json.side_effect = ValueError("not json")
        else:
            response.json.return_value = body
        client = Mock()
        client.__aenter__ = AsyncMock(return_value=client)
        client.__aexit__ = AsyncMock(return_value=False)
        client.post = AsyncMock(return_value=response)
        return client

    def test_pre_send_failure_releases_reservation_for_fresh_submission(self):
        payload = {"phone_number": "0712345678", "amount": 250, "route": "Town"}
        client = self._stk_post_client(
            body={"ResponseCode": "0", "CheckoutRequestID": "checkout-1"}
        )
        with (
            patch(
                "app.daraja.get_access_token",
                new_callable=AsyncMock,
                side_effect=[main.daraja.DarajaError("Token unavailable."), "token"],
            ),
            patch("app.daraja.httpx.AsyncClient", return_value=client),
        ):
            first = self._submit_stk(payload)
            client.post.assert_not_awaited()
            second = self._submit_stk(payload)

        self.assertEqual(first.status_code, 502)
        self.assertEqual(second.status_code, 200)
        client.post.assert_awaited_once()

    def test_ambiguous_post_send_responses_are_not_retried_with_same_key(self):
        payload = {"phone_number": "0712345678", "amount": 250, "route": "Town"}
        cases = (
            {"status_code": 502, "body": {"errorMessage": "Bad gateway"}},
            {"status_code": 200, "invalid_json": True},
            {"status_code": 200, "body": ["not", "an", "object"]},
        )
        for index, case in enumerate(cases):
            with self.subTest(case=case):
                headers = {"Idempotency-Key": f"12345678-1234-5678-1234-56781234567{index}"}
                client = self._stk_post_client(**case)
                with (
                    patch(
                        "app.daraja.get_access_token",
                        new_callable=AsyncMock,
                        return_value="token",
                    ),
                    patch("app.daraja.httpx.AsyncClient", return_value=client),
                ):
                    first = self.client.post("/stk-push", json=payload, headers=headers)
                    second = self.client.post("/stk-push", json=payload, headers=headers)

                self.assertEqual(first.status_code, 504)
                self.assertEqual(second.status_code, 504)
                self.assertIn("outcome is unknown", first.json()["detail"])
                client.post.assert_awaited_once()
                self.assertEqual(
                    main.store.reserve_stk_submission(
                        headers["Idempotency-Key"], "0712345678", 250, "Town", "test-conductor"
                    )[0]["state"],
                    "unknown",
                )

    def test_explicit_provider_rejection_still_releases_reservation(self):
        payload = {"phone_number": "0712345678", "amount": 250, "route": "Town"}
        client = self._stk_post_client(
            body={"ResponseCode": "1", "ResponseDescription": "Request rejected."}
        )
        with (
            patch(
                "app.daraja.get_access_token",
                new_callable=AsyncMock,
                return_value="token",
            ),
            patch("app.daraja.httpx.AsyncClient", return_value=client),
        ):
            first = self._submit_stk(payload)
            second = self._submit_stk(payload)

        self.assertEqual(first.status_code, 502)
        self.assertEqual(second.status_code, 502)
        self.assertEqual(client.post.await_count, 2)

    def _attempt_url(self, key=None):
        return f"/stk-push/attempts/{key or IDEMPOTENCY_HEADERS['Idempotency-Key']}"

    def _accepted_attempt(self):
        client = self._stk_post_client(
            body={"ResponseCode": "0", "CheckoutRequestID": "checkout-recover"}
        )
        with (
            patch("app.daraja.get_access_token", new_callable=AsyncMock, return_value="token"),
            patch("app.daraja.httpx.AsyncClient", return_value=client),
        ):
            response = self._submit_stk(
                {"phone_number": "0712345678", "amount": 250, "route": "Town"}
            )
        self.assertEqual(response.status_code, 200)

    def test_attempt_recovery_requires_authentication(self):
        anonymous = TestClient(main.app)
        response = anonymous.get(self._attempt_url())
        self.assertIn(response.status_code, (401, 403))

    def test_attempt_recovery_returns_owner_checkout_and_status(self):
        self._accepted_attempt()
        pending = self.client.get(self._attempt_url())
        self.assertEqual(pending.status_code, 200)
        self.assertEqual(
            pending.json(),
            {"state": "accepted", "checkout_request_id": "checkout-recover", "status": "pending"},
        )
        self._callback("checkout-recover", result_code=1)
        failed = self.client.get(self._attempt_url())
        self.assertEqual(failed.json()["status"], "failed")
        self.assertNotIn("0712345678", failed.text)

    def test_attempt_recovery_hides_other_users_attempts(self):
        self._accepted_attempt()
        with patch(
            "app.auth.verify_firebase_id_token",
            return_value={"uid": "other-conductor", "role": "conductor"},
        ):
            other = self.client.get(self._attempt_url())
            missing = self.client.get(
                self._attempt_url("00000000-0000-0000-0000-000000000000")
            )
        self.assertEqual(other.status_code, 404)
        self.assertEqual(other.json(), missing.json())
        self.assertNotIn("checkout-recover", other.text)

    def test_other_user_cannot_reuse_a_key_to_reach_the_attempt(self):
        self._accepted_attempt()
        with patch(
            "app.auth.verify_firebase_id_token",
            return_value={"uid": "other-conductor", "role": "conductor"},
        ), patch.object(main.daraja, "stk_push", new_callable=AsyncMock) as push:
            response = self._submit_stk(
                {"phone_number": "0712345678", "amount": 250, "route": "Town"}
            )
        self.assertEqual(response.status_code, 409)
        push.assert_not_awaited()

    def test_attempt_recovery_reports_unknown_outcome_without_resolving_it(self):
        with patch.object(
            main.daraja,
            "stk_push",
            new_callable=AsyncMock,
            side_effect=main.daraja.DarajaOutcomeUnknown("lost"),
        ):
            self._submit_stk({"phone_number": "0712345678", "amount": 250, "route": "Town"})
        response = self.client.get(self._attempt_url())
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["state"], "unknown")
        self.assertNotIn("status", response.json())

    def test_attempt_recovery_rejects_malformed_key(self):
        self.assertEqual(self.client.get("/stk-push/attempts/not-a-key").status_code, 422)

    def test_legacy_ownerless_attempt_is_not_recoverable(self):
        main.store.reserve_stk_submission(
            IDEMPOTENCY_HEADERS["Idempotency-Key"], "0712345678", 250, "Town"
        )
        self.assertEqual(self.client.get(self._attempt_url()).status_code, 404)

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

    def test_non_zero_callback_result_becomes_terminal_failure(self):
        self._pending_stk()

        response = self._callback("checkout-1", result_code=1)
        status = self.client.get("/stk-push/checkout-1/status")

        self.assertEqual(response.status_code, 200)
        transaction = main.store.get_stk_transaction("checkout-1")
        self.assertEqual(transaction["status"], "failed")
        self.assertIsNone(transaction["receipt_number"])
        self.assertIsNone(transaction["phone_number"])
        self.assertEqual(
            status.json(),
            {"status": "failed", "reason": "Payment was not completed."},
        )

    def test_late_success_callback_does_not_override_failure(self):
        self._pending_stk()
        self._callback("checkout-1", result_code=1)

        late = self._callback("checkout-1", items=self._successful_items())

        self.assertEqual(late.status_code, 200)
        transaction = main.store.get_stk_transaction("checkout-1")
        self.assertEqual(transaction["status"], "failed")
        self.assertIsNone(transaction["receipt_number"])
        self.assertEqual(
            main.store.claim_known_receipt("ABC123XYZ", 250)[0],
            "not_found",
        )

    def test_status_query_without_result_code_stays_pending(self):
        self._pending_stk()
        with patch.object(main.daraja, "query_stk_status", new_callable=AsyncMock) as query:
            query.return_value = {"CheckoutRequestID": "checkout-1"}
            response = self.client.get("/stk-push/checkout-1/status")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), {"status": "pending"})

    def test_status_query_processing_description_stays_pending(self):
        self._pending_stk()
        with patch.object(main.daraja, "query_stk_status", new_callable=AsyncMock) as query:
            query.return_value = {
                "CheckoutRequestID": "checkout-1",
                "ResultCode": "1",
                "ResultDesc": "The request is still being processed",
            }
            response = self.client.get("/stk-push/checkout-1/status")

        self.assertEqual(response.json(), {"status": "pending"})
        self.assertEqual(main.store.get_stk_transaction("checkout-1")["status"], "pending")

    def test_status_query_failure_is_never_final_and_valid_callback_can_still_confirm(self):
        self._pending_stk()
        with patch.object(main.daraja, "query_stk_status", new_callable=AsyncMock) as query:
            query.return_value = {
                "CheckoutRequestID": "checkout-1",
                "ResultCode": "1",
                "ResultDesc": "Request declined",
            }
            status = self.client.get("/stk-push/checkout-1/status")

        self.assertEqual(status.json(), {"status": "pending"})
        transaction = main.store.get_stk_transaction("checkout-1")
        self.assertEqual(transaction["status"], "pending")
        self.assertIsNotNone(transaction["phone_number"])

        self._callback("checkout-1", items=self._successful_items())

        self.assertEqual(main.store.get_stk_transaction("checkout-1")["status"], "success")

    def test_callback_failure_remains_the_only_way_to_fail_a_payment(self):
        self._pending_stk()
        with patch.object(main.daraja, "query_stk_status", new_callable=AsyncMock) as query:
            query.return_value = {"CheckoutRequestID": "checkout-1", "ResultCode": "1"}
            self.client.get("/stk-push/checkout-1/status")
        self._callback("checkout-1", result_code=1)

        self.assertEqual(main.store.get_stk_transaction("checkout-1")["status"], "failed")

    def test_failure_callback_cannot_downgrade_a_successful_payment(self):
        self._pending_stk()
        self._callback("checkout-1", items=self._successful_items())

        late_failure = self._callback("checkout-1", result_code=1)

        self.assertEqual(late_failure.status_code, 200)
        transaction = main.store.get_stk_transaction("checkout-1")
        self.assertEqual(transaction["status"], "success")
        self.assertEqual(transaction["receipt_number"], "ABC123XYZ")

    def test_replayed_success_callback_with_other_receipt_keeps_first_receipt(self):
        self._pending_stk()
        self._callback("checkout-1", items=self._successful_items(receipt="FIRST111"))

        self._callback("checkout-1", items=self._successful_items(receipt="SECOND222"))

        transaction = main.store.get_stk_transaction("checkout-1")
        self.assertEqual(transaction["receipt_number"], "FIRST111")
        self.assertEqual(main.store.claim_known_receipt("SECOND222", 250)[0], "not_found")

    def test_success_callback_without_receipt_fails_closed(self):
        self._pending_stk()
        items = [i for i in self._successful_items() if i["Name"] != "MpesaReceiptNumber"]

        self._callback("checkout-1", items=items)

        transaction = main.store.get_stk_transaction("checkout-1")
        self.assertEqual(transaction["status"], "failed")
        self.assertIsNone(transaction["receipt_number"])

    def test_callback_for_unknown_checkout_is_acknowledged_without_side_effects(self):
        self._pending_stk()

        response = self._callback("not-ours", items=self._successful_items())

        self.assertEqual(response.status_code, 200)
        self.assertIsNone(main.store.get_stk_transaction("not-ours"))
        self.assertEqual(main.store.get_stk_transaction("checkout-1")["status"], "pending")
        self.assertEqual(main.store.claim_known_receipt("ABC123XYZ", 250)[0], "not_found")

    def test_callback_only_affects_its_own_checkout(self):
        self._pending_stk("checkout-1")
        self._pending_stk("checkout-2", phone="0711111111")

        self._callback("checkout-1", items=self._successful_items())

        self.assertEqual(main.store.get_stk_transaction("checkout-1")["status"], "success")
        self.assertEqual(main.store.get_stk_transaction("checkout-2")["status"], "pending")

    def test_callback_with_missing_or_malformed_result_code_stays_pending(self):
        for index, bad_code in enumerate((None, True, "abc", {}, [], 1.5)):
            with self.subTest(result_code=bad_code):
                checkout = f"checkout-bad-{index}"
                self._pending_stk(checkout, phone=f"07000000{index:02d}")
                response = self._callback(checkout, result_code=bad_code)
                self.assertEqual(response.status_code, 200)
                self.assertEqual(main.store.get_stk_transaction(checkout)["status"], "pending")

    def test_status_query_provider_error_never_marks_payment_failed(self):
        self._pending_stk()
        with patch.object(
            main.daraja,
            "query_stk_status",
            new_callable=AsyncMock,
            side_effect=main.daraja.DarajaError("Provider unavailable."),
        ):
            response = self.client.get("/stk-push/checkout-1/status")

        self.assertEqual(response.status_code, 502)
        self.assertEqual(main.store.get_stk_transaction("checkout-1")["status"], "pending")

    def test_status_query_malformed_result_code_stays_pending(self):
        for index, bad_code in enumerate((True, "abc", {}, [], 1.5, "")):
            with self.subTest(result_code=bad_code):
                checkout = f"checkout-query-{index}"
                self._pending_stk(checkout, phone=f"07000000{index:02d}")
                with patch.object(
                    main.daraja, "query_stk_status", new_callable=AsyncMock
                ) as query:
                    query.return_value = {"CheckoutRequestID": checkout, "ResultCode": bad_code}
                    response = self.client.get(f"/stk-push/{checkout}/status")
                self.assertEqual(response.status_code, 200)
                self.assertEqual(response.json(), {"status": "pending"})

    def test_status_query_with_non_text_description_does_not_crash(self):
        self._pending_stk()
        with patch.object(main.daraja, "query_stk_status", new_callable=AsyncMock) as query:
            query.return_value = {
                "CheckoutRequestID": "checkout-1",
                "ResultCode": "1",
                "ResultDesc": {"unexpected": "shape"},
            }
            response = self.client.get("/stk-push/checkout-1/status")

        self.assertEqual(response.status_code, 200)

    def test_status_response_does_not_disclose_payment_details(self):
        self._pending_stk()
        self._callback("checkout-1", items=self._successful_items())

        response = self.client.get("/stk-push/checkout-1/status")

        self.assertEqual(response.json(), {"status": "success"})
        for secret in ("ABC123XYZ", "0712345678", "254712345678", "250"):
            self.assertNotIn(secret, response.text)

    def test_replayed_verification_result_cannot_change_a_completed_verification(self):
        main.store.begin_verification("ABC123XYZ", 250)

        def result(amount, code="0"):
            return self.client.post(
                "/daraja/transaction-status-result",
                json={
                    "Result": {
                        "TransactionID": "ABC123XYZ",
                        "ResultCode": code,
                        "ResultParameters": {
                            "ResultParameter": [{"Key": "Amount", "Value": amount}]
                        },
                    }
                },
            )

        result(250)
        result(100)
        result(250, code="1")
        self.client.post(
            "/daraja/timeout", json={"Result": {"TransactionID": "ABC123XYZ"}}
        )

        status = main.store.get_verification("ABC123XYZ")
        self.assertEqual(status["status"], "verified")
        self.assertEqual(status["actual"], 250)

    def test_verification_result_for_unknown_code_creates_nothing(self):
        response = self.client.post(
            "/daraja/transaction-status-result",
            json={"Result": {"TransactionID": "NOSUCH123", "ResultCode": "0"}},
        )
        self.assertEqual(response.status_code, 200)
        self.assertIsNone(main.store.get_verification("NOSUCH123"))

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

    def test_daraja_requests_use_bearer_access_token(self):
        response = Mock(status_code=200)
        response.json.return_value = {"ResponseCode": "0"}
        client = Mock()
        client.__aenter__ = AsyncMock(return_value=client)
        client.__aexit__ = AsyncMock(return_value=False)
        client.post = AsyncMock(return_value=response)

        async def send_requests():
            with (
                patch(
                    "app.daraja.get_access_token",
                    new_callable=AsyncMock,
                    return_value="sandbox-access-token",
                ),
                patch(
                    "app.daraja.generate_security_credential",
                    return_value="encrypted-sandbox-credential",
                ),
                patch("app.daraja.httpx.AsyncClient", return_value=client),
            ):
                await main.daraja.stk_push("254712345678", 250, "Town", "Fare")
                await main.daraja.query_stk_status("checkout-1")
                await main.daraja.verify_c2b_transaction("ABC123XYZ")

        asyncio.run(send_requests())

        self.assertEqual(client.post.await_count, 3)
        for call in client.post.await_args_list:
            scheme, _, token = call.kwargs["headers"]["Authorization"].partition(" ")
            self.assertEqual(scheme, "Be" + "arer")
            self.assertEqual(token, "sandbox-access-token")
            self.assertIn("sandbox.safaricom.co.ke", call.args[0])

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

    def test_real_verification_callback_completes_pending_status(self):
        main.store.begin_verification("ABC123XYZ", 250, "test-conductor")

        response = self.client.post(
            "/daraja/transaction-status-result",
            json={
                "Result": {
                    "TransactionID": "ABC123XYZ",
                    "ResultCode": "0",
                    "ResultParameters": {
                        "ResultParameter": [{"Key": "Amount", "Value": 250}]
                    },
                }
            },
        )
        status = self.client.get("/verify/ABC123XYZ/status")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(status.status_code, 200)
        self.assertEqual(status.json()["status"], "verified")

    def test_real_verification_callback_rejects_mismatched_amount(self):
        main.store.begin_verification("ABC123XYZ", 250, "test-conductor")

        response = self.client.post(
            "/daraja/transaction-status-result",
            json={
                "Result": {
                    "TransactionID": "ABC123XYZ",
                    "ResultCode": "0",
                    "ResultParameters": {
                        "ResultParameter": [{"Key": "Amount", "Value": 200}]
                    },
                }
            },
        )
        status = self.client.get("/verify/ABC123XYZ/status")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(status.status_code, 200)
        self.assertEqual(status.json()["status"], "amount_mismatch")

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


TEST_TOKEN = "t" * 40


class CallbackAuthTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = TemporaryDirectory()
        self.original_store = main.store
        main.store = TransactionStore(
            os.path.join(self.temp_dir.name, "transactions.sqlite3"),
            os.environ["THIBITIFARE_PHONE_ENCRYPTION_KEY"],
        )
        main.store.initialize()
        token_patch = patch.object(main.settings, "CALLBACK_AUTH_TOKEN", TEST_TOKEN)
        token_patch.start()
        self.addCleanup(token_patch.stop)
        self.client = TestClient(main.app)
        main.store.create_stk_transaction("checkout-1", "0712345678", 250, "Town", None)

    def tearDown(self):
        main.store = self.original_store
        self.temp_dir.cleanup()

    def _stk_body(self, result_code=0):
        items = [
            {"Name": "MpesaReceiptNumber", "Value": "ABC123XYZ"},
            {"Name": "Amount", "Value": 250},
            {"Name": "PhoneNumber", "Value": 254712345678},
        ]
        return {
            "Body": {
                "stkCallback": {
                    "CheckoutRequestID": "checkout-1",
                    "ResultCode": result_code,
                    "CallbackMetadata": {"Item": items if result_code == 0 else []},
                }
            }
        }

    def _routes(self):
        return [
            ("/daraja/callback", self._stk_body()),
            ("/daraja/transaction-status-result", {"Result": {"ResultCode": 0}}),
            ("/daraja/timeout", {"Result": {"TransactionID": "ABC123XYZ"}}),
        ]

    def test_valid_token_is_accepted_on_every_callback_route(self):
        for path, body in self._routes():
            response = self.client.post(f"{path}?token={TEST_TOKEN}", json=body)
            self.assertEqual(response.status_code, 200, path)

    def test_missing_wrong_or_empty_token_is_rejected_on_every_route(self):
        for path, body in self._routes():
            for suffix in ("", "?token=", "?token=wrong", f"?token={TEST_TOKEN}x", f"?other={TEST_TOKEN}"):
                response = self.client.post(path + suffix, json=body)
                self.assertEqual(response.status_code, 403, path + suffix)

    def test_rejected_callback_cannot_change_state_or_create_receipt(self):
        response = self.client.post("/daraja/callback?token=wrong", json=self._stk_body())

        self.assertEqual(response.status_code, 403)
        transaction = main.store.get_stk_transaction("checkout-1")
        self.assertEqual(transaction["status"], "pending")
        self.assertIsNone(transaction["receipt_number"])
        self.assertEqual(main.store.claim_known_receipt("ABC123XYZ", 250)[0], "not_found")

    def test_rejected_failure_callback_cannot_fail_a_payment(self):
        self.client.post("/daraja/callback", json=self._stk_body(result_code=1))

        self.assertEqual(main.store.get_stk_transaction("checkout-1")["status"], "pending")

    def test_authenticated_callback_still_applies_and_is_idempotent(self):
        url = f"/daraja/callback?token={TEST_TOKEN}"
        self.client.post(url, json=self._stk_body())
        self.client.post(url, json=self._stk_body())

        transaction = main.store.get_stk_transaction("checkout-1")
        self.assertEqual(transaction["status"], "success")
        self.assertEqual(transaction["receipt_number"], "ABC123XYZ")

    def test_secret_is_not_in_error_response_or_logs(self):
        with self.assertLogs(level="DEBUG") as captured:
            import logging

            logging.getLogger("app-test").debug("probe")
            response = self.client.post("/daraja/callback?token=wrong", json=self._stk_body())

        self.assertNotIn(TEST_TOKEN, response.text)
        self.assertNotIn(TEST_TOKEN, "\n".join(captured.output))

    def test_log_filter_redacts_token_in_access_log_records(self):
        import logging

        record = logging.LogRecord(
            "uvicorn.access", logging.INFO, "", 0,
            '%s - "%s %s HTTP/1.1" %d',
            ("127.0.0.1", "POST", f"/daraja/callback?token={TEST_TOKEN}&x=1", 200), None,
        )
        main._RedactCallbackToken().filter(record)

        rendered = record.getMessage()
        self.assertNotIn(TEST_TOKEN, rendered)
        self.assertIn("token=[redacted]&x=1", rendered)

    def test_callback_url_includes_token_only_when_configured(self):
        with_token = main.daraja.callback_url("/daraja/callback")
        self.assertTrue(with_token.endswith(f"/daraja/callback?token={TEST_TOKEN}"))
        with patch.object(main.settings, "CALLBACK_AUTH_TOKEN", ""):
            self.assertFalse("token=" in main.daraja.callback_url("/daraja/callback"))

    def test_health_reports_protection_state_without_the_secret(self):
        enabled = self.client.get("/")
        with patch.object(main.settings, "CALLBACK_AUTH_TOKEN", ""):
            disabled = self.client.get("/")

        self.assertEqual(enabled.json()["callback_auth"], "enabled")
        self.assertEqual(disabled.json()["callback_auth"], "disabled")
        self.assertNotIn(TEST_TOKEN, enabled.text)

    def test_settings_validation_fails_closed(self):
        from app.config import Settings

        class Short(Settings):
            CALLBACK_AUTH_TOKEN = "short"

        class Production(Settings):
            ENV = "production"
            CALLBACK_AUTH_TOKEN = ""

        class Good(Settings):
            ENV = "production"
            CALLBACK_AUTH_TOKEN = TEST_TOKEN

        with self.assertRaises(RuntimeError) as short:
            Short()
        with self.assertRaises(RuntimeError):
            Production()
        Good()
        self.assertNotIn("short", str(short.exception).replace("DARAJA_CALLBACK_TOKEN", ""))


class AttemptReviewTests(unittest.TestCase):
    KEY = "12345678-1234-5678-1234-567812345678"
    URL = f"/admin/stk-attempts/{KEY}/review"
    BODY = {
        "outcome": "no_change_review",
        "evidence_type": "daraja_portal",
        "evidence_reference": "PORTAL-REF-1",
        "reason": "Portal shows no matching request for this attempt.",
    }

    def setUp(self):
        self.temp_dir = TemporaryDirectory()
        self.original_store = main.store
        main.store = TransactionStore(
            os.path.join(self.temp_dir.name, "transactions.sqlite3"),
            os.environ["THIBITIFARE_PHONE_ENCRYPTION_KEY"],
        )
        main.store.initialize()
        self.identity = {"uid": "test-admin", "role": "admin"}
        auth_patch = patch(
            "app.auth.verify_firebase_id_token",
            side_effect=lambda *_a, **_k: dict(self.identity),
        )
        auth_patch.start()
        self.addCleanup(auth_patch.stop)
        self.client = TestClient(main.app, headers={"Authorization": "Bearer unit-test-token"})
        main.store.reserve_stk_submission(self.KEY, "0712345678", 250, "Town", "test-conductor")
        main.store.mark_stk_submission_unknown(self.KEY, "Outcome unknown.")

    def tearDown(self):
        main.store = self.original_store
        self.temp_dir.cleanup()

    def _rows(self, sql):
        with closing(sqlite3.connect(main.store.database_path)) as connection:
            return connection.execute(sql).fetchall()

    def _as(self, uid, role):
        self.identity = {"uid": uid, "role": role}

    def _assert_unchanged_unknown(self):
        self.assertEqual(
            main.store.get_stk_attempt(self.KEY, "test-conductor")["state"], "unknown"
        )
        self.assertEqual(self._rows("SELECT COUNT(*) FROM transactions"), [(0,)])

    def test_unauthenticated_request_is_rejected(self):
        response = TestClient(main.app).post(self.URL, json=self.BODY)

        self.assertIn(response.status_code, (401, 403))
        self.assertEqual(self._rows("SELECT COUNT(*) FROM stk_attempt_reviews"), [(0,)])

    def test_unauthenticated_unknown_attempt_listing_is_rejected_and_unrecorded(self):
        response = TestClient(main.app).get("/admin/stk-attempts/unknown")

        self.assertEqual(response.status_code, 401)
        self.assertEqual(response.headers.get("WWW-Authenticate"), "Bearer")
        self.assertNotIn("attempts", response.json())
        self.assertEqual(self._rows("SELECT COUNT(*) FROM audit_events"), [(0,)])
        self.assertEqual(self._rows("SELECT COUNT(*) FROM stk_attempt_reviews"), [(0,)])
        self._assert_unchanged_unknown()

    def test_conductor_cannot_review_even_their_own_attempt(self):
        for uid in ("test-conductor", "other-conductor"):
            self._as(uid, "conductor")
            response = self.client.post(self.URL, json=self.BODY)
            self.assertEqual(response.status_code, 403)
        self.assertEqual(self._rows("SELECT COUNT(*) FROM stk_attempt_reviews"), [(0,)])
        self._assert_unchanged_unknown()

    def test_admin_cannot_review_their_own_attempt(self):
        self._as("test-conductor", "admin")

        response = self.client.post(self.URL, json=self.BODY)

        self.assertEqual(response.status_code, 403)
        self.assertEqual(self._rows("SELECT COUNT(*) FROM stk_attempt_reviews"), [(0,)])

    def test_admin_cannot_mark_success_or_omit_evidence(self):
        bad_bodies = [
            {**self.BODY, "outcome": "success"},
            {**self.BODY, "outcome": "failed"},
            {**self.BODY, "receipt_number": "ABC123XYZ"},
            {**self.BODY, "reason": "short"},
            {**self.BODY, "reason": "Passenger 0712345678 says they paid."},
            {**self.BODY, "evidence_reference": "0712345678"},
            {**self.BODY, "evidence_reference": "a b"},
            {k: v for k, v in self.BODY.items() if k != "evidence_reference"},
            {k: v for k, v in self.BODY.items() if k != "reason"},
        ]
        for body in bad_bodies:
            response = self.client.post(self.URL, json=body)
            self.assertEqual(response.status_code, 422, body)

        self.assertEqual(self._rows("SELECT COUNT(*) FROM stk_attempt_reviews"), [(0,)])
        self.assertEqual(self._rows("SELECT COUNT(*) FROM used_codes"), [(0,)])
        self._assert_unchanged_unknown()

    def test_admin_review_is_recorded_without_changing_state(self):
        response = self.client.post(self.URL, json=self.BODY)

        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertEqual(body["previous_state"], "unknown")
        self.assertEqual(body["new_state"], "unknown")
        self.assertEqual(body["outcome"], "no_change_review")
        self._assert_unchanged_unknown()
        self.assertEqual(
            self._rows(
                "SELECT idempotency_key, admin_uid, previous_state, new_state, outcome,"
                " evidence_type, evidence_reference FROM stk_attempt_reviews"
            ),
            [(self.KEY, "test-admin", "unknown", "unknown", "no_change_review",
              "daraja_portal", "PORTAL-REF-1")],
        )
        self.assertEqual(
            self._rows(
                "SELECT actor_uid, actor_role, action, subject_uid, response_status"
                " FROM audit_events"
            ),
            [("test-admin", "admin", "stk.attempt.review", "test-conductor", 200)],
        )

    def test_rejected_attempts_are_audited_with_their_status(self):
        self._as("test-conductor", "conductor")
        self.client.post(self.URL, json=self.BODY)

        self.assertEqual(
            self._rows("SELECT actor_uid, action, response_status FROM audit_events"),
            [("test-conductor", "stk.attempt.review", 403)],
        )

    def test_review_and_audit_records_hold_no_passenger_data_or_secrets(self):
        response = self.client.post(self.URL, json=self.BODY)
        listing = self.client.get("/admin/stk-attempts/unknown")
        dump = repr(
            self._rows("SELECT * FROM stk_attempt_reviews")
            + self._rows("SELECT * FROM audit_events")
        ) + response.text + listing.text

        for forbidden in ("0712345678", "254712345678", "unit-test-secret", "unit-test-passkey"):
            self.assertNotIn(forbidden, dump)

    def test_replayed_evidence_is_rejected_but_new_evidence_is_recorded(self):
        first = self.client.post(self.URL, json=self.BODY)
        replay = self.client.post(self.URL, json=self.BODY)
        other = self.client.post(self.URL, json={**self.BODY, "evidence_reference": "PORTAL-REF-2"})

        self.assertEqual((first.status_code, replay.status_code, other.status_code), (200, 409, 200))
        self.assertEqual(self._rows("SELECT COUNT(*) FROM stk_attempt_reviews"), [(2,)])
        self._assert_unchanged_unknown()

    def test_concurrent_identical_reviews_record_exactly_one(self):
        from concurrent.futures import ThreadPoolExecutor

        def attempt(_):
            try:
                return main.store.review_unknown_stk_attempt(
                    self.KEY, "test-admin", "other", "SAME-REF", "Concurrent review attempt."
                ) is not None
            except main.StorageConflict:
                return False

        with ThreadPoolExecutor(max_workers=4) as executor:
            results = list(executor.map(attempt, range(4)))

        self.assertEqual(results.count(True), 1)
        self.assertEqual(self._rows("SELECT COUNT(*) FROM stk_attempt_reviews"), [(1,)])

    def test_missing_attempt_is_not_found(self):
        response = self.client.post(
            "/admin/stk-attempts/99999999-1234-5678-1234-567812345678/review", json=self.BODY
        )

        self.assertEqual(response.status_code, 404)

    def test_terminal_and_in_flight_attempts_cannot_be_reviewed(self):
        accepted = "aaaaaaaa-1234-5678-1234-567812345678"
        fresh = "bbbbbbbb-1234-5678-1234-567812345678"
        main.store.reserve_stk_submission(accepted, "0712345678", 100, "Town", "test-conductor")
        main.store.create_stk_transaction("checkout-9", "0712345678", 100, "Town", accepted)
        main.store.apply_stk_callback("checkout-9", True, "ZZZ999AAA", 100, True)
        main.store.reserve_stk_submission(fresh, "0712345678", 120, "Town", "test-conductor")

        for key in (accepted, fresh):
            response = self.client.post(f"/admin/stk-attempts/{key}/review", json=self.BODY)
            self.assertEqual(response.status_code, 409, key)

        self.assertEqual(main.store.get_stk_transaction("checkout-9")["status"], "success")
        self.assertEqual(
            main.store.get_stk_attempt(fresh, "test-conductor")["state"], "submitting"
        )
        self.assertEqual(self._rows("SELECT COUNT(*) FROM stk_attempt_reviews"), [(0,)])

    def test_review_does_not_allow_a_second_prompt_with_the_same_attempt(self):
        self.client.post(self.URL, json=self.BODY)
        self._as("test-conductor", "conductor")

        with patch.object(main.daraja, "stk_push", new_callable=AsyncMock) as push:
            response = self.client.post(
                "/stk-push",
                json={"phone_number": "0712345678", "amount": 250, "route": "Town"},
                headers={"Idempotency-Key": self.KEY},
            )

        push.assert_not_awaited()
        self.assertNotEqual(response.status_code, 200)
        self.assertEqual(
            main.store.get_stk_attempt(self.KEY, "test-conductor")["state"], "unknown"
        )

    def test_unknown_attempt_listing_is_admin_only_and_shows_reviews(self):
        self.client.post(self.URL, json=self.BODY)

        admin = self.client.get("/admin/stk-attempts/unknown")
        self._as("test-conductor", "conductor")
        conductor = self.client.get("/admin/stk-attempts/unknown")

        self.assertEqual(conductor.status_code, 403)
        attempts = admin.json()["attempts"]
        self.assertEqual([a["idempotency_key"] for a in attempts], [self.KEY])
        self.assertEqual(attempts[0]["review_count"], 1)


    def test_listing_exposes_only_minimal_fields_and_never_changes_state(self):
        for index in range(3):
            key = f"cccccccc-0000-0000-0000-00000000000{index}"
            main.store.reserve_stk_submission(key, "0798765432", 480 + index, "Secret Route", "owner-x")
            main.store.mark_stk_submission_unknown(key, "Outcome unknown.")
        accepted = "dddddddd-1234-5678-1234-567812345678"
        main.store.reserve_stk_submission(accepted, "0711111111", 90, "Town", "owner-y")
        main.store.create_stk_transaction("checkout-acc", "0711111111", 90, "Town", accepted)
        before = self._rows("SELECT * FROM idempotency_keys ORDER BY 1")

        response = self.client.get("/admin/stk-attempts/unknown?limit=100")

        self.assertEqual(response.status_code, 200)
        attempts = response.json()["attempts"]
        self.assertEqual(len(attempts), 4)
        self.assertNotIn(accepted, {a["idempotency_key"] for a in attempts})
        for attempt in attempts:
            self.assertEqual(
                set(attempt),
                {"idempotency_key", "owner_uid", "reason", "updated_at", "review_count"},
            )
        for forbidden in (
            "0798765432", "254798765432", "0712345678", "Secret Route", "fingerprint",
            "phone", "checkout", "receipt", "amount", "unit-test-secret", "unit-test-passkey",
        ):
            self.assertNotIn(forbidden, response.text)
        self.assertEqual(self._rows("SELECT * FROM idempotency_keys ORDER BY 1"), before)
        self.assertEqual(self._rows("SELECT COUNT(*) FROM transactions WHERE status != 'pending'"), [(0,)])
        self.assertEqual(
            self._rows("SELECT action, response_status FROM audit_events"),
            [("stk.attempt.list", 200)],
        )

    def test_listing_pagination_is_bounded_and_stable(self):
        for index in range(5):
            key = f"eeeeeeee-0000-0000-0000-00000000000{index}"
            main.store.reserve_stk_submission(key, "0712345678", 100, "Town", "owner-x")
            main.store.mark_stk_submission_unknown(key, "Outcome unknown.")

        page_one = self.client.get("/admin/stk-attempts/unknown?limit=3&offset=0").json()["attempts"]
        page_two = self.client.get("/admin/stk-attempts/unknown?limit=3&offset=3").json()["attempts"]

        keys = [a["idempotency_key"] for a in page_one + page_two]
        self.assertEqual(len(keys), 6)
        self.assertEqual(len(set(keys)), 6)
        for query in ("limit=0", "limit=101", "offset=-1", "limit=abc"):
            self.assertEqual(
                self.client.get(f"/admin/stk-attempts/unknown?{query}").status_code, 422, query
            )

    def test_review_never_marks_an_attempt_paid_or_failed(self):
        main.store.create_stk_transaction("checkout-u", "0712345678", 250, "Town", None)

        self.client.post(self.URL, json=self.BODY)

        self.assertEqual(main.store.get_stk_transaction("checkout-u")["status"], "pending")
        self.assertEqual(self._rows("SELECT state FROM idempotency_keys"), [("unknown",)])
        self.assertEqual(self._rows("SELECT COUNT(*) FROM transactions WHERE receipt_number IS NOT NULL"), [(0,)])

    def test_review_route_does_not_accept_get_or_delete(self):
        self.assertEqual(self.client.get(self.URL).status_code, 405)
        self.assertEqual(self.client.delete(self.URL).status_code, 405)
        self.assertEqual(self.client.delete("/admin/stk-attempts/unknown").status_code, 405)


class VerificationOwnershipTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = TemporaryDirectory()
        self.original_store = main.store
        main.store = TransactionStore(
            os.path.join(self.temp_dir.name, "transactions.sqlite3"),
            os.environ["THIBITIFARE_PHONE_ENCRYPTION_KEY"],
        )
        main.store.initialize()
        self.identity = {"uid": "test-conductor", "role": "conductor"}
        auth_patch = patch(
            "app.auth.verify_firebase_id_token",
            side_effect=lambda *_a, **_k: dict(self.identity),
        )
        auth_patch.start()
        self.addCleanup(auth_patch.stop)
        self.client = TestClient(main.app, headers={"Authorization": "Bearer unit-test-token"})

    def tearDown(self):
        main.store = self.original_store
        self.temp_dir.cleanup()

    def test_owner_reads_their_verification_status(self):
        main.store.begin_verification("ABC123XYZ", 250, "test-conductor")

        response = self.client.get("/verify/ABC123XYZ/status")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["status"], "pending")

    def test_foreign_missing_and_ownerless_records_are_indistinguishable(self):
        main.store.begin_verification("FOREIGN123", 250, "someone-else")
        main.store.begin_verification("LEGACY123", 250)

        responses = [
            self.client.get(f"/verify/{code}/status")
            for code in ("FOREIGN123", "LEGACY123", "MISSING123")
        ]

        self.assertEqual([r.status_code for r in responses], [404, 404, 404])
        self.assertEqual(len({r.text for r in responses}), 1)

    def test_unauthenticated_access_is_rejected(self):
        main.store.begin_verification("ABC123XYZ", 250, "test-conductor")

        response = TestClient(main.app).get("/verify/ABC123XYZ/status")

        self.assertIn(response.status_code, (401, 403))

    def test_other_conductor_cannot_probe_or_replay_a_verification_via_post(self):
        main.store.begin_verification("ABC123XYZ", 250, "someone-else")
        with (
            patch.object(main.settings, "VERIFY_STUB_MODE", False),
            patch.object(main.daraja, "verify_c2b_transaction", new_callable=AsyncMock) as verify,
        ):
            response = self.client.post(
                "/verify", json={"transaction_code": "ABC123XYZ", "amount": 999}
            )

        self.assertEqual(response.status_code, 409)
        self.assertNotIn("250", response.text)
        verify.assert_not_awaited()

    def test_real_verify_flow_records_the_authenticated_owner(self):
        with (
            patch.object(main.settings, "VERIFY_STUB_MODE", False),
            patch.object(main.daraja, "verify_c2b_transaction", new_callable=AsyncMock) as verify,
        ):
            verify.return_value = {"ResponseCode": "0"}
            self.client.post("/verify", json={"transaction_code": "ABC123XYZ", "amount": 250})

        self.assertEqual(self.client.get("/verify/ABC123XYZ/status").status_code, 200)
        self.identity = {"uid": "intruder", "role": "conductor"}
        self.assertEqual(self.client.get("/verify/ABC123XYZ/status").status_code, 404)


if __name__ == "__main__":
    unittest.main()

    unittest.main()
