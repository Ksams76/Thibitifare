import os

from dotenv import load_dotenv

load_dotenv()


def _require(key: str) -> str:
    val = os.getenv(key)
    if not val:
        raise RuntimeError(
            f"Missing required environment variable: {key}. "
            f"Copy .env.example to .env and fill it in."
        )
    return val


class Settings:
    # --- Core Daraja app credentials (from your Daraja app's "Keys" page) ---
    CONSUMER_KEY: str = _require("DARAJA_CONSUMER_KEY")
    CONSUMER_SECRET: str = _require("DARAJA_CONSUMER_SECRET")

    # --- Sandbox test values (from the Daraja "Test Credentials" page) ---
    # 174379 is Safaricom's standard sandbox Lipa Na M-Pesa shortcode — every
    # developer testing in sandbox uses the same one. You only get your own
    # shortcode once you go live with a paybill/till number.
    SHORTCODE: str = os.getenv("DARAJA_SHORTCODE", "174379")
    PASSKEY: str = _require("DARAJA_PASSKEY")

    # --- Environment: "sandbox" or "production" ---
    ENV: str = os.getenv("DARAJA_ENV", "sandbox")

    # --- Your public callback URL (ngrok URL during development, no trailing slash) ---
    CALLBACK_URL: str = _require("DARAJA_CALLBACK_URL")

    # --- Used for TransactionStatusQuery (the /verify endpoint's real path) ---
    INITIATOR_NAME: str = os.getenv("DARAJA_INITIATOR_NAME", "testapi")
    INITIATOR_PASSWORD: str = os.getenv("DARAJA_INITIATOR_PASSWORD", "Safaricom999!*!")
    CERT_PATH: str = os.getenv("DARAJA_CERT_PATH", "certs/sandbox_public_key.pem")

    # --- Demo safety switch: see README "Verify: real vs stub" section ---
    # TransactionStatusQuery is asynchronous (Safaricom posts the real answer
    # to ResultURL later) which makes it awkward to demo live. While this is
    # true, /verify checks against transactions FareGuard has actually seen
    # via its own STK Push callbacks instead of round-tripping to Daraja
    # again. Set DARAJA_VERIFY_STUB=false once transaction-status-result is
    # wired up for production use.
    VERIFY_STUB_MODE: bool = os.getenv("DARAJA_VERIFY_STUB", "true").lower() == "true"

    @property
    def base_url(self) -> str:
        if self.ENV == "production":
            return "https://api.safaricom.co.ke"
        return "https://sandbox.safaricom.co.ke"


settings = Settings()
