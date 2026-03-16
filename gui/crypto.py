from cryptography.fernet import Fernet
import os


def get_fernet() -> Fernet:
    key = os.environ.get("SECRET_KEY")
    if not key:
        raise RuntimeError("SECRET_KEY environment variable is not set")
    return Fernet(key.encode() if isinstance(key, str) else key)


def encrypt_value(f: Fernet, plaintext: str) -> str:
    return f.encrypt(plaintext.encode()).decode()


def decrypt_value(f: Fernet, ciphertext: str) -> str:
    return f.decrypt(ciphertext.encode()).decode()


def is_encrypted(value: str) -> bool:
    """Detect Fernet tokens — they start with gAAAAA"""
    return isinstance(value, str) and value.startswith("gAAAAA")
