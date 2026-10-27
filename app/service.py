"""ファイル取込・店舗データの組み立て・作成ジョブの実行"""
import os, re, json, uuid, zipfile, traceback, threading, hashlib
from concurrent.futures import ThreadPoolExecutor
import pandas as pd
from . import db
from engine.loaders import load, StoreData, KINDS, LoadError
from engine.planner import DayPlanner, PlanError
from engine.excel import write as write_excel

_pool = ThreadPoolExecutor(max_workers=int(os.environ.get("KADOU_WORKERS", "2")))
_cache = {}            # store_id -> (signature, StoreData)
_cache_lock = threading.Lock()

def store_dir(store_id, *sub):
    p = os.path.join(db.DATA_DIR, "stores", str(store_id), *sub)
    os.makedirs(p, exist_ok=True)
    return p

def safe_name(name):
    name = os.path.basename(name or "file")
    return re.sub(r'[\\/:*?"<>|]', "_", name)

# ---------------------------------------------------------------- 取込
def _norm(s):
    return re.sub(r"[\s　]", "", str(s or "")).translate(str.maketrans("０１２３４５６７８９ＡＢＣＤＥＦＧＨＩＪＫＬＭＮＯＰＱＲＳＴＵＶＷＸＹＺ",
                                                                   "0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZ"))

def _hash_file(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for b in iter(lambda: f.read(1 << 20), b""): h.update(b)
    return h.hexdigest()

def import_file(store_id, filename, content):
    """バイト列から取り込む（小さいファイル用）"""
    tdir = os.path.join(db.DATA_DIR, "tmp"); os.makedirs(tdir, exist_ok=True)
    tmp = os.path.join(tdir, uuid.uuid4().hex)
    with open(tmp, "wb") as f: f.write(content)
    return import_path(store_id, filename, tmp)

def import_path(store_id, filename, tmp):
    """一時ファイルから取り込む（アップロードはディスクへ書き出してから渡す）"""
    store = db.org(store_id)
    fn = safe_name(filename)
    digest = _hash_file(tmp)
    fill_hashes(store_id)
    if db.q("SELECT 1 FROM files WHERE store_id=? AND hash=?", (store_id, digest), one=True):
        os.remove(tmp)
        return dict(filename=fn, ok=False, skipped=True, message="同じ内容のファイルが取込済みのため飛ばしました")
    raw = os.path.join(store_dir(store_id, "raw"), f"{uuid.uuid4().hex[:8]}_{fn}")
    os.replace(tmp, raw)
    try:
        kind, df, info = load(raw)
    except LoadError as e:
        os.remove(raw); return dict(filename=fn, ok=False, message=str(e))
    except Exception as e:
        os.remove(raw); return dict(filename=fn, ok=False, message=f"読み込みに失敗しました（{e}）")
    if kind == "labor_budget":
        os.remove(raw)
        return dict(filename=fn, ok=False, message="労働時間予算は複数店舗分のファイルのため、「マスタ・設定」の「マスタ取込」から取り込んでください")
    cache = os.path.join(store_dir(store_id, "cache"), os.path.basename(raw) + ".pkl")
    df.to_pickle(cache)
    warn = None
    # 店舗名の食い違いチェック（月間稼働表・日割予算に店舗名が書かれている）
    st = info.get("store")
    if st and store and _norm(store["name"]) not in _norm(st) and _norm(st) not in _norm(store["name"]) \
            and not (store["code"] and _norm(store["code"]) in _norm(st)):
        warn = f"ファイルの店舗「{st}」と登録店舗「{store['name']}」が一致しません"
    pf, pt = info.get("period_from"), info.get("period_to")
    fmt = lambda d: pd.Timestamp(d).strftime("%Y-%m-%d") if d is not None and not pd.isna(d) else None
    info_s = {k: (fmt(v) if isinstance(v, pd.Timestamp) else v) for k, v in info.items() if k not in ("period_from", "period_to")}
    db.x("INSERT INTO files(store_id,kind,filename,path,cache,period_from,period_to,info,warning,uploaded_at,hash) "
         "VALUES(?,?,?,?,?,?,?,?,?,?,?)",
         (store_id, kind, fn, raw, cache, fmt(pf), fmt(pt), json.dumps(info_s, ensure_ascii=False, default=str), warn,
          db.now(), digest))
    with _cache_lock: _cache.pop(store_id, None)
    return dict(filename=fn, ok=True, kind=KINDS[kind], period=f"{fmt(pf)}〜{fmt(pt)}", warning=warn)

def delete_file(file_id):
    f = db.q("SELECT * FROM files WHERE id=?", (file_id,), one=True)
    if not f: return None
    for p in (db.resolve(f["path"]), db.resolve(f["cache"])):
        try: os.remove(p)
        except OSError: pass
    db.x("DELETE FROM files WHERE id=?", (file_id,))
    with _cache_lock: _cache.pop(f["store_id"], None)
    return f["store_id"]

def fill_hashes(store_id):
    """以前に取り込んだファイルのハッシュを補う"""
    for r in db.q("SELECT id, path FROM files WHERE store_id=? AND hash IS NULL", (store_id,)):
        rp = db.resolve(r["path"])
        if rp and os.path.isfile(rp): db.x("UPDATE files SET hash=? WHERE id=?", (_hash_file(rp), r["id"]))

def duplicate_files(store_id):
    """同じ内容のファイルのうち、最初に取り込んだもの以外"""
    fill_hashes(store_id)
    seen, dup = set(), []
    for r in db.q("SELECT id, hash FROM files WHERE store_id=? ORDER BY id", (store_id,)):
        if r["hash"] and r["hash"] in seen: dup.append(r["id"])
        elif r["hash"]: seen.add(r["hash"])
    return dup

def remove_duplicates(store_id):
    dup = duplicate_files(store_id)
    for i in dup: delete_file(i)
    return len(dup)

def coverage_all(store_ids=None):
    """店舗ごと・種類ごとの取込済み期間（一覧画面用に1回で集計）"""
    rows = db.q("SELECT store_id, kind, MIN(period_from) a, MAX(period_to) b, COUNT(*) n FROM files GROUP BY store_id, kind")
    out = {}
    for r in rows:
        if store_ids is None or r["store_id"] in store_ids:
            out.setdefault(r["store_id"], {})[r["kind"]] = dict(start=r["a"], end=r["b"], n=r["n"])
    return out

def files(store_id):
    return db.q("SELECT * FROM files WHERE store_id=? ORDER BY kind, period_from, id", (store_id,))

def coverage(store_id):
    """種類ごとの取込済み期間と、作成できる日付の範囲"""
    out = {}
    for k, label in KINDS.items():
        rows = db.q("SELECT MIN(period_from) a, MAX(period_to) b, COUNT(*) n FROM files WHERE store_id=? AND kind=?",
                    (store_id, k), one=True)
        out[k] = dict(label=label, n=rows["n"], start=rows["a"], end=rows["b"])
    return out

def store_data(store_id):
    sig = tuple((r["id"], r["uploaded_at"]) for r in db.q("SELECT id, uploaded_at FROM files WHERE store_id=? ORDER BY id", (store_id,)))
    with _cache_lock:
        c = _cache.get(store_id)
        if c and c[0] == sig: return c[1]
    frames = []
    for r in db.q("SELECT kind, cache FROM files WHERE store_id=? ORDER BY uploaded_at, id", (store_id,)):
        frames.append((r["kind"], pd.read_pickle(db.resolve(r["cache"]))))
    data = StoreData(frames)
    with _cache_lock: _cache[store_id] = (sig, data)
    return data

def plan_days(store_id):
    d = store_data(store_id)
    if d.plan is None: return []
    return sorted(d.plan["日付"].dt.strftime("%Y-%m-%d").unique())

# ---------------------------------------------------------------- 労働時間予算
def import_labor_budget(division_id, filename, content, start_ym):
    """事業部単位の労働時間予算（店番×月）を取り込む。店舗コード＝事業部コード×1000＋店番"""
    from engine.loaders import parse_labor_budget
    div = db.org(division_id)
    tdir = os.path.join(db.DATA_DIR, "tmp"); os.makedirs(tdir, exist_ok=True)
    tmp = os.path.join(tdir, f"{uuid.uuid4().hex[:8]}_{safe_name(filename)}")
    with open(tmp, "wb") as f: f.write(content)
    try:
        df, info = parse_labor_budget(tmp)
    finally:
        try: os.remove(tmp)
        except OSError: pass
    y0, m0 = map(int, start_ym.split("-")[:2])
    yms = [f"{y0 + (m0 - 1 + i) // 12}-{(m0 - 1 + i) % 12 + 1:02d}" for i in range(12)]
    stores = {r["code"]: r for r in db.orgs("store", division_id)}
    dcode = int(div["code"]) if str(div["code"]).isdigit() else None
    matched, unmatched = set(), []
    with db._lock, db.conn() as c:
        for ban, g in df.groupby("店番"):
            code = str(dcode * 1000 + int(ban)) if dcode is not None else None
            st = stores.get(code) or stores.get(str(int(ban)))
            if not st: unmatched.append(int(ban)); continue
            matched.add(st["id"])
            for _, r in g.iterrows():
                c.execute("INSERT OR REPLACE INTO labor_budget VALUES(?,?,?,?,?,?,?,?,?)",
                          (st["id"], yms[int(r["月"])], float(r["正社員"]), float(r["パート"]), float(r["アルバイト"]),
                           float(r["合計"]), info.get("ki"), safe_name(filename), db.now()))
        c.commit()
    return dict(stores=len(matched), months=f"{yms[0]}〜{yms[-1]}", unmatched=unmatched, ki=info.get("ki"))

def labor_budget(store_id):
    return {r["ym"]: dict(r) for r in db.q("SELECT * FROM labor_budget WHERE store_id=? ORDER BY ym", (store_id,))}

def _rule_break(sh):
    return 0 if sh <= 330 else 60 if sh <= 540 else 90 if sh <= 570 else 120

def plan_hours_by_month(store_id):
    """月度ごとの計画実働（拘束−休憩。休憩はKADOU実績から推定）と実績（KADOU）、対象日数"""
    from engine.common import getsudo, getsudo_range, merged_settings
    from engine.compare import BreakModel
    d = store_data(store_id)
    sd = int(merged_settings(db.settings(store_id))["month_start_day"])
    out = {}
    def slot(ym):
        if ym not in out:
            g0, g1 = getsudo_range(ym, sd)
            out[ym] = dict(hours=0.0, days=0, actual=None, actual_days=0, month_days=(g1 - g0).days + 1,
                           start=f"{g0:%m/%d}", end=f"{g1:%m/%d}")
        return out[ym]
    if d.plan is not None:
        brk = BreakModel(d.plan, d.kadou)
        p = d.plan[d.plan["区分"].isin(["出勤", "半休"]) & d.plan["開始"].notna()].copy()
        if len(p):
            sh = [(e.hour * 60 + e.minute) - (s_.hour * 60 + s_.minute) for s_, e in zip(p["開始"], p["終了"])]
            p["実働"] = [(x - brk(n, x)) / 60 for n, x in zip(p["社員名"], sh)]
            p["ym"] = [getsudo(x, sd) for x in p["日付"]]
            for ym, g in p.groupby("ym"): slot(ym)["hours"] = float(g["実働"].sum())
        for ym, g in d.plan.groupby([getsudo(x, sd) for x in d.plan["日付"]]): slot(ym)["days"] = int(g["日付"].nunique())
    if d.kadou is not None:
        k = d.kadou[d.kadou["ymd"] < pd.Timestamp.today().normalize()]     # 今日以降は予定なので実績に含めない
        k = k.assign(ym=[getsudo(x, sd) for x in k["ymd"]])
        for ym, g in k.groupby("ym"):
            x = slot(ym); x["actual"] = float(g["s_time"].sum()) / 60; x["actual_days"] = int(g["ymd"].nunique())
    return out

# ---------------------------------------------------------------- 作成ジョブ
def start_job(store_id, date_from, date_to):
    jid = db.x("INSERT INTO jobs(store_id,date_from,date_to,status,progress,created_at) VALUES(?,?,?,?,?,?)",
               (store_id, date_from, date_to, "待機", "", db.now()))
    _pool.submit(_run_job, jid)
    return jid

def _run_job(jid):
    job = db.q("SELECT * FROM jobs WHERE id=?", (jid,), one=True)
    store_id = job["store_id"]
    _, _, store = db.path_of(store_id)
    outdir = store_dir(store_id, "out", str(jid))
    db.x("UPDATE jobs SET status='実行中', outdir=? WHERE id=?", (outdir, jid))
    days = list(pd.date_range(job["date_from"], job["date_to"]))
    results = []
    try:
        data = store_data(store_id)
        settings = db.settings(store_id)
        labor = labor_budget(store_id)
        for i, d in enumerate(days, 1):
            db.x("UPDATE jobs SET progress=? WHERE id=?", (f"{i}/{len(days)}日（{d:%m/%d}）", jid))
            try:
                pl = DayPlanner(data, settings, d, store["name"], labor=labor).run()
                fn = f"日別稼働表_{safe_name(store['name'])}_{d:%Y-%m-%d}.xlsx"
                write_excel(pl, os.path.join(outdir, fn))
                results.append(dict(ok=True, file=fn, **pl.summary_dict()))
            except PlanError as e:
                results.append(dict(ok=False, day=f"{d:%Y-%m-%d}", error=str(e)))
            except Exception as e:
                results.append(dict(ok=False, day=f"{d:%Y-%m-%d}", error=f"作成中にエラー：{e}",
                                    trace=traceback.format_exc()[-1500:]))
        okf = [r["file"] for r in results if r.get("ok")]
        if len(okf) > 1:
            zp = os.path.join(outdir, f"日別稼働表_{safe_name(store['name'])}_{job['date_from']}_{job['date_to']}.zip")
            with zipfile.ZipFile(zp, "w", zipfile.ZIP_DEFLATED) as z:
                for f in okf: z.write(os.path.join(outdir, f), f)
        status = "完了" if all(r.get("ok") for r in results) else ("一部エラー" if okf else "エラー")
        db.x("UPDATE jobs SET status=?, progress='', result=?, finished_at=? WHERE id=?",
             (status, json.dumps(results, ensure_ascii=False, default=str), db.now(), jid))
    except Exception as e:
        db.x("UPDATE jobs SET status='エラー', error=?, result=?, finished_at=? WHERE id=?",
             (f"{e}", json.dumps(results, ensure_ascii=False, default=str), db.now(), jid))
