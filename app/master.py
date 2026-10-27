"""会社・事業部・店舗マスタ（snowhc 抽出：会社事業部.xlsx／会社事業部店舗.xlsx）の取込"""
import re, json, io
import pandas as pd
import openpyxl
from . import db

class MasterError(Exception):
    pass

def _read(content):
    """1行目がタイトル（ODBC/...）、2行目が見出しの抽出Excel"""
    wb = openpyxl.load_workbook(io.BytesIO(content), read_only=True)
    rows = list(wb.worksheets[0].iter_rows(values_only=True))
    hi = next((i for i, r in enumerate(rows[:5]) if r and "KAISHA_CD" in [str(x) for x in r]), None)
    if hi is None: raise MasterError("KAISHA_CD 列が見つかりません")
    df = pd.DataFrame(rows[hi + 1:], columns=[str(x) for x in rows[hi]]).dropna(how="all")
    return df

def kind_of(df):
    if "TENPO_CD" in df.columns: return "store"
    if "JIGYOBU_CD" in df.columns and any(c in df.columns for c in ("JIGYOUBU_NM", "JIGYOBU_NM")): return "division"
    raise MasterError("会社事業部／会社事業部店舗の形式ではありません")

def _strip(s):
    return re.sub(r"[（(].*?[）)]", "", str(s)).strip()

def company_name_guess(div_names):
    """会社名がマスタに無いため、配下の事業部名の共通部分から仮の名前を作る"""
    names = [_strip(n) for n in div_names if n and not re.search(r"経費振替|本部店", str(n))]
    names = [n for n in names if n]
    if not names: return None
    pre = names[0]
    for n in names[1:]:
        k = 0
        while k < min(len(pre), len(n)) and pre[k] == n[k]: k += 1
        pre = pre[:k]
    return pre if len(pre) >= 2 else names[0]

def _norm_code(v):
    try: return str(int(float(v)))
    except (TypeError, ValueError): return str(v).strip()

class _Org:
    """既存の組織をメモリに読み込み、1トランザクションで登録・更新する"""
    def __init__(self, c):
        self.c = c
        rows = [dict(r) for r in c.execute("SELECT * FROM org")]
        self.by_id = {r["id"]: r for r in rows}
        self.by_code = {}
        for r in rows:
            self.by_code.setdefault((r["kind"], r["code"]), r)
        self.manual = [r for r in rows if r.get("source") != "master" and (r["code"] or "").strip().isdigit()]
        self.stats = {k: {"追加": 0, "更新": 0} for k in ("company", "division", "store")}

    def match(self, kind, code, parent_code=None):
        o = self.by_code.get((kind, code))
        if o: return o
        # 手入力の登録をマスタのコードに読み替える：00→0、店舗の001→事業部コード×1000+1
        for r in self.manual:
            if r["kind"] != kind: continue
            cv = int(r["code"].strip())
            if kind in ("company", "division") and cv == int(code): return r
            if kind == "store" and parent_code is not None and cv < 1000 and int(parent_code) * 1000 + cv == int(code):
                p = self.by_id.get(r["parent_id"])
                if p and _norm_code(p["code"]) == str(int(parent_code)): return r
        return None

    def upsert(self, kind, code, name, parent_id, info=None, keep_name=False, parent_code=None):
        o = self.match(kind, code, parent_code)
        info_s = json.dumps(info, ensure_ascii=False, default=str, sort_keys=True) if info else None
        if o:
            new_name = o["name"] if keep_name and o["name"] else name
            new_info = info_s if info_s is not None else o.get("info")
            if (o["code"], o["name"], o["parent_id"], o.get("source"), o.get("info")) != (code, new_name, parent_id, "master", new_info):
                self.c.execute("UPDATE org SET code=?, name=?, parent_id=?, source='master', info=? WHERE id=?",
                               (code, new_name, parent_id, new_info, o["id"]))
                o.update(code=code, name=new_name, parent_id=parent_id, source="master", info=new_info)
                self.by_code[(kind, code)] = o
                if o in self.manual: self.manual.remove(o)
                self.stats[kind]["更新"] += 1
            return o["id"]
        cur = self.c.execute("INSERT INTO org(kind,code,name,parent_id,created_at,source,info) VALUES(?,?,?,?,?,?,?)",
                             (kind, code, name, parent_id, db.now(), "master", info_s))
        r = dict(id=cur.lastrowid, kind=kind, code=code, name=name, parent_id=parent_id, source="master", info=info_s)
        self.by_id[r["id"]] = r; self.by_code[(kind, code)] = r
        self.stats[kind]["追加"] += 1
        return r["id"]

def import_master(contents):
    """contents: [(filename, bytes)]。会社事業部・会社事業部店舗のどちらか片方でも可"""
    divs, stores = None, None
    for fn, b in contents:
        df = _read(b)
        if kind_of(df) == "store": stores = df
        else: divs = df
    if divs is None and stores is None: raise MasterError("マスタのファイルがありません")
    div_names = {}
    if divs is not None:
        nm = "JIGYOUBU_NM" if "JIGYOUBU_NM" in divs.columns else "JIGYOBU_NM"
        for _, r in divs.iterrows():
            div_names[_norm_code(r["JIGYOBU_CD"])] = (_norm_code(r["KAISHA_CD"]), str(r[nm]).strip())
    with db._lock, db.conn() as c:
        O = _Org(c)
        # 既存の事業部名（今回のファイルに無い場合に会社名の推定へ使う）
        for r in O.by_id.values():
            if r["kind"] == "division" and r.get("source") == "master" and r["code"] not in div_names:
                p = O.by_id.get(r["parent_id"])
                if p: div_names.setdefault(r["code"], (p["code"], r["name"]))
        comp_codes = {v[0] for v in div_names.values()}
        if stores is not None: comp_codes |= {_norm_code(x) for x in stores["KAISHA_CD"]}
        comp_id = {}
        for cc in sorted(comp_codes, key=lambda x: int(x) if x.isdigit() else 0):
            guess = company_name_guess([n for k, n in div_names.values() if k == cc]) or f"会社{cc}"
            comp_id[cc] = O.upsert("company", cc, guess, None, keep_name=True)
        div_codes = dict(div_names)
        if stores is not None:
            for _, r in stores[["KAISHA_CD", "JIGYOBU_CD"]].drop_duplicates().iterrows():
                dc = _norm_code(r["JIGYOBU_CD"])
                if dc not in div_codes: div_codes[dc] = (_norm_code(r["KAISHA_CD"]), None)
        div_id = {}
        for dc, (cc, name) in sorted(div_codes.items(), key=lambda x: int(x[0]) if x[0].isdigit() else 0):
            div_id[dc] = O.upsert("division", dc, name or f"事業部{dc}", comp_id[cc], keep_name=name is None)
        if stores is not None:
            cols = [k for k in ("URIBA_MENSEKI", "SIKICHI_MENSEKI", "MISE_KBN") if k in stores.columns]
            for _, r in stores.iterrows():
                dc, sc = _norm_code(r["JIGYOBU_CD"]), _norm_code(r["TENPO_CD"])
                info = {k: int(r[k]) for k in cols if pd.notna(r[k])}
                O.upsert("store", sc, str(r["TENPO_NM"]).strip(), div_id[dc], info=info, parent_code=dc)
        c.commit()
    return O.stats
