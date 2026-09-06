import re
import secrets
from dataclasses import dataclass

from argon2 import PasswordHasher
from argon2.exceptions import InvalidHash, VerifyMismatchError
from pydantic import SecretStr

_PUBLIC_KEY_ID_BYTES = 8
_SECRET_BYTES = 32
_KEY_PATTERN = re.compile(
    r"mb_(?P<public_key_id>[0-9a-f]{16})\.(?P<secret>[A-Za-z0-9_-]{43})"
)

_hasher = PasswordHasher(
    time_cost=3,
    memory_cost=65536,
    parallelism=4,
    hash_len=32,
    salt_len=16,
)


class ApiKeyFormatError(ValueError):
    """Raised when an API key does not match the expected format."""


@dataclass(frozen=True)
class GeneratedApiKey:
    public_key_id: str
    key_prefix: str
    secret_hash: str
    plaintext: SecretStr

    def __repr__(self) -> str:
        return (
            f"GeneratedApiKey(public_key_id={self.public_key_id!r}, "
            f"key_prefix={self.key_prefix!r}, "
            "secret_hash=<redacted>, plaintext=<redacted>)"
        )


@dataclass(frozen=True)
class ParsedApiKey:
    public_key_id: str
    secret: SecretStr

    def __repr__(self) -> str:
        return (
            f"ParsedApiKey(public_key_id={self.public_key_id!r}, "
            "secret=<redacted>)"
        )


def generate_api_key() -> GeneratedApiKey:
    public_key_id = secrets.token_hex(_PUBLIC_KEY_ID_BYTES)
    secret = secrets.token_urlsafe(_SECRET_BYTES)
    key_prefix = f"mb_{public_key_id}"
    plaintext = f"{key_prefix}.{secret}"
    secret_hash = hash_api_key_secret(secret)

    return GeneratedApiKey(
        public_key_id=public_key_id,
        key_prefix=key_prefix,
        secret_hash=secret_hash,
        plaintext=SecretStr(plaintext),
    )


def parse_api_key(raw_key: str) -> ParsedApiKey:
    match = _KEY_PATTERN.fullmatch(raw_key)
    if match is None:
        raise ApiKeyFormatError(
            "API key does not match the expected mb_<id>.<secret> format"
        )

    return ParsedApiKey(
        public_key_id=match.group("public_key_id"),
        secret=SecretStr(match.group("secret")),
    )


def hash_api_key_secret(secret: str) -> str:
    return _hasher.hash(secret)


def verify_api_key_secret(stored_hash: str, secret: str) -> bool:
    try:
        return _hasher.verify(stored_hash, secret)
    except (VerifyMismatchError, InvalidHash):
        return False