import base64
from pathlib import Path

from cryptography.hazmat.primitives.asymmetric.padding import PKCS1v15
from cryptography.hazmat.primitives.serialization import load_pem_public_key
from cryptography.x509 import load_pem_x509_certificate

from .config import settings


class SecurityCredentialError(Exception):
    """Raised when the security credential cannot be generated."""


def generate_security_credential() -> str:
    """
    Encrypt the initiator password with Safaricom's public certificate, as
    required for TransactionStatusQuery (and other B2C-style) requests.

    Download the cert from Daraja's docs page ("Certificate for Sandbox" or
    "Certificate for Production") and place it at settings.CERT_PATH.
    """
    cert_path = Path(settings.CERT_PATH)
    if not cert_path.exists():
        raise SecurityCredentialError(
            f"Certificate not found at {cert_path}. Download the sandbox or "
            f"production public key cert from Daraja and place it there "
            f"(see README)."
        )

    try:
        cert_bytes = cert_path.read_bytes()
        try:
            public_key = load_pem_public_key(cert_bytes)
        except ValueError:
            # Some Daraja certs are shipped as X.509 certs rather than bare
            # public keys — handle both.
            cert = load_pem_x509_certificate(cert_bytes)
            public_key = cert.public_key()

        encrypted = public_key.encrypt(
            settings.INITIATOR_PASSWORD.encode(),
            PKCS1v15(),
        )
        return base64.b64encode(encrypted).decode()
    except SecurityCredentialError:
        raise
    except Exception as e:
        raise SecurityCredentialError(f"Failed to generate security credential: {e}")
