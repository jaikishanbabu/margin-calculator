"""Admin accounts: password hashing, lockout, tokens, role checks."""
import base64
import hashlib
import hmac
import os
import re
import threading
import time
from datetime import datetime, timedelta, timezone

import jwt

from . import audit, config, db

ROLES = ("superadmin", "admin")
USERNAME_RE = re.compile(r"^[a-z0-9._-]{3,40}$")
PBKDF2_ITERS = 390_000
_DUMMY_HASH = None


class AuthError(Exception):
    def __init__(self, msg, status=401):
        super().__init__(msg)
        self.status = status


def now():
    return datetime.now(timezone.utc)


# ---------- passwords ----------
def hash_password(pw: str) -> str:
    salt = os.urandom(16)
    dk = hashlib.pbkdf2_hmac("sha256", pw.encode(), salt, PBKDF2_ITERS)
    return f"pbkdf2_sha256${PBKDF2_ITERS}${base64.b64encode(salt).decode()}${base64.b64encode(dk).decode()}"


def verify_password(pw: str, stored: str) -> bool:
    try:
        _, iters, salt, dk = stored.split("$")
        calc = hashlib.pbkdf2_hmac("sha256", pw.encode(), base64.b64decode(salt), int(iters))
        return hmac.compare_digest(calc, base64.b64decode(dk))
    except Exception:
        return False


def _dummy_verify(pw):
    """Spend the same time for unknown users so usernames can't be probed by timing."""
    global _DUMMY_HASH
    if _DUMMY_HASH is None:
        _DUMMY_HASH = hash_password("dummy-password-for-timing")
    verify_password(pw, _DUMMY_HASH)


def check_password_rules(pw: str, username=""):
    pw = pw or ""
    if len(pw) < 12:
        raise ValueError("Password must be at least 12 characters")
    if username and username.lower() in pw.lower():
        raise ValueError("Password must not contain the username")
    classes = sum(bool(re.search(p, pw)) for p in (r"[a-z]", r"[A-Z]", r"\d", r"[^A-Za-z0-9]"))
    if classes < 3:
        raise ValueError("Password needs at least 3 of: lowercase, uppercase, digit, symbol")


# ---------- per-IP throttle (in memory; DB lockout below survives restarts) ----------
_ip_hits = {}
_ip_lock = threading.Lock()


def throttle_ip(ip):
    t = time.time()
    with _ip_lock:
        hits = [h for h in _ip_hits.get(ip, []) if t - h < 60]
        if len(hits) >= config.IP_LOGIN_LIMIT_PER_MIN:
            _ip_hits[ip] = hits
            return False
        hits.append(t)
        _ip_hits[ip] = hits
        return True


def reset_throttle():
    with _ip_lock:
        _ip_hits.clear()


# ---------- tokens ----------
def make_token(user):
    t = now()
    payload = {"sub": user["username"], "role": user["role"], "tv": user["token_version"],
               "iat": int(t.timestamp()), "exp": int((t + timedelta(hours=config.TOKEN_HOURS)).timestamp()),
               "iss": "margin-engine"}
    return jwt.encode(payload, _secret(), algorithm="HS256")


def _secret():
    if config.JWT_SECRET:
        return config.JWT_SECRET
    if config.IS_DEV:
        return "dev-only-secret-not-for-deployment-000000"
    raise RuntimeError("JWT_SECRET missing")


def user_from_token(token: str):
    try:
        p = jwt.decode(token, _secret(), algorithms=["HS256"], issuer="margin-engine",
                       options={"require": ["exp", "iat", "sub", "tv"]})
    except jwt.PyJWTError:
        return None
    u = db.query_one("SELECT * FROM users WHERE username = ?", (p["sub"],))
    if not u or not u["active"] or u["token_version"] != p["tv"]:
        return None  # disabled, password changed or role changed -> old tokens die
    return u


# ---------- users ----------
def public_user(u):
    locked = bool(u.get("locked_until") and u["locked_until"] > now().isoformat())
    return {"id": u["id"], "username": u["username"], "full_name": u["full_name"], "role": u["role"],
            "active": bool(u["active"]), "locked": locked, "created_at": u["created_at"],
            "created_by": u["created_by"], "last_login_at": u["last_login_at"],
            "must_change_password": bool(u.get("must_change_password"))}


def ensure_superadmin():
    """SUPERADMIN_PASSWORD only bootstraps the account. It is never re-applied, except when the
    break-glass switch SUPERADMIN_RESET=1 is set (audited; forces another password change)."""
    pw = config.SUPERADMIN_PASSWORD
    name = config.SUPERADMIN_USERNAME
    with db.tx() as c:
        u = c.execute("SELECT * FROM users WHERE username = ?", (name,)).fetchone()
        if not u:
            if not pw:
                print("WARNING: SUPERADMIN_PASSWORD not set - no SuperAdmin account created")
                return
            c.insert("INSERT INTO users (username, full_name, pw_hash, role, active, must_change_password, created_at, created_by) "
                     "VALUES (?, 'Super Admin', ?, 'superadmin', 1, 1, ?, 'system')", (name, hash_password(pw), now().isoformat()))
            audit.append(c, "system", "superadmin_bootstrapped", f"{name} (must change password at first login)")
        elif config.SUPERADMIN_RESET and pw:
            c.execute("UPDATE users SET pw_hash = ?, role = 'superadmin', active = 1, failed_logins = 0, locked_until = NULL, "
                      "must_change_password = 1, token_version = token_version + 1 WHERE id = ?", (hash_password(pw), u["id"]))
            audit.append(c, "system", "superadmin_break_glass_reset",
                         "SUPERADMIN_RESET=1: password re-applied from server secret; change required; remove SUPERADMIN_RESET")


def login(username, password, ip=None):
    username = (username or "").strip().lower()
    if not throttle_ip(ip or "-"):
        audit.log(username, "login_throttled", "", ip)
        raise AuthError("Too many attempts. Wait a minute and try again.", 429)
    err = None
    with db.tx() as c:
        u = c.execute("SELECT * FROM users WHERE username = ?", (username,)).fetchone()
        if not u:
            _dummy_verify(password or "")
            audit.append(c, username, "login_failed", "unknown user", ip)
            err = AuthError("Wrong username or password")
        elif u["locked_until"] and u["locked_until"] > now().isoformat():
            audit.append(c, username, "login_blocked", "account locked", ip)
            err = AuthError("Account temporarily locked after repeated failures. Try again later.", 423)
        elif not u["active"]:
            _dummy_verify(password or "")
            audit.append(c, username, "login_failed", "account disabled", ip)
            err = AuthError("Wrong username or password")
        elif not verify_password(password or "", u["pw_hash"]):
            fails = u["failed_logins"] + 1
            locked = None
            if fails >= config.LOCKOUT_THRESHOLD:
                locked = (now() + timedelta(minutes=config.LOCKOUT_MINUTES)).isoformat()
                fails = 0
            c.execute("UPDATE users SET failed_logins = ?, locked_until = ? WHERE id = ?", (fails, locked, u["id"]))
            audit.append(c, username, "login_failed", "wrong password" + ("; account locked" if locked else ""), ip)
            err = AuthError("Wrong username or password")
        else:
            c.execute("UPDATE users SET failed_logins = 0, locked_until = NULL, last_login_at = ? WHERE id = ?",
                      (now().isoformat(), u["id"]))
            audit.append(c, username, "login", "", ip)
            u = c.execute("SELECT * FROM users WHERE id = ?", (u["id"],)).fetchone()
    if err:
        raise err
    return u


def require(user, *roles):
    if user is None:
        raise AuthError("Login required", 401)
    if roles and user["role"] not in roles:
        raise AuthError("You don't have permission for this", 403)


def create_user(actor, username, password, full_name="", role="admin", ip=None):
    require(actor, "superadmin")
    username = (username or "").strip().lower()
    if not USERNAME_RE.match(username):
        raise ValueError("Username: 3-40 chars, lowercase letters, digits, . _ -")
    if role not in ROLES:
        raise ValueError("Invalid role")
    check_password_rules(password, username)
    with db.tx() as c:
        if c.execute("SELECT id FROM users WHERE username = ?", (username,)).fetchone():
            raise ValueError("Username already exists")
        uid = c.insert("INSERT INTO users (username, full_name, pw_hash, role, active, must_change_password, created_at, created_by) "
                       "VALUES (?, ?, ?, ?, 1, 1, ?, ?)",
                       (username, (full_name or "")[:80], hash_password(password), role, now().isoformat(), actor["username"]))
        audit.append(c, actor["username"], "user_created", {"username": username, "role": role}, ip)
        return public_user(c.execute("SELECT * FROM users WHERE id = ?", (uid,)).fetchone())


def update_user(actor, user_id, active=None, password=None, full_name=None, role=None, unlock=False, ip=None):
    require(actor, "superadmin")
    with db.tx() as c:
        u = c.execute("SELECT * FROM users WHERE id = ?", (user_id,)).fetchone()
        if not u:
            raise LookupError("User not found")
        if u["username"] == config.SUPERADMIN_USERNAME:
            raise ValueError("The main SuperAdmin can't be changed from here (use Account to change its own password)")
        changes, bump = [], False
        if active is not None:
            c.execute("UPDATE users SET active = ? WHERE id = ?", (1 if active else 0, user_id))
            changes.append("enabled" if active else "disabled")
            bump = bump or not active
        if password:
            check_password_rules(password, u["username"])
            c.execute("UPDATE users SET pw_hash = ?, failed_logins = 0, locked_until = NULL, must_change_password = 1 WHERE id = ?",
                      (hash_password(password), user_id))
            changes.append("password reset")
            bump = True
        if full_name is not None:
            c.execute("UPDATE users SET full_name = ? WHERE id = ?", (full_name[:80], user_id))
            changes.append("name changed")
        if role:
            if role not in ROLES:
                raise ValueError("Invalid role")
            if role != u["role"]:
                c.execute("UPDATE users SET role = ? WHERE id = ?", (role, user_id))
                changes.append(f"role {u['role']}->{role}")
                bump = True
        if unlock:
            c.execute("UPDATE users SET failed_logins = 0, locked_until = NULL WHERE id = ?", (user_id,))
            changes.append("unlocked")
        if bump:
            c.execute("UPDATE users SET token_version = token_version + 1 WHERE id = ?", (user_id,))
        audit.append(c, actor["username"], "user_updated", {"username": u["username"], "changes": changes}, ip)
        return public_user(c.execute("SELECT * FROM users WHERE id = ?", (user_id,)).fetchone())


def change_own_password(user, old, new, ip=None):
    require(user)
    if not verify_password(old or "", user["pw_hash"]):
        raise ValueError("Current password is wrong")
    if verify_password(new or "", user["pw_hash"]):
        raise ValueError("New password must be different from the current one")
    if user["role"] == "superadmin":
        if len(new or "") < 16:
            raise ValueError("SuperAdmin password must be at least 16 characters")
        if config.SUPERADMIN_PASSWORD and new == config.SUPERADMIN_PASSWORD:
            raise ValueError("Choose a password different from the deployment bootstrap password")
    check_password_rules(new, user["username"])
    with db.tx() as c:
        c.execute("UPDATE users SET pw_hash = ?, must_change_password = 0, token_version = token_version + 1 WHERE id = ?",
                  (hash_password(new), user["id"]))
        audit.append(c, user["username"], "password_changed", "first-login change" if user["must_change_password"] else "", ip)
