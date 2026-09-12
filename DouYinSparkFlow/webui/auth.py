import hashlib
import hmac
import secrets

from utils.config import get_app_settings, save_app_settings


def hash_password(password, salt=None):
    salt = salt or secrets.token_hex(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt.encode("utf-8"), 480000)
    return f"pbkdf2_sha256${salt}${digest.hex()}"


def verify_password(password, stored_hash):
    if not stored_hash or "$" not in stored_hash:
        return False
    try:
        algorithm, salt, digest = stored_hash.split("$", 2)
    except ValueError:
        return False
    if algorithm != "pbkdf2_sha256":
        return False
    candidate = hash_password(password, salt=salt)
    return hmac.compare_digest(candidate, stored_hash)


def is_bootstrapped():
    return bool(get_app_settings().get("admin_password_hash"))


def _session_version(value):
    try:
        return max(0, int(value))
    except (TypeError, ValueError):
        return 0


def bootstrap_admin_password(password, username="admin"):
    settings = get_app_settings(force_reload=True)
    settings["admin_username"] = username.strip() or "admin"
    settings["admin_password_hash"] = hash_password(password)
    settings["admin_session_version"] = _session_version(settings.get("admin_session_version")) + 1
    return save_app_settings(settings)


def update_admin_password(password):
    settings = get_app_settings(force_reload=True)
    settings["admin_password_hash"] = hash_password(password)
    settings["admin_session_version"] = _session_version(settings.get("admin_session_version")) + 1
    return save_app_settings(settings)


def issue_session(request, username, *, role="admin", account_refs=None, auth_version=0):
    request.session.clear()
    request.session["user"] = username
    request.session["role"] = role
    request.session["account_refs"] = list(account_refs or [])
    request.session["auth_version"] = _session_version(auth_version)
    request.session["session_id"] = secrets.token_urlsafe(24)
    request.session["csrf_token"] = secrets.token_urlsafe(24)


def clear_session(request):
    request.session.clear()


def current_user(request):
    return request.session.get("user")


def current_principal(request):
    """Resolve the current session to live role and account assignments."""
    username = current_user(request)
    if not username:
        return None
    session_id = request.session.get("session_id", "")
    role = request.session.get("role")
    session_version = _session_version(request.session.get("auth_version"))
    admin_username = str(get_app_settings().get("admin_username", "admin")).strip() or "admin"
    if role == "admin" or (role is None and username.casefold() == admin_username.casefold()):
        settings = get_app_settings()
        if session_version != _session_version(settings.get("admin_session_version")):
            return None
        return {
            "username": admin_username,
            "role": "admin",
            "account_refs": [],
            "session_id": session_id,
            "enabled": True,
        }

    try:
        from webui.users import find_web_user
        user = find_web_user(username)
    except Exception:
        user = None
    if user and user.get("enabled", True):
        if session_version != _session_version(user.get("session_version")):
            return None
        return {
            "username": user["username"],
            "role": "user",
            "account_refs": list(user.get("account_refs", [])),
            "session_id": session_id,
            "enabled": True,
        }
    return None


def is_admin(request):
    principal = current_principal(request)
    return bool(principal and principal.get("role") == "admin")


def csrf_token(request):
    token = request.session.get("csrf_token")
    if not token:
        token = secrets.token_urlsafe(24)
        request.session["csrf_token"] = token
    return token


def validate_csrf(request, submitted_token):
    stored_token = request.session.get("csrf_token")
    return bool(stored_token and submitted_token and hmac.compare_digest(stored_token, submitted_token))


def is_https_request(request):
    forwarded_proto = request.headers.get("x-forwarded-proto", "")
    if forwarded_proto:
        return forwarded_proto.lower() == "https"
    return request.url.scheme == "https"
