"""
Sign-in helpers. PINs are never stored, only a salted, slow hash (PBKDF2), so the
config file doesn't reveal them. These are demo credentials for a fake market.
"""
import hashlib
import hmac


def pin_hash(user, pin):
    return hashlib.pbkdf2_hmac("sha256", pin.encode(), f"bayleaf:{user}".encode(), 100_000).hex()


def check_pin(users, user, pin):
    u = users.get(user)
    if not u or not u.get("pin_hash") or not pin:
        return False
    return hmac.compare_digest(u["pin_hash"], pin_hash(user, pin))
