"""Passkey credentials and single-use, session-bound WebAuthn challenges."""

import hashlib
import json
import secrets
import time
from pathlib import Path
from urllib.parse import urlsplit

from filelock import FileLock
from webauthn import (
    generate_registration_options, generate_authentication_options,
    verify_registration_response, verify_authentication_response,
)
from webauthn.helpers import bytes_to_base64url, base64url_to_bytes, options_to_json
from webauthn.helpers.structs import (
    AuthenticatorSelectionCriteria, PublicKeyCredentialDescriptor,
    ResidentKeyRequirement, UserVerificationRequirement,
)

from utils.config import read_app_settings, _save_json_file
from utils.secure_storage import read_json
from webui.users import find_web_user, normalize_username, stable_user_id


PASSKEYS_FILE = Path(__file__).resolve().parents[1] / "webui_passkeys.json"
CHALLENGE_SECONDS = 120
MAX_CHALLENGES = 512
MAX_KEYS_PER_USER = 10


def login_identity(username):
    settings = read_app_settings()
    admin = str(settings.get("admin_username") or "admin")
    if normalize_username(username) == normalize_username(admin) and settings.get("admin_password_hash"):
        return {"username": admin, "role": "admin", "account_refs": [],
                "auth_version": int(settings.get("admin_session_version") or 0),
                "user_key": "admin:" + hashlib.sha256(settings["session_secret"].encode()).hexdigest()}
    user = find_web_user(username)
    if not user or not user["enabled"]:
        return None
    return {"username": user["username"], "role": "user", "account_refs": user["account_refs"],
            "auth_version": user["session_version"], "user_key": "user:" + stable_user_id(user)}


class PasskeyStore:
    def __init__(self, rp_id, origin):
        parsed = urlsplit(origin)
        if not rp_id or not parsed.hostname or not (
            parsed.hostname == rp_id or parsed.hostname.endswith("." + rp_id)
        ) or parsed.scheme not in {"https", "http"} or (
            parsed.scheme == "http" and parsed.hostname != "localhost"
        ) or parsed.path or parsed.query or parsed.fragment or parsed.username or parsed.password:
            raise ValueError("Passkeys require an RP ID and an HTTPS origin without a path (localhost may use HTTP).")
        self.rp_id = rp_id
        self.origin = origin

    def _change(self, operation):
        with FileLock(f"{PASSKEYS_FILE}.lock", timeout=30):
            data = read_json(PASSKEYS_FILE) if PASSKEYS_FILE.exists() else {"credentials": [], "challenges": {}}
            now = time.time()
            data["challenges"] = {key: value for key, value in data["challenges"].items() if value["expires"] > now}
            try:
                return operation(data)
            finally:
                _save_json_file(PASSKEYS_FILE, data)

    def list_keys(self, identity):
        return self._change(lambda data: [
            {"id": key["id"], "label": key["label"], "created_at": key["created_at"]}
            for key in data["credentials"] if key["user_key"] == identity["user_key"]
        ])

    def delete(self, identity, credential_id, *, authorize=None):
        def change(data):
            if authorize:
                authorize()
            data["credentials"] = [key for key in data["credentials"]
                                   if not (key["user_key"] == identity["user_key"] and key["id"] == credential_id)]
        self._change(change)

    def revoke_all(self, user_key, *, authorize=None):
        def change(data):
            if authorize:
                authorize()
            data["credentials"] = [key for key in data["credentials"] if key["user_key"] != user_key]
            data["challenges"] = {key: value for key, value in data["challenges"].items()
                                  if value["identity"]["user_key"] != user_key}
        self._change(change)

    def begin(self, identity, binding, *, registration=False, authorize=None):
        def change(data):
            if authorize:
                authorize()
            data["challenges"] = {key: value for key, value in data["challenges"].items() if value["binding"] != binding}
            if len(data["challenges"]) >= MAX_CHALLENGES:
                raise ValueError("尝试次数过多，请稍后重试。")
            keys = [key for key in data["credentials"] if key["user_key"] == identity["user_key"] and key["rp_id"] == self.rp_id]
            descriptors = [PublicKeyCredentialDescriptor(id=base64url_to_bytes(key["id"])) for key in keys]
            if registration:
                if len(keys) >= MAX_KEYS_PER_USER:
                    raise ValueError("最多绑定 10 个通行密钥，请先移除不用的密钥。")
                options = generate_registration_options(
                    rp_id=self.rp_id, rp_name="SparkFlow", user_name=identity["username"],
                    user_id=hashlib.sha256(identity["user_key"].encode()).digest(),
                    exclude_credentials=descriptors,
                    authenticator_selection=AuthenticatorSelectionCriteria(
                        resident_key=ResidentKeyRequirement.PREFERRED,
                        user_verification=UserVerificationRequirement.REQUIRED,
                    ),
                )
            else:
                if not keys:
                    raise ValueError("无法使用通行密钥登录，请使用密码。")
                options = generate_authentication_options(
                    rp_id=self.rp_id, allow_credentials=descriptors,
                    user_verification=UserVerificationRequirement.REQUIRED,
                )
            challenge_id = secrets.token_urlsafe(32)
            data["challenges"][challenge_id] = {
                "identity": identity, "binding": binding, "registration": registration,
                "challenge": bytes_to_base64url(options.challenge), "expires": time.time() + CHALLENGE_SECONDS,
                "origin": self.origin, "rp_id": self.rp_id,
            }
            return {"challenge_id": challenge_id, "options": json.loads(options_to_json(options))}
        return self._change(change)

    def finish(self, challenge_id, binding, credential, *, registration=False, label="", identity=None, authorize=None):
        def change(data):
            challenge = data["challenges"].pop(challenge_id, None)
            if authorize:
                authorize()
            if not isinstance(credential, dict):
                raise ValueError("Invalid credential")
            if not challenge or challenge["binding"] != binding or challenge["registration"] != registration:
                raise ValueError("验证已过期，请重新操作。")
            expected = challenge["identity"]
            live = login_identity(expected["username"])
            if not live or any(live[key] != expected[key] for key in ("user_key", "auth_version", "role")):
                raise ValueError("账号状态已变化，请重新登录。")
            if identity and identity["user_key"] != live["user_key"]:
                raise ValueError("账号不匹配。")
            if challenge["origin"] != self.origin or challenge["rp_id"] != self.rp_id:
                raise ValueError("验证地址已变化，请重新操作。")
            common = dict(credential=credential, expected_challenge=base64url_to_bytes(challenge["challenge"]),
                          expected_rp_id=self.rp_id, expected_origin=self.origin, require_user_verification=True)
            if registration:
                result = verify_registration_response(**common)
                if authorize:
                    authorize()
                # Credential or account changes during verification invalidate
                # the result before any new credential is published.
                live = login_identity(expected["username"])
                if not live or any(live[key] != expected[key] for key in ("user_key", "auth_version", "role")):
                    raise ValueError("账号状态已变化，请重新登录。")
                credential_id = bytes_to_base64url(result.credential_id)
                if any(key["id"] == credential_id for key in data["credentials"]):
                    raise ValueError("这个通行密钥已经绑定。")
                if sum(key["user_key"] == live["user_key"] for key in data["credentials"]) >= MAX_KEYS_PER_USER:
                    raise ValueError("绑定数量已达上限。")
                data["credentials"].append({
                    "id": credential_id, "user_key": live["user_key"], "rp_id": self.rp_id,
                    "public_key": bytes_to_base64url(result.credential_public_key), "sign_count": result.sign_count,
                    "label": str(label or "我的通行密钥")[:80], "created_at": time.strftime("%Y-%m-%d", time.gmtime()),
                })
            else:
                key = next((key for key in data["credentials"] if key["id"] == credential.get("id")
                            and key["user_key"] == live["user_key"] and key["rp_id"] == self.rp_id), None)
                if not key:
                    raise ValueError("无法使用此通行密钥。")
                result = verify_authentication_response(
                    **common, credential_public_key=base64url_to_bytes(key["public_key"]),
                    credential_current_sign_count=key["sign_count"],
                )
                if authorize:
                    authorize()
                live = login_identity(expected["username"])
                if not live or any(live[key] != expected[key] for key in ("user_key", "auth_version", "role")):
                    raise ValueError("账号状态已变化，请重新登录。")
                key["sign_count"] = result.new_sign_count
            return live
        return self._change(change)
