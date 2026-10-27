"""日別稼働計画アプリ（社内サーバー用）"""
import os, re, json, secrets, base64
from urllib.parse import quote
from fastapi import FastAPI, Request, UploadFile, File, Form, HTTPException
from fastapi.responses import HTMLResponse, RedirectResponse, FileResponse, Response, JSONResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from . import db, service, master, auth
from engine.common import DEFAULT_SETTINGS, merged_settings
from engine.loaders import KINDS

ROOT = os.path.dirname(os.path.dirname(__file__))
app = FastAPI(title="日別稼働計画")
app.mount("/static", StaticFiles(directory=os.path.join(ROOT, "static")), name="static")
T = Jinja2Templates(directory=os.path.join(ROOT, "templates"))
db.init()
MAX_UPLOAD = int(os.environ.get("KADOU_MAX_UPLOAD_MB", "500")) * 1024 * 1024

auth.init()

# ------------------------------------------------------------ ログイン（個人情報を含むため全画面で必須）
PUBLIC = ("/login", "/static/")
ADMIN_ONLY = ("/admin", "/filter", "/master/", "/org/")
_initial_cache = {}

def _is_initial(u):
    key = (u["id"], u["pw_hash"])
    if key not in _initial_cache: _initial_cache[key] = auth.is_initial_pw(u)
    return _initial_cache[key]

@app.middleware("http")
async def login_required(request: Request, call_next):
    path = request.url.path
    if path == "/login" or path.startswith("/static/"):
        return await call_next(request)
    u = auth.read_token(request.cookies.get(auth.COOKIE, ""))
    if not u:
        if request.method == "GET" and not path.startswith("/api/"):
            nxt = path + (("?" + request.url.query) if request.url.query else "")
            return RedirectResponse("/login?next=" + quote(nxt), 303)
        return Response("ログインが必要です", 401)
    is_admin_path = any(path == x or path.startswith(x if x.endswith("/") else x + "/") for x in ADMIN_ONLY)
    unlocked = auth.admin_unlocked(request.cookies.get(auth.ADMIN_COOKIE, ""), u["id"])
    if is_admin_path and path != "/admin/unlock" and not unlocked:
        if request.method == "GET":
            nxt = path + (("?" + request.url.query) if request.url.query else "")
            return RedirectResponse("/admin/unlock?next=" + quote(nxt), 303)
        return HTMLResponse("<p>管理者パスワードの入力が必要です。<a href='/admin/unlock'>入力する</a></p>", 403)
    request.state.user = u
    request.state.initial_pw = _is_initial(u)
    request.state.admin = unlocked
    request.state.admin_pw_initial = auth.admin_pw_is_initial() if unlocked else False
    resp = await call_next(request)
    if is_admin_path and unlocked and not getattr(request.state, "admin_lock", False):
        resp.set_cookie(auth.ADMIN_COOKIE, auth.make_admin_token(u["id"]), max_age=auth.ADMIN_IDLE_SECONDS, httponly=True, samesite="lax")
    if not getattr(request.state, "logout", False):
        u2 = auth.user(u["id"])                      # パスワード変更後は新しい内容でセッションを更新
        if u2: resp.set_cookie(auth.COOKIE, auth.make_token(u2), max_age=auth.IDLE_SECONDS, httponly=True, samesite="lax")
    return resp

def _safe_next(nxt):
    return nxt if nxt and nxt.startswith("/") and not nxt.startswith("//") else "/"

@app.get("/login", response_class=HTMLResponse)
def login_page(request: Request, next: str = "/", msg: str = ""):
    return page(request, "login.html", next=_safe_next(next), msg=msg, err="")

@app.post("/login", response_class=HTMLResponse)
def login_post(request: Request, login_id: str = Form(...), password: str = Form(...), next: str = Form("/")):
    login_id = login_id.strip()
    wait = auth.locked(login_id)
    if wait:
        return page(request, "login.html", next=_safe_next(next), err=f"ログインに続けて失敗したため、{wait // 60 + 1}分ほど待ってからやり直してください", login_id=login_id)
    u = auth.authenticate(login_id, password)
    if not u:
        return page(request, "login.html", next=_safe_next(next), err="IDまたはパスワードが違います", login_id=login_id)
    resp = RedirectResponse(_safe_next(next), 303)
    resp.set_cookie(auth.COOKIE, auth.make_token(u), max_age=auth.IDLE_SECONDS, httponly=True, samesite="lax")
    return resp

@app.post("/logout")
def logout(request: Request):
    request.state.logout = True
    resp = RedirectResponse("/login?msg=" + quote("ログアウトしました"), 303)
    resp.delete_cookie(auth.COOKIE); resp.delete_cookie(auth.ADMIN_COOKIE)
    return resp

@app.get("/account/password", response_class=HTMLResponse)
def password_page(request: Request, msg: str = ""):
    return page(request, "password.html", msg=msg, err="")

@app.post("/account/password", response_class=HTMLResponse)
def password_post(request: Request, current: str = Form(...), new1: str = Form(...), new2: str = Form(...)):
    u = request.state.user
    err = None
    if not auth.check_pw(current, u["pw_hash"], u["salt"]): err = "今のパスワードが違います"
    elif new1 != new2: err = "新しいパスワード（確認）が一致しません"
    else: err = auth.password_problem(new1, u["login_id"])
    if err: return page(request, "password.html", err=err, msg="")
    auth.set_password(u["id"], new1)
    return RedirectResponse("/account/password?msg=" + quote("パスワードを変更しました"), 303)

# ------------------------------------------------------------ 管理者パスワード（マスタ・設定に入る）
@app.get("/admin/unlock", response_class=HTMLResponse)
def unlock_page(request: Request, next: str = "/admin"):
    return page(request, "unlock.html", next=_safe_next(next), err="")

@app.post("/admin/unlock", response_class=HTMLResponse)
def unlock_post(request: Request, password: str = Form(...), next: str = Form("/admin")):
    u = request.state.user
    key = f"admin:{u['id']}"
    wait = auth.locked(key)
    if wait:
        return page(request, "unlock.html", next=_safe_next(next), err=f"続けて間違えたため、{wait // 60 + 1}分ほど待ってからやり直してください")
    ok = auth.check_admin_pw(password)
    with auth._fail_lock:
        if ok: auth._fail.pop(key, None)
        else:
            n, _ = auth._fail.get(key, (0, 0)); auth._fail[key] = (n + 1, __import__("time").time())
    if not ok:
        return page(request, "unlock.html", next=_safe_next(next), err="管理者パスワードが違います")
    nxt = _safe_next(next)
    resp = RedirectResponse(nxt if nxt.startswith("/admin") or nxt.startswith("/filter") else "/admin", 303)
    resp.set_cookie(auth.ADMIN_COOKIE, auth.make_admin_token(u["id"]), max_age=auth.ADMIN_IDLE_SECONDS, httponly=True, samesite="lax")
    return resp

@app.post("/admin/lock")
def admin_lock(request: Request):
    request.state.admin_lock = True
    resp = RedirectResponse("/", 303)
    resp.delete_cookie(auth.ADMIN_COOKIE)
    return resp

@app.post("/admin/password")
def admin_password(request: Request, current: str = Form(...), new1: str = Form(...), new2: str = Form(...)):
    if not auth.check_admin_pw(current): err = "今の管理者パスワードが違います"
    elif new1 != new2: err = "新しい管理者パスワード（確認）が一致しません"
    else: err = auth.password_problem(new1)
    if err: return RedirectResponse("/admin?tab=users&msg=" + quote(err), 303)
    auth.set_admin_pw(new1)
    resp = RedirectResponse("/admin?tab=users&msg=" + quote("管理者パスワードを変更しました"), 303)
    request.state.admin_lock = True                       # 新しいパスワードで解錠し直し
    resp.set_cookie(auth.ADMIN_COOKIE, auth.make_admin_token(request.state.user["id"]), max_age=auth.ADMIN_IDLE_SECONDS, httponly=True, samesite="lax")
    return resp

# ------------------------------------------------------------ ユーザー管理（マスタ・設定）
@app.get("/admin/users")
def users_page(msg: str = ""):
    return RedirectResponse("/admin?tab=users" + ("&msg=" + quote(msg) if msg else ""), 303)

@app.post("/admin/users/add")
def users_add(login_id: str = Form(...), name: str = Form(""), password: str = Form(...)):
    login_id = login_id.strip()
    if not login_id: return RedirectResponse("/admin?tab=users&msg=" + quote("IDを入力してください"), 303)
    if auth.user_by_login(login_id): return RedirectResponse("/admin?tab=users&msg=" + quote(f"ID {login_id} は登録済みです"), 303)
    pr = auth.password_problem(password, login_id)
    if pr: return RedirectResponse("/admin?tab=users&msg=" + quote(pr), 303)
    auth.add_user(login_id, name.strip(), password, False)
    return RedirectResponse("/admin?tab=users&msg=" + quote(f"ユーザー {login_id} を登録しました（初回ログイン後にパスワード変更を案内します）"), 303)

@app.post("/admin/users/{uid}/reset")
def users_reset(uid: int, password: str = Form(...)):
    u = auth.user(uid)
    if not u: raise HTTPException(404)
    pr = auth.password_problem(password, u["login_id"])
    if pr: return RedirectResponse("/admin?tab=users&msg=" + quote(pr), 303)
    auth.set_password(uid, password, must_change=True)
    return RedirectResponse("/admin?tab=users&msg=" + quote(f"{u['login_id']} のパスワードを再設定しました"), 303)

@app.post("/admin/users/{uid}/delete")
def users_delete(request: Request, uid: int):
    u = auth.user(uid)
    if not u: raise HTTPException(404)
    if u["id"] == request.state.user["id"]:
        return RedirectResponse("/admin?tab=users&msg=" + quote("自分自身は削除できません"), 303)
    if db.q("SELECT COUNT(*) n FROM users", one=True)["n"] <= 1:
        return RedirectResponse("/admin?tab=users&msg=" + quote("ユーザーが1人もいなくなるため削除できません"), 303)
    db.x("DELETE FROM users WHERE id=?", (uid,))
    return RedirectResponse("/admin?tab=users&msg=" + quote(f"ユーザー {u['login_id']} を削除しました"), 303)

def page(request, name, **ctx):
    return T.TemplateResponse(request, name, ctx)

def need_store(store_id):
    s = db.org(store_id)
    if not s or s["kind"] != "store": raise HTTPException(404, "店舗が見つかりません")
    return s

@app.get("/", response_class=HTMLResponse)
def home(request: Request, msg: str = "", q: str = ""):
    q = q.strip()
    hits = []
    if q:
        like = f"%{q}%"
        hits = db.q("SELECT s.*, d.name dname, d.code dcode, c.name cname FROM org s "
                    "LEFT JOIN org d ON s.parent_id=d.id LEFT JOIN org c ON d.parent_id=c.id "
                    "WHERE s.kind='store' AND (s.name LIKE ? OR s.code LIKE ?) "
                    "AND COALESCE(d.visible,1)=1 AND COALESCE(c.visible,1)=1 "
                    "AND (COALESCE(s.visible,1)=1 OR s.id IN (SELECT DISTINCT store_id FROM files)) "
                    "ORDER BY CAST(s.code AS INTEGER) LIMIT 200",
                    (like, like))
    cov = service.coverage_all()
    active = []
    if cov:
        ids = ",".join(str(i) for i in cov)
        active = db.q(f"SELECT s.*, d.name dname, c.name cname FROM org s LEFT JOIN org d ON s.parent_id=d.id "
                      f"LEFT JOIN org c ON d.parent_id=c.id WHERE s.id IN ({ids}) ORDER BY CAST(s.code AS INTEGER)")
    last = {r["store_id"]: r for r in db.q("SELECT j.* FROM jobs j JOIN (SELECT store_id, MAX(id) mid FROM jobs GROUP BY store_id) m ON j.id=m.mid")}
    hidden = db.q("SELECT COUNT(*) n FROM org WHERE kind IN ('company','division') AND COALESCE(visible,1)=0", one=True)["n"]
    hidden_st = db.q("SELECT COUNT(*) n FROM org WHERE kind='store' AND COALESCE(visible,1)=0", one=True)["n"]
    return page(request, "home.html", hits=hits, q=q, active=active, cov=cov, last=last,
                companies=db.orgs("company", visible_only=True), msg=msg, hidden=hidden, hidden_st=hidden_st,
                n_store=db.q("SELECT COUNT(*) n FROM org WHERE kind='store'", one=True)["n"])

@app.get("/admin", response_class=HTMLResponse)
def admin_page(request: Request, msg: str = "", tab: str = "org"):
    tab = tab if tab in ("org", "filter", "import", "users") else "org"
    ctx = dict(tab=tab, msg=msg)
    counts = {r["parent_id"]: r["n"] for r in db.q("SELECT parent_id, COUNT(*) n FROM org GROUP BY parent_id")}
    ctx["counts"] = counts
    if tab == "org":
        ctx["tree"] = [dict(c=c, divs=db.orgs("division", c["id"], visible_only=True)) for c in db.orgs("company", visible_only=True)]
        ctx["companies"] = db.orgs("company", visible_only=True); ctx["divisions"] = db.orgs("division")
    elif tab == "filter":
        ctx["tree"] = [dict(c=c, divs=db.orgs("division", c["id"])) for c in db.orgs("company")]
        ctx["has"] = {r["parent_id"] for r in db.q("SELECT DISTINCT o.parent_id FROM files f JOIN org o ON f.store_id=o.id")}
        sby = {}
        for r in db.orgs("store"): sby.setdefault(r["parent_id"], []).append(r)
        ctx["sby"] = sby
        ctx["data_st"] = {r["store_id"] for r in db.q("SELECT DISTINCT store_id FROM files")}
    elif tab == "import":
        # 労働時間予算：取込状況（事業部ごと）と、取込先の初期値（直近に取り込んだ事業部→データのある店舗の事業部）
        lab_div = {r["did"]: r for r in db.q(
            "SELECT o.parent_id did, COUNT(DISTINCT l.store_id) n, MIN(l.ym) a, MAX(l.ym) b, MAX(l.ki) ki, MAX(l.imported_at) t "
            "FROM labor_budget l JOIN org o ON l.store_id=o.id GROUP BY o.parent_id")}
        act = db.q("SELECT o.parent_id FROM files f JOIN org o ON f.store_id=o.id LIMIT 1", one=True)
        ctx.update(lab_div=lab_div,
                   lab_default=(max(lab_div.values(), key=lambda r: r["t"])["did"] if lab_div else (act["parent_id"] if act else None)),
                   lab_divs=[x for x in db.orgs("division", visible_only=True) if counts.get(x["id"])],
                   default_ym=_fy_start(), companies=db.orgs("company", visible_only=True), divisions=db.orgs("division"))
    elif tab == "users":
        ctx["users"] = auth.users()
        ctx["admin_pw_changed"] = auth.admin_pw_changed_at()
    return page(request, "admin.html", **ctx)

@app.get("/api/orgs")
def api_orgs(kind: str, parent: int | None = None):
    """会社・事業部・店舗の選択肢（プルダウン用）"""
    if kind not in ("company", "division", "store"): raise HTTPException(400)
    rows = db.orgs(kind, parent, visible_only=kind != "store") if kind != "company" else db.orgs("company", visible_only=True)
    has = set(service.coverage_all()) if kind == "store" else set()
    if kind == "store": rows = [r for r in rows if r["visible"] != 0 or r["id"] in has]
    return JSONResponse([dict(id=r["id"], code=r["code"] or "", name=r["name"], data=r["id"] in has) for r in rows])

@app.get("/division/{did}", response_class=HTMLResponse)
def division_page(request: Request, did: int, msg: str = "", all: int = 0):
    d = db.org(did)
    if not d or d["kind"] != "division": raise HTTPException(404)
    c = db.org(d["parent_id"]) if d["parent_id"] else None
    stores_all = db.orgs("store", did)
    cov = service.coverage_all({s["id"] for s in stores_all})
    shown = [x for x in stores_all if x["visible"] != 0 or x["id"] in cov]
    stores = stores_all if all else shown
    n_hidden = len(stores_all) - len(shown)
    last = {r["store_id"]: r for r in db.q("SELECT j.* FROM jobs j JOIN (SELECT store_id, MAX(id) mid FROM jobs GROUP BY store_id) m ON j.id=m.mid")}
    sids = {x["id"] for x in stores}
    lab = {r["store_id"]: r for r in db.q("SELECT store_id, MIN(ym) a, MAX(ym) b, MAX(ki) ki, MAX(imported_at) t "
                                          "FROM labor_budget GROUP BY store_id") if r["store_id"] in sids}
    return page(request, "division.html", d=d, c=c, stores=stores, cov=cov, last=last, msg=msg, lab=lab,
                n_hidden=n_hidden, show_all=bool(all))

def _fy_start():
    import datetime as _dt
    t = _dt.date.today(); fy = t.year if t.month >= 3 else t.year - 1
    return f"{fy}-03"

@app.post("/master/labor")
async def master_labor(file: UploadFile = File(...), start_ym: str = Form(...), division_id: int = Form(...)):
    d = db.org(division_id)
    if not d or d["kind"] != "division":
        return RedirectResponse("/admin?tab=import&msg=" + quote("労働時間予算を取り込む事業部を選んでください"), 303)
    try:
        r = service.import_labor_budget(division_id, file.filename, await file.read(), start_ym)
    except Exception as e:
        return RedirectResponse("/admin?tab=import&msg=" + quote(f"労働時間予算を取り込めません：{e}"), 303)
    msg = (f"労働時間予算（{r['ki'] or ''}期）を {d['name']} に取り込みました：{r['stores']}店舗・{r['months']}"
           + (f"\n店番が見つからず取り込めなかった店：{'、'.join(map(str, r['unmatched']))}" if r["unmatched"] else ""))
    return RedirectResponse("/admin?tab=import&msg=" + quote(msg), 303)

@app.post("/master/import")
async def master_import(files: list[UploadFile] = File(...)):
    contents = [(uf.filename, await uf.read()) for uf in files if uf.filename]
    try:
        st = master.import_master(contents)
    except master.MasterError as e:
        return RedirectResponse("/admin?tab=import&msg=" + quote(f"マスタを取り込めません：{e}"), 303)
    label = {"company": "会社", "division": "事業部", "store": "店舗"}
    msg = "マスタを取り込みました。\n" + "\n".join(f"{label[k]}：追加{v['追加']}件・更新{v['更新']}件" for k, v in st.items())
    return RedirectResponse("/admin?tab=import&msg=" + quote(msg), 303)

@app.post("/org/add")
def org_add(kind: str = Form(...), name: str = Form(...), code: str = Form(""), parent_id: str = Form("")):
    name = name.strip()
    if kind not in ("company", "division", "store") or not name: raise HTTPException(400, "入力が不正です")
    pid = int(parent_id) if parent_id else None
    if kind != "company":
        p = db.org(pid) if pid else None
        want = "company" if kind == "division" else "division"
        if not p or p["kind"] != want: raise HTTPException(400, "上位の組織を選んでください")
    oid = db.add_org(kind, name, code.strip(), pid)
    if kind == "store": return RedirectResponse(f"/store/{oid}", 303)
    return RedirectResponse("/admin?tab=org&msg=" + quote(f"「{name}」を登録しました"), 303)

@app.post("/org/{oid}/rename")
def org_rename(oid: int, name: str = Form(...), code: str = Form("")):
    if not db.org(oid): raise HTTPException(404)
    db.x("UPDATE org SET name=?, code=? WHERE id=?", (name.strip(), code.strip(), oid))
    return RedirectResponse("/admin?tab=org&msg=" + quote("名称を変更しました"), 303)

@app.post("/org/{oid}/delete")
def org_delete(oid: int):
    o = db.org(oid)
    if not o: raise HTTPException(404)
    if db.q("SELECT 1 FROM org WHERE parent_id=?", (oid,)):
        return RedirectResponse("/admin?tab=org&msg=" + quote(f"「{o['name']}」の下に組織があるため削除できません"), 303)
    if db.q("SELECT 1 FROM files WHERE store_id=?", (oid,)):
        return RedirectResponse("/admin?tab=org&msg=" + quote(f"「{o['name']}」に取込ファイルがあるため削除できません（先にファイルを削除）"), 303)
    db.x("DELETE FROM jobs WHERE store_id=?", (oid,))
    db.x("DELETE FROM org WHERE id=?", (oid,))
    return RedirectResponse("/admin?tab=org&msg=" + quote(f"「{o['name']}」を削除しました"), 303)

# ------------------------------------------------------------ 表示する会社・事業部
HIDE_RE = re.compile(r"経費振替|本部店|(?<!ホーム)センター")   # ホームセンターは外さない

@app.get("/filter")
def filter_page(msg: str = ""):
    return RedirectResponse("/admin?tab=filter" + ("&msg=" + quote(msg) if msg else ""), 303)

@app.post("/filter")
async def filter_save(request: Request):
    form = await request.form(max_fields=20000)   # 店舗のチェックが約2千件あるため
    preset = form.get("preset")
    ids_on = {int(v) for v in form.getlist("show")}
    with db._lock, db.conn() as c:
        for r in c.execute("SELECT id, kind, name, parent_id, visible FROM org").fetchall():
            if preset == "all": v = 1
            elif preset == "main":
                if r["kind"] == "store":
                    v = r["visible"] if r["visible"] is not None else 1     # 店舗の設定はそのまま
                else:
                    n = c.execute("SELECT COUNT(*) FROM org WHERE parent_id=?", (r["id"],)).fetchone()[0]
                    v = 0 if (r["kind"] == "division" and (n == 0 or HIDE_RE.search(r["name"]))) else 1
            else: v = 1 if r["id"] in ids_on else 0
            c.execute("UPDATE org SET visible=? WHERE id=?", (v, r["id"]))
        c.commit()
    return RedirectResponse("/admin?tab=filter&msg=" + quote("表示する会社・事業部・店舗を保存しました"), 303)

# ------------------------------------------------------------ 店舗
@app.get("/store/{store_id}", response_class=HTMLResponse)
def store_page(request: Request, store_id: int, msg: str = ""):
    s = need_store(store_id)
    c, d, _ = db.path_of(store_id)
    fl = []
    for f in service.files(store_id):
        info = json.loads(f["info"] or "{}")
        fl.append(dict(f=f, kind=KINDS.get(f["kind"], f["kind"]), info=info))
    days = service.plan_days(store_id)
    jobs = db.q("SELECT * FROM jobs WHERE store_id=? ORDER BY id DESC LIMIT 20", (store_id,))
    info = json.loads(s["info"] or "{}") if "info" in s.keys() and s["info"] else {}
    lb = service.labor_budget(store_id)
    ph = service.plan_hours_by_month(store_id) if lb else {}
    from engine.common import getsudo_label, merged_settings as _ms
    _sd = int(_ms(db.settings(store_id))["month_start_day"])
    labor_rows = [dict(v, plan=ph.get(ym), label=getsudo_label(ym, _sd)) for ym, v in lb.items()]
    return page(request, "store.html", s=s, c=c, d=d, files=fl, cov=service.coverage(store_id), labor_rows=labor_rows,
                days=days, jobs=jobs, msg=msg, dup=len(service.duplicate_files(store_id)), sinfo=info)

@app.post("/store/{store_id}/upload", response_class=HTMLResponse)
async def upload(request: Request, store_id: int, files: list[UploadFile] = File(...)):
    need_store(store_id)
    results = []
    tdir = os.path.join(db.DATA_DIR, "tmp"); os.makedirs(tdir, exist_ok=True)
    for uf in files:
        if not uf.filename: continue
        tmp = os.path.join(tdir, f"up_{secrets.token_hex(8)}")
        size, too_big = 0, False
        with open(tmp, "wb") as f:                     # メモリに溜めず少しずつ書き出す
            while True:
                chunk = await uf.read(4 * 1024 * 1024)
                if not chunk: break
                size += len(chunk)
                if size > MAX_UPLOAD: too_big = True; break
                f.write(chunk)
        if too_big:
            os.remove(tmp)
            results.append(dict(filename=uf.filename, ok=False, message=f"ファイルが大きすぎます（{MAX_UPLOAD // 1024 // 1024}MBまで）")); continue
        results.append(service.import_path(store_id, uf.filename, tmp))
    ok = sum(r["ok"] for r in results)
    lines = [f"{r['filename']}：" + (f"{r['kind']}（{r['period']}）" + (f"　※{r['warning']}" if r.get("warning") else "")
                                     if r["ok"] else (r["message"] if r.get("skipped") else f"取込できません － {r['message']}"))
             for r in results]
    msg = f"{ok}件取り込みました。\n" + "\n".join(lines)
    return RedirectResponse(f"/store/{store_id}?msg=" + quote(msg), 303)

@app.post("/store/{store_id}/dedupe")
def dedupe(store_id: int):
    need_store(store_id)
    n = service.remove_duplicates(store_id)
    return RedirectResponse(f"/store/{store_id}?msg=" + quote(f"同じ内容のファイル{n}件を削除しました"), 303)

@app.post("/file/{file_id}/delete")
def file_delete(file_id: int):
    sid = service.delete_file(file_id)
    if sid is None: raise HTTPException(404)
    return RedirectResponse(f"/store/{sid}?msg=" + quote("ファイルを削除しました"), 303)

@app.post("/store/{store_id}/generate")
def generate(store_id: int, date_from: str = Form(...), date_to: str = Form("")):
    need_store(store_id)
    date_to = date_to or date_from
    if date_to < date_from: date_from, date_to = date_to, date_from
    import pandas as pd
    if (pd.Timestamp(date_to) - pd.Timestamp(date_from)).days > 62:
        return RedirectResponse(f"/store/{store_id}?msg=" + quote("一度に作成できるのは63日までです"), 303)
    jid = service.start_job(store_id, date_from, date_to)
    return RedirectResponse(f"/job/{jid}", 303)

@app.get("/store/{store_id}/settings", response_class=HTMLResponse)
def settings_page(request: Request, store_id: int, msg: str = ""):
    s = need_store(store_id)
    c, d, _ = db.path_of(store_id)
    cur = merged_settings(db.settings(store_id))
    show = {k: (", ".join(map(str, v)) if isinstance(v, list) else v) for k, v in cur.items()}
    return page(request, "settings.html", s=s, c=c, d=d, st=show, msg=msg)

LIST_KEYS = {"start_items", "end_items", "exclude_items", "spike_keywords", "leave_kinds"}
@app.post("/store/{store_id}/settings")
async def settings_save(request: Request, store_id: int):
    need_store(store_id)
    form = await request.form()
    out = {}
    for k in DEFAULT_SETTINGS:
        v = str(form.get(k, "")).strip()
        if v == "": continue
        if k in LIST_KEYS: out[k] = [x.strip() for x in v.replace("、", ",").split(",") if x.strip()]
        elif k in ("staffed_registers", "dedicated_registers"):
            out[k] = "auto" if v.lower() == "auto" else [int(x) for x in v.replace("、", ",").split(",") if x.strip().isdigit()]
        elif k in ("cap", "leave_hours"): out[k] = float(v)
        elif k in ("min_lanes", "tr_keep", "history_days", "ref_weeks", "month_start_day"): out[k] = int(float(v))
        else: out[k] = v
    if form.get("reset"): out = {}
    db.save_settings(store_id, out)
    return RedirectResponse(f"/store/{store_id}/settings?msg=" + quote("設定を保存しました"), 303)

# ------------------------------------------------------------ 月度計画と労働時間予算の比較
def _periods(store_id):
    rows = db.q("SELECT DISTINCT period_from a, period_to b FROM files WHERE store_id=? AND kind='plan' ORDER BY a DESC", (store_id,))
    return [(r["a"], r["b"]) for r in rows if r["a"] and r["b"]]

@app.get("/store/{store_id}/compare", response_class=HTMLResponse)
def compare_page(request: Request, store_id: int, date_from: str = "", date_to: str = ""):
    s = need_store(store_id)
    c, d, _ = db.path_of(store_id)
    periods = _periods(store_id)
    if not date_from and periods: date_from, date_to = periods[0]
    res, err = None, None
    labor = service.labor_budget(store_id)
    if date_from and date_to:
        if not labor: err = "労働時間予算が取り込まれていません（「マスタ・設定」の「マスタ取込」から取り込んでください）"
        else:
            try:
                from engine.compare import compare_period
                res = compare_period(service.store_data(store_id), labor, db.settings(store_id), date_from, date_to)
            except Exception as e:
                err = str(e)
    return page(request, "compare.html", s=s, c=c, d=d, periods=periods, date_from=date_from, date_to=date_to,
                res=res, err=err, settings=merged_settings(db.settings(store_id)))

@app.get("/store/{store_id}/compare.xlsx")
def compare_xlsx(store_id: int, date_from: str, date_to: str):
    s = need_store(store_id)
    from engine.compare import compare_period, write_excel
    res = compare_period(service.store_data(store_id), service.labor_budget(store_id), db.settings(store_id), date_from, date_to)
    out = os.path.join(service.store_dir(store_id, "compare"), f"労働時間比較_{service.safe_name(s['name'])}_{date_from}_{date_to}.xlsx")
    write_excel(res, out, s["name"])
    return FileResponse(out, filename=os.path.basename(out))

# ------------------------------------------------------------ ジョブ
@app.get("/job/{jid}", response_class=HTMLResponse)
def job_page(request: Request, jid: int):
    j = db.q("SELECT * FROM jobs WHERE id=?", (jid,), one=True)
    if not j: raise HTTPException(404)
    c, d, s = db.path_of(j["store_id"])
    res = json.loads(j["result"] or "[]")
    zipname = None
    od = db.resolve(j["outdir"])
    if od and os.path.isdir(od):
        z = [f for f in os.listdir(od) if f.endswith(".zip")]
        zipname = z[0] if z else None
    return page(request, "job.html", j=j, c=c, d=d, s=s, res=res, zipname=zipname)

@app.get("/job/{jid}/file/{name}")
def job_file(jid: int, name: str):
    j = db.q("SELECT * FROM jobs WHERE id=?", (jid,), one=True)
    if not j or not j["outdir"]: raise HTTPException(404)
    p = os.path.join(db.resolve(j["outdir"]), os.path.basename(name))
    if not os.path.isfile(p): raise HTTPException(404)
    return FileResponse(p, filename=os.path.basename(p))
