import os
import unittest

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
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

NETLIFY = "https://thibitifare.netlify.app"
PREFLIGHT_HEADERS = {
    "Access-Control-Request-Method": "POST",
    "Access-Control-Request-Headers": "authorization,content-type,idempotency-key",
}


def _app_with_origins(raw: str) -> FastAPI:
    """Mirrors the production middleware wiring using the real parser and defaults."""
    app = FastAPI()
    app.add_middleware(
        CORSMiddleware,
        allow_origins=[*main._LOCAL_DEV_ORIGINS, *main._parse_frontend_origins(raw)],
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    @app.get("/")
    def root():
        return {"ok": True}

    return app


class CorsConfigurationTests(unittest.TestCase):
    def test_parser_trims_and_ignores_blank_entries(self):
        self.assertEqual(
            main._parse_frontend_origins(f" {NETLIFY} , ,https://other.example,"),
            [NETLIFY, "https://other.example"],
        )
        self.assertEqual(main._parse_frontend_origins(""), [])

    def test_configured_netlify_origin_is_allowed_for_requests_and_preflight(self):
        client = TestClient(_app_with_origins(NETLIFY))
        simple = client.get("/", headers={"Origin": NETLIFY})
        preflight = client.options("/", headers={"Origin": NETLIFY, **PREFLIGHT_HEADERS})

        self.assertEqual(simple.headers.get("access-control-allow-origin"), NETLIFY)
        self.assertEqual(preflight.status_code, 200)
        self.assertEqual(preflight.headers.get("access-control-allow-origin"), NETLIFY)
        allowed = preflight.headers.get("access-control-allow-headers", "").lower()
        self.assertIn("idempotency-key", allowed)
        self.assertIn("authorization", allowed)

    def test_unrelated_origin_is_not_allowed(self):
        client = TestClient(_app_with_origins(NETLIFY))
        evil = "https://evil.example"
        simple = client.get("/", headers={"Origin": evil})
        preflight = client.options("/", headers={"Origin": evil, **PREFLIGHT_HEADERS})

        self.assertNotIn("access-control-allow-origin", simple.headers)
        self.assertEqual(preflight.status_code, 400)
        self.assertNotIn("access-control-allow-origin", preflight.headers)

    def test_netlify_origin_is_not_allowed_when_not_configured(self):
        client = TestClient(_app_with_origins(""))
        response = client.get("/", headers={"Origin": NETLIFY})
        self.assertNotIn("access-control-allow-origin", response.headers)

    def test_local_development_origins_stay_allowed(self):
        client = TestClient(_app_with_origins(""))
        for origin in ("http://localhost:5173", "http://127.0.0.1:5173"):
            with self.subTest(origin=origin):
                response = client.get("/", headers={"Origin": origin})
                self.assertEqual(response.headers.get("access-control-allow-origin"), origin)

    def test_real_app_uses_cors_middleware_with_local_origins_and_no_wildcard(self):
        cors = [m for m in main.app.user_middleware if m.cls is CORSMiddleware]
        self.assertEqual(len(cors), 1)
        origins = cors[0].kwargs["allow_origins"]
        for origin in main._LOCAL_DEV_ORIGINS:
            self.assertIn(origin, origins)
        self.assertNotIn("*", origins)

    def test_real_app_rejects_preflight_from_unrelated_origin(self):
        client = TestClient(main.app)
        response = client.options(
            "/stk-push",
            headers={"Origin": "https://evil.example", **PREFLIGHT_HEADERS},
        )
        self.assertNotIn("access-control-allow-origin", response.headers)


if __name__ == "__main__":
    unittest.main()
