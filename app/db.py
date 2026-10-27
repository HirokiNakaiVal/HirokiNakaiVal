"""SQLite：組織階層（会社・事業部・店舗）、取込ファイル、作成ジョブ"""
import os, json, sqlite3, threading, datetime as dt

DATA_DIR = os.environ.get("KADOU_DATA_DIR", os.path.join(os.path.dirname(os.path.dirname(__file__)), "data"))
os.makedirs(DATA_DIR, exist_ok=True)
DB_PATH = os.path.join(DATA_DIR, "app.db")
_lock = threading.Lock()

SCHEMA = """
CREATE TABLE IF NOT EXISTS org (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  kind TEXT NOT NULL CHECK(kind IN ('company','division','store')),
  code TEXT, name TEXT NOT NULL, parent_id INTEGER REFERENCES org(id),
  settings TEXT DEFAULT '{}', created_at TEXT
);
CREATE TABLE IF NOT EXISTS files (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  store_id INTEGER NOT NULL REFERENCES org(id),
  kind TEXT, filename TEXT, path TEXT, cache TEXT,
  period_from TEXT, period_to TEXT, info TEXT, warning TEXT,
  uploaded_at TEXT
);
CREATE TABLE IF NOT EXISTS labor_budget (
  store_id INTEGER NOT NULL REFERENCES org(id),
  ym TEXT NOT NULL,                       -- YYYY-MM
  regular REAL, part REAL, arbeit REAL, total REAL,
  ki TEXT, source TEXT, imported_at TEXT,
  PRIMARY KEY (store_id, ym)
);
CREATE TABLE IF NOT EXISTS jobs (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  store_id INTEGER NOT NULL REFERENCES org(id),
  date_from TEXT, date_to TEXT, status TEXT, progress TEXT,
  outdir TEXT, result TEXT, error TEXT, created_at TEXT, finished_at TEXT
);
"""

def resolve(path):
    """保存されたパスが見つからない場合（フォルダを移動したときなど）、今のデータ保存先から探し直す"""
    if not path or os.path.exists(path): return path
    norm = path.replace("\\", "/")
    for key in ("/stores/", "/tmp/"):
        i = norm.find(key)
        if i >= 0:
            cand = os.path.join(DATA_DIR, *norm[i + 1:].split("/"))
            if os.path.exists(cand): return cand
    return path

def now():
    return dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S")

def conn():
    c = sqlite3.connect(DB_PATH, timeout=30)
    c.row_factory = sqlite3.Row
    return c

def _cols(c, table):
    return {r[1] for r in c.execute(f"PRAGMA table_info({table})")}

def init():
    with _lock, conn() as c:
        c.executescript(SCHEMA)
        # 列の追加（既存DBの移行）
        if "source" not in _cols(c, "org"): c.execute("ALTER TABLE org ADD COLUMN source TEXT")
        if "info" not in _cols(c, "org"): c.execute("ALTER TABLE org ADD COLUMN info TEXT")
        if "hash" not in _cols(c, "files"): c.execute("ALTER TABLE files ADD COLUMN hash TEXT")
        if "visible" not in _cols(c, "org"): c.execute("ALTER TABLE org ADD COLUMN visible INTEGER DEFAULT 1")
        c.execute("CREATE INDEX IF NOT EXISTS ix_org_kind_code ON org(kind, code)")
        c.execute("CREATE INDEX IF NOT EXISTS ix_org_parent ON org(parent_id)")
        c.execute("CREATE INDEX IF NOT EXISTS ix_files_store ON files(store_id, kind)")
        # 起動時に実行中だったジョブは中断扱い
        c.execute("UPDATE jobs SET status='中断', error='サーバー再起動で中断' WHERE status IN ('待機','実行中')")

def q(sql, args=(), one=False):
    with conn() as c:
        rows = c.execute(sql, args).fetchall()
    return (rows[0] if rows else None) if one else rows

def x(sql, args=()):
    with _lock, conn() as c:
        cur = c.execute(sql, args)
        return cur.lastrowid

# ---- 組織
def orgs(kind=None, parent=None, visible_only=False):
    sql, a = "SELECT * FROM org WHERE 1=1", []
    if kind: sql += " AND kind=?"; a.append(kind)
    if visible_only: sql += " AND COALESCE(visible,1)=1"
    if parent is not None: sql += " AND parent_id=?"; a.append(parent)
    return q(sql + " ORDER BY CAST(code AS INTEGER), code, name", a)

def org(i): return q("SELECT * FROM org WHERE id=?", (i,), one=True)

def find_org(kind, code):
    return q("SELECT * FROM org WHERE kind=? AND code=? ORDER BY id LIMIT 1", (kind, str(code)), one=True)

def count_children(i):
    return q("SELECT COUNT(*) n FROM org WHERE parent_id=?", (i,), one=True)["n"]

def add_org(kind, name, code="", parent_id=None):
    return x("INSERT INTO org(kind,code,name,parent_id,created_at) VALUES(?,?,?,?,?)",
             (kind, code, name, parent_id, now()))

def path_of(store_id):
    s = org(store_id); d = org(s["parent_id"]) if s and s["parent_id"] else None
    c = org(d["parent_id"]) if d and d["parent_id"] else None
    return c, d, s

def settings(store_id):
    s = org(store_id)
    return json.loads(s["settings"] or "{}") if s else {}

def save_settings(store_id, d):
    x("UPDATE org SET settings=? WHERE id=?", (json.dumps(d, ensure_ascii=False), store_id))
