"""Runtime credential lookup without persisting raw agent secrets."""

import hashlib
import hmac


class CredentialStore:
    """Hold provisioned secrets in memory and expose only verification helpers."""

    def __init__(self, secrets: dict[str, str]):
        self._secrets = dict(secrets)

    def secret_for(self, instance_id: str) -> str | None:
        return self._secrets.get(instance_id)

    def secret_hash(self, instance_id: str) -> str | None:
        secret = self.secret_for(instance_id)
        if secret is None:
            return None
        return hashlib.sha256(secret.encode("utf-8")).hexdigest()

    def matches_hash(self, instance_id: str, expected_hash: str) -> bool:
        actual_hash = self.secret_hash(instance_id)
        return actual_hash is not None and hmac.compare_digest(actual_hash, expected_hash)