import json
import base64
import hashlib
import hmac
import os
import getpass
import time
import urllib.error
import urllib.request


gotrue_url = os.environ.get("CK_GOTRUE_INTERNAL_URL", "http://auth:9999").rstrip("/")
email = os.environ.get("CK_ADMIN_EMAIL", "").strip()
password = os.environ.get("CK_ADMIN_PASSWORD", "")
service_key = (
    os.environ.get("CK_SUPABASE_SERVICE_KEY", "").strip()
    or os.environ.get("SERVICE_ROLE_KEY", "").strip()
)


def local_service_key() -> str:
    secret = os.environ.get("JWT_SECRET", "").strip()
    if not secret:
        return ""
    now = int(time.time())
    header = {"alg": "HS256", "typ": "JWT"}
    payload = {"role": "service_role", "iss": "supabase", "iat": now, "exp": now + 3600}

    def encode(value: dict) -> str:
        raw = json.dumps(value, separators=(",", ":")).encode()
        return base64.urlsafe_b64encode(raw).rstrip(b"=").decode()

    unsigned = f"{encode(header)}.{encode(payload)}"
    signature = hmac.new(
        secret.encode(), unsigned.encode(), hashlib.sha256
    ).digest()
    return f"{unsigned}.{base64.urlsafe_b64encode(signature).rstrip(b'=').decode()}"


def request(path: str, method: str = "GET", body: dict | None = None) -> tuple[int, dict]:
    payload = None if body is None else json.dumps(body).encode()
    headers = {"Authorization": f"Bearer {service_key}"}
    if payload is not None:
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(
        f"{gotrue_url}{path}", data=payload, headers=headers, method=method
    )
    try:
        with urllib.request.urlopen(req, timeout=10) as response:
            return response.status, json.loads(response.read() or b"{}")
    except urllib.error.HTTPError as error:
        detail = error.read().decode(errors="replace")
        try:
            return error.code, json.loads(detail)
        except json.JSONDecodeError:
            return error.code, {"error": detail}


if not email:
    print("CK_ADMIN_EMAIL is empty; skipping initial admin bootstrap.")
    raise SystemExit(0)
if not password:
    password = getpass.getpass("Initial admin password: ")
    confirmation = getpass.getpass("Confirm initial admin password: ")
    if not password or password != confirmation:
        raise SystemExit("Passwords were empty or did not match")
if not service_key:
    service_key = local_service_key()
if not service_key:
    raise SystemExit(
        "Set SERVICE_ROLE_KEY, CK_SUPABASE_SERVICE_KEY, or JWT_SECRET for admin bootstrap"
    )

for _ in range(60):
    try:
        with urllib.request.urlopen(f"{gotrue_url}/health", timeout=3):
            break
    except (OSError, urllib.error.HTTPError):
        time.sleep(2)
else:
    raise SystemExit("GoTrue did not become ready within 120 seconds")

status, data = request(
    "/admin/users",
    method="POST",
    body={
        "email": email,
        "password": password,
        "email_confirm": True,
        "app_metadata": {"tier": "platform_admin"},
    },
)
if status in (200, 201):
    print(f"Created platform admin {email}")
    raise SystemExit(0)

if status not in (400, 422) or "exist" not in json.dumps(data).lower():
    raise SystemExit(f"GoTrue admin user creation failed ({status}): {data}")

status, users = request("/admin/users?per_page=100")
if status != 200:
    raise SystemExit(f"Could not list GoTrue users ({status}): {users}")
matches = [user for user in users.get("users", []) if user.get("email") == email]
if not matches:
    raise SystemExit(f"GoTrue reports {email} exists, but it was not returned by user listing")

user_id = matches[0]["id"]
status, updated = request(
    f"/admin/users/{user_id}",
    method="PUT",
    body={
        "password": password,
        "email_confirm": True,
        "app_metadata": {"tier": "platform_admin"},
    },
)
if status not in (200, 201):
    raise SystemExit(f"Could not update platform admin ({status}): {updated}")
print(f"Updated platform admin {email}")
