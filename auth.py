"""
Kalshi authenticates trading requests with per-request RSA-PSS signatures,
not a simple bearer token. Every authenticated request needs three headers:

    KALSHI-ACCESS-KEY:       your API key ID
    KALSHI-ACCESS-SIGNATURE: base64 RSA-PSS signature over (timestamp + method + path)
    KALSHI-ACCESS-TIMESTAMP: current time in milliseconds since epoch

Double-check the exact signing payload format (some doc sources show
timestamp+method+path, others include the body) against Kalshi's current
docs before going live — signing bugs fail closed (401s), so it's safe to
iterate on in a paper/sandbox setting.
"""
import time
import base64
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding


class KalshiAuth:
    def __init__(self, api_key_id: str, private_key_path: str):
        if not api_key_id:
            raise ValueError("KALSHI_API_KEY_ID is not set")
        self.api_key_id = api_key_id
        with open(private_key_path, "rb") as f:
            self._private_key = serialization.load_pem_private_key(f.read(), password=None)

    def sign_request(self, method: str, path: str) -> dict:
        """Return the auth headers for one request. `path` should include
        the leading /trade-api/v2 prefix, matching what Kalshi expects to
        verify against."""
        timestamp_ms = str(int(time.time() * 1000))
        message = f"{timestamp_ms}{method.upper()}{path}".encode("utf-8")

        signature = self._private_key.sign(
            message,
            padding.PSS(
                mgf=padding.MGF1(hashes.SHA256()),
                salt_length=padding.PSS.DIGEST_LENGTH,
            ),
            hashes.SHA256(),
        )

        return {
            "KALSHI-ACCESS-KEY": self.api_key_id,
            "KALSHI-ACCESS-SIGNATURE": base64.b64encode(signature).decode("utf-8"),
            "KALSHI-ACCESS-TIMESTAMP": timestamp_ms,
        }
