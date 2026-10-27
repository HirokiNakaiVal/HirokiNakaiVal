"""ログイン（ユーザー・パスワード・署名付きセッション、社内SSO認証）"""
import os, json, time, hmac, base64, hashlib, secrets, threading
import urllib.request, urllib.error
from urllib.parse import quote_plus
from . import db

COOKIE = "kadou_session"
IDLE_SECONDS = int(os.environ.get("KADOU_SESSION_HOURS", "12")) * 3600
INITIAL_ID, INITIAL_PW = "999999", "999999"
ADMIN_COOKIE = "kadou_admin"
ADMIN_IDLE_SECONDS = int(os.environ.get("KADOU_ADMIN_MINUTES", "30")) * 60
INITIAL_ADMIN_PW = "999999"
MAX_FAIL, LOCK_SECONDS = 5, 300
ITER = 200_000

_fail = {}                       # login_id -> (失敗回数, 最終失敗時刻)
_fail_lock = threading.Lock()

USERS_SQL = """
CREATE TABLE IF NOT EXISTS app_config (key TEXT PRIMARY KEY, value TEXT);
CREATE TABLE IF NOT EXISTS users (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  login_id TEXT NOT NULL UNIQUE,
  name TEXT,
  pw_hash TEXT NOT NULL,
  salt TEXT NOT NULL,
  is_admin INTEGER DEFAULT 0,
  must_change INTEGER DEFAULT 0,
  created_at TEXT, last_login TEXT, pw_changed_at TEXT
);
"""

# ---------------------------------------------------------------- 鍵・ハッシュ
_SECRET = None
def _secret():
    """セッション署名の鍵（データ保存先の secret.key。無ければ作る）"""
    global _SECRET
    if _SECRET is None:
        p = os.path.join(db.DATA_DIR, "secret.key")
        if not os.path.exists(p):
            with open(p, "w") as f: f.write(secrets.token_hex(32))
        _SECRET = open(p).read().strip().encode()
    return _SECRET

def hash_pw(pw, salt=None):
    salt = salt or secrets.token_hex(16)
    h = hashlib.pbkdf2_hmac("sha256", pw.encode("utf-8"), salt.encode(), ITER).hex()
    return h, salt

def check_pw(pw, h, salt):
    return hmac.compare_digest(hash_pw(pw, salt)[0], h)

# ---------------------------------------------------------------- ユーザー
def init():
    with db._lock, db.conn() as c:
        c.executescript(USERS_SQL)
        if not c.execute("SELECT 1 FROM app_config WHERE key='admin_pw'").fetchone():
            h, s_ = hash_pw(INITIAL_ADMIN_PW)
            c.execute("INSERT INTO app_config VALUES('admin_pw', ?)", (json.dumps({"h": h, "s": s_, "changed": None}),))
        if "auth" not in [r[1] for r in c.execute("PRAGMA table_info(users)")]:
            c.execute("ALTER TABLE users ADD COLUMN auth TEXT DEFAULT 'local'")   # local / sso
        if not c.execute("SELECT 1 FROM users LIMIT 1").fetchone():
            h, s = hash_pw(INITIAL_PW)
            c.execute("INSERT INTO users(login_id,name,pw_hash,salt,is_admin,must_change,created_at) VALUES(?,?,?,?,1,1,?)",
                      (INITIAL_ID, "初期管理者", h, s, db.now()))
    _secret()

def user_by_login(login_id):
    return db.q("SELECT * FROM users WHERE login_id=?", (login_id,), one=True)

def user(uid):
    return db.q("SELECT * FROM users WHERE id=?", (uid,), one=True)

def users():
    return db.q("SELECT * FROM users ORDER BY login_id")

def add_user(login_id, name, pw, is_admin, method="local"):
    # SSOユーザーはこのアプリにパスワードを持たない（照合できない乱数を入れておく）
    h, s = hash_pw(pw if method == "local" else secrets.token_hex(32))
    return db.x("INSERT INTO users(login_id,name,pw_hash,salt,is_admin,must_change,created_at,auth) VALUES(?,?,?,?,?,?,?,?)",
                (login_id, name, h, s, 1 if is_admin else 0, 1 if method == "local" else 0, db.now(), method))

def is_sso(u):
    return (u["auth"] if "auth" in u.keys() else "local") == "sso"

def set_password(uid, pw, must_change=False):
    h, s = hash_pw(pw)
    db.x("UPDATE users SET pw_hash=?, salt=?, must_change=?, pw_changed_at=? WHERE id=?",
         (h, s, 1 if must_change else 0, db.now(), uid))

def is_initial_pw(u):
    return not is_sso(u) and u["login_id"] == INITIAL_ID and check_pw(INITIAL_PW, u["pw_hash"], u["salt"])

def password_problem(pw, login_id=""):
    if len(pw) < 8: return "パスワードは8文字以上にしてください"
    if pw == login_id: return "IDと同じパスワードは使えません"
    if pw.isdigit() and len(set(pw)) == 1: return "同じ数字だけのパスワードは使えません"
    return None

# ---------------------------------------------------------------- ログイン試行の制限
def locked(login_id):
    with _fail_lock:
        n, t = _fail.get(login_id, (0, 0))
        if n >= MAX_FAIL and time.time() - t < LOCK_SECONDS: return int(LOCK_SECONDS - (time.time() - t))
        if n >= MAX_FAIL: _fail.pop(login_id, None)
    return 0

class AuthError(Exception):
    """ID・パスワード違い以外でログインできない（SSOに接続できない、未登録など）"""

def authenticate(login_id, pw):
    """ローカルユーザーはこのアプリのパスワード、それ以外はSSOで照合。成功ならユーザーを返す"""
    if locked(login_id): return None
    u = user_by_login(login_id)
    err = None
    if u is not None and not is_sso(u):
        ok = check_pw(pw, u["pw_hash"], u["salt"])
    else:
        cfg = sso_config()
        # 未登録のIDはSSOに問い合わせない（社員のパスワード確認に使われないように）
        if not cfg["enabled"]:
            ok = False
        elif u is None and not cfg["auto_register"]:
            ok, err = False, AuthError("このアプリの利用登録がありません。管理者に、マスタ・設定の「ユーザー管理」での登録を依頼してください")
        else:
            try:
                ok = sso_check(login_id, pw, cfg)
            except AuthError as e:
                ok, err = False, e
            if ok and u is None:
                add_user(login_id, "", "", False, method="sso")
                u = user_by_login(login_id)
    with _fail_lock:
        if ok: _fail.pop(login_id, None)
        elif err is None:                    # SSOに接続できないときはパスワード違いではないので数えない
            n, _ = _fail.get(login_id, (0, 0)); _fail[login_id] = (n + 1, time.time())
    if err: raise err
    if ok: db.x("UPDATE users SET last_login=? WHERE id=?", (db.now(), u["id"]))
    return u if ok else None

# ---------------------------------------------------------------- 社内SSO（SSOのhttp認証仕様.xlsx）
# 社員コードとパスワードを SSO サーバーへ POST し、200 なら成功、404 なら失敗
SSO_DEFAULT = {"enabled": True, "url": "https://sso.valorholdings.co.jp/", "app_path": "",
               "encoding": "utf-8", "timeout": 10, "auto_register": False}

def sso_config():
    r = db.q("SELECT value FROM app_config WHERE key='sso'", one=True)
    cfg = dict(SSO_DEFAULT)
    if r: cfg.update(json.loads(r["value"]))
    return cfg

def set_sso_config(cfg):
    db.x("INSERT OR REPLACE INTO app_config VALUES('sso', ?)", (json.dumps(cfg, ensure_ascii=False),))

def sso_body(login_id, pw, cfg):
    enc = cfg["encoding"]
    if cfg["app_path"]:
        return (f"module=SSO&action=AgentAuth&login_id={quote_plus(login_id, encoding=enc)}"
                f"&login_password={quote_plus(pw, encoding=enc)}&app_path={quote_plus(cfg['app_path'], encoding=enc)}")
    return f"module=SSO&action=REST&id={quote_plus(login_id, encoding=enc)}&pwd={quote_plus(pw, encoding=enc)}"

def sso_status(login_id, pw, cfg=None):
    """SSOサーバーの応答ステータスを返す（接続できなければ AuthError）"""
    cfg = cfg or sso_config()
    req = urllib.request.Request(cfg["url"], data=sso_body(login_id, pw, cfg).encode("ascii"), method="POST",
                                 headers={"Content-Type": f"application/x-www-form-urlencoded; charset={cfg['encoding']}"})
    try:
        with urllib.request.urlopen(req, timeout=float(cfg["timeout"])) as r:
            return r.status
    except urllib.error.HTTPError as e:
        return e.code
    except Exception as e:
        raise AuthError(f"SSOサーバーに接続できません（{type(e).__name__}）。しばらくしてからやり直すか、管理者に連絡してください")

def sso_check(login_id, pw, cfg=None):
    cfg = cfg or sso_config()
    if not login_id or not pw: return False
    # REST（app_path空白）はこのSSOではパスワードを検証しないため、ログインには使わせない
    if not cfg["app_path"]:
        raise AuthError("SSOの設定が未完了です（app_path が未設定）。管理者に連絡してください")
    st = sso_status(login_id, pw, cfg)
    if st == 200: return True
    if st == 404: return False
    raise AuthError(f"SSOサーバーの応答が想定外です（ステータス {st}）。管理者に連絡してください")

# ---------------------------------------------------------------- セッション（署名付きクッキー）
def _sign(data):
    return base64.urlsafe_b64encode(hmac.new(_secret(), data, hashlib.sha256).digest()).decode().rstrip("=")

def make_token(u):
    # パスワードを変えると古いセッションが無効になるよう、パスワードハッシュの一部も入れる
    payload = json.dumps({"uid": u["id"], "t": int(time.time()), "v": u["pw_hash"][:12]}).encode()
    b = base64.urlsafe_b64encode(payload).decode().rstrip("=")
    return f"{b}.{_sign(b.encode())}"

def read_token(tok):
    """有効ならユーザーを返す（最終アクセスから IDLE_SECONDS を過ぎたら無効）"""
    try:
        b, sig = tok.split(".", 1)
        if not hmac.compare_digest(sig, _sign(b.encode())): return None
        payload = json.loads(base64.urlsafe_b64decode(b + "=" * (-len(b) % 4)))
        if time.time() - payload["t"] > IDLE_SECONDS: return None
        u = user(payload["uid"])
        if not u or u["pw_hash"][:12] != payload.get("v"): return None
        return u
    except Exception:
        return None


# ---------------------------------------------------------------- 管理者パスワード（マスタ・設定に入るとき）
def _admin_rec():
    r = db.q("SELECT value FROM app_config WHERE key='admin_pw'", one=True)
    return json.loads(r["value"]) if r else None

def check_admin_pw(pw):
    r = _admin_rec()
    return bool(r) and check_pw(pw, r["h"], r["s"])

def set_admin_pw(pw):
    h, s_ = hash_pw(pw)
    db.x("INSERT OR REPLACE INTO app_config VALUES('admin_pw', ?)", (json.dumps({"h": h, "s": s_, "changed": db.now()}),))

def admin_pw_is_initial():
    r = _admin_rec()
    return bool(r) and not r.get("changed")

def admin_pw_changed_at():
    r = _admin_rec()
    return (r or {}).get("changed")

def make_admin_token(uid):
    r = _admin_rec() or {"h": ""}
    payload = json.dumps({"uid": uid, "t": int(time.time()), "v": r["h"][:12]}).encode()
    b = base64.urlsafe_b64encode(payload).decode().rstrip("=")
    return f"{b}.{_sign(b'admin:' + b.encode())}"

def admin_unlocked(tok, uid):
    """ログイン中の本人が、管理者パスワードを入れてから ADMIN_IDLE_SECONDS 以内か"""
    try:
        b, sig = tok.split(".", 1)
        if not hmac.compare_digest(sig, _sign(b"admin:" + b.encode())): return False
        p = json.loads(base64.urlsafe_b64decode(b + "=" * (-len(b) % 4)))
        r = _admin_rec() or {"h": ""}
        return p["uid"] == uid and time.time() - p["t"] <= ADMIN_IDLE_SECONDS and p.get("v") == r["h"][:12]
    except Exception:
        return False
