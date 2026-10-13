"""Single-account auth. APP_USERNAME + APP_PASSWORD_HASH (bcrypt) from env; signed session cookie; failed-login rate limit."""
from __future__ import annotations
import hmac, os, secrets, time
from collections import defaultdict, deque
import bcrypt
from itsdangerous import BadSignature, URLSafeTimedSerializer

SESSION_COOKIE = "oe_session"
MAX_AGE = int(os.getenv("SESSION_MAX_AGE_S", str(12 * 3600)))
_secret = os.getenv("SECRET_KEY") or secrets.token_urlsafe(32)   # random per-process if unset (sessions reset on restart)
_ser = URLSafeTimedSerializer(_secret, salt="oe-login")
_fails: dict[str, deque] = defaultdict(deque)
FAIL_WINDOW, FAIL_MAX = 15 * 60, 5
_DUMMY = bcrypt.hashpw(b"x", bcrypt.gensalt(rounds=12))

def configured() -> bool:
    return bool(os.getenv("APP_USERNAME") and os.getenv("APP_PASSWORD_HASH"))

def locked(ip: str) -> int:
    q, now = _fails[ip], time.time()
    while q and q[0] < now - FAIL_WINDOW:
        q.popleft()
    return int(q[0] + FAIL_WINDOW - now) if len(q) >= FAIL_MAX else 0

def check(username: str, password: str, ip: str) -> bool:
    if not configured():
        return False
    ok_user = hmac.compare_digest(username.encode(), os.environ["APP_USERNAME"].encode())
    h = os.environ["APP_PASSWORD_HASH"].encode() if ok_user else _DUMMY  # constant-ish time
    ok = bcrypt.checkpw(password.encode()[:72], h) and ok_user
    if ok:
        _fails.pop(ip, None)
    else:
        _fails[ip].append(time.time())
    return ok

def make_session(username: str) -> str:
    return _ser.dumps({"u": username})

def read_session(token: str | None) -> str | None:
    if not token:
        return None
    try:
        d = _ser.loads(token, max_age=MAX_AGE)
    except BadSignature:
        return None
    return d.get("u") if d.get("u") == os.getenv("APP_USERNAME") else None

def check_token(tok: str | None) -> bool:
    """Optional bearer token for API clients: API_TOKEN env."""
    t = os.getenv("API_TOKEN")
    return bool(t and tok and hmac.compare_digest(t, tok))
