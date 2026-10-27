"""1日分の稼働計画を作る（勤務・休憩・作業推定・タスクリレー・持ち場・レジ割当）"""
import math
import pandas as pd
from .common import (SLOT, WD, m, hm, to_min, isreg, reg_label, reg_no, is_time, is_excluded_day,
                     merged_settings, getsudo, getsudo_range, getsudo_label)

class PlanError(Exception):
    pass

class DayPlanner:
    def __init__(self, data, settings, day, store_name="", labor=None):
        self.d = data
        self.labor_src = labor or {}          # {"YYYY-MM": {"regular","part","arbeit","total"}}
        self.S = merged_settings(settings)
        self.day = pd.Timestamp(day).normalize()
        self.store_name = store_name
        self.warnings = []
        self.notes = []

    # ------------------------------------------------------------------ 入力
    def _prepare(self):
        d, S, day = self.d, self.S, self.day
        if d.plan is None: raise PlanError("月間稼働表が取り込まれていません")
        self.allp = d.plan.sort_values("日付", kind="stable")
        # 表示順は月間稼働表どおり（部署順・社員順）
        pday = self.allp[self.allp["日付"] == day]
        if "順" in pday: pday = pday.sort_values("順", kind="stable")
        self.plan = pday.reset_index(drop=True)
        if self.plan.empty:
            raise PlanError(f"{day:%Y/%m/%d} の月間稼働表がありません（取込済み期間："
                            f"{self.allp['日付'].min():%Y/%m/%d}〜{self.allp['日付'].max():%Y/%m/%d}）")
        self.PE = self.allp["日付"].max()
        self.st_day = dict(zip(self.plan["社員名"], self.plan["区分"]))
        self.dept_of = dict(zip(self.plan["社員名"], self.plan["部門"]))
        self.OPEN, self.CLOSE = to_min(S["open"]), to_min(S["close"])
        self.open_T = range(self.OPEN // SLOT, self.CLOSE // SLOT)
        self.REGDEPT = S["register_dept"]
        self.START = list(S["start_items"]); self.END = list(S["end_items"])
        self.EXCL = set(S["exclude_items"])
        self.NOON = to_min(S["noon_meeting"])
        ev = self.allp.dropna(subset=["行事"]).groupby("日付")["行事"].first()
        self.events = ev
        self.event_today = ev.get(day)
        # KADOU実績（指定日の直前 history_days 日）
        if d.kadou is None:
            self.hist = pd.DataFrame(columns=["nm", "ymd", "item", "s_time"])
            self.warnings.append("KADOU実績が無いため、作業は部門の既定（接客）で作成しました")
        else:
            k = d.kadou
            self.hist = k[(k["ymd"] < day) & (k["ymd"] >= day - pd.Timedelta(days=int(S["history_days"])))].copy()
            if self.hist.empty:
                self.warnings.append(f"{day:%Y/%m/%d}の直前{S['history_days']}日にKADOU実績がありません")
        self.hist["wd"] = pd.to_datetime(self.hist["ymd"]).dt.weekday if len(self.hist) else pd.Series(dtype=int)
        self.hist_from = self.hist["ymd"].min() if len(self.hist) else None
        self.hist_to = self.hist["ymd"].max() if len(self.hist) else None
        self.kero = S["kerosene_item"] if S["kerosene_item"] and (self.hist["item"] == S["kerosene_item"]).any() else None

    # ------------------------------------------------------------------ 休憩の学習
    def _learn_breaks(self):
        p = self.allp[(self.allp["日付"] < self.day) & self.allp["開始"].apply(is_time)].copy()
        if p.empty or self.hist.empty:
            self.bj = pd.DataFrame(columns=["社員名", "拘束", "休憩"]); return
        p["拘束"] = [m(e) - m(s) for s, e in zip(p["開始"], p["終了"])]
        w = self.hist.groupby(["nm", "ymd"])["s_time"].sum().rename("作業").reset_index()
        bj = p.merge(w, left_on=["社員名", "日付"], right_on=["nm", "ymd"])
        bj["休憩"] = bj["拘束"] - bj["作業"]
        self.bj = bj[bj["休憩"] >= 0]

    def break_for(self, nm, sh):
        bj = self.bj
        own = bj[(bj["社員名"] == nm) & (bj["拘束"] == sh)]["休憩"]
        if len(own): return int(own.mode().iloc[0]) // 15 * 15, f"本人の実績（同じ拘束時間{len(own)}日）"
        al = bj[bj["拘束"] == sh]["休憩"]
        if len(al): return int(al.mode().iloc[0]) // 15 * 15, f"店全体の実績（同じ拘束時間{len(al)}件）"
        v = 0 if sh <= 330 else 60 if sh <= 540 else 90 if sh <= 570 else 120
        return v, "ルール（6時間以下0分・9時間まで60分・9.5時間90分・それ以上120分）"

    # ------------------------------------------------------------------ 作業の推定
    def estimate(self, nm, W):
        key = (nm, W)
        if key in self._est: return self._est[key]
        h = self.hist[(self.hist["nm"] == nm) & ~self.hist["item"].isin(self.EXCL)]
        wdn = WD[self.day.weekday()]
        if h.empty:
            dept = self.dept_of.get(nm)
            peers = set(self.plan.loc[self.plan["部門"] == dept, "社員名"])
            pk = self.hist[self.hist["nm"].isin(peers) & ~self.hist["item"].isin(self.EXCL)]
            base = pk.groupby("item")["s_time"].sum().idxmax() if len(pk) else "接客"
            res = ([(base, W)], base, [[nm, f"実績なし（{dept}で最も多い作業）", 0, base, 0, 0, "基本作業", W]])
            self._est[key] = res; return res
        hw = h[h["wd"] == self.day.weekday()]
        src, basis = (hw, f"同じ{wdn}曜") if hw["ymd"].nunique() >= 2 else (h, "全日")
        nd = src["ymd"].nunique()
        daily = src.groupby(["item", "ymd"])["s_time"].sum().reset_index()
        st_ = daily.groupby("item").agg(日数=("ymd", "nunique"), 合計=("s_time", "sum"),
                                        中央値=("s_time", "median")).reset_index()
        st_["実施率"] = st_["日数"] / nd
        base = st_.loc[st_["合計"].idxmax(), "item"]
        fx = st_[(st_["item"] != base) & (st_["実施率"] >= 0.5)].sort_values("実施率", ascending=False)
        take = {}
        for _, x in fx.iterrows():
            v = int(round(x["中央値"] / 15) * 15)
            if v and sum(take.values()) + v <= W - 30: take[x["item"]] = v
        take[base] = W - sum(take.values())
        rows = []
        for _, x in st_.sort_values("合計", ascending=False).iterrows():
            a = x["item"]
            kind = "基本作業" if a == base else "定常作業" if a in take else "不採用"
            rows.append([nm, basis, nd, a, int(x["日数"]), int(x["中央値"]), kind, take.get(a, 0)])
        res = ([(a, v) for a, v in take.items() if a != base] + [(base, take[base])], base, rows)
        self._est[key] = res
        return res

    # ------------------------------------------------------------------ タスクリレー
    def _tasks(self):
        d, day = self.d, self.day
        if d.tasks is None:
            self.tr = pd.DataFrame(columns=["作業指示ID", "作業者氏名", "nm", "タイトル", "実施期間F", "実施期間TO",
                                            "作業予定時間", "予定時間補完", "期間内出勤日数", "計画外換算日数", "割当分"])
            self.tr_placed = {}
            self.notes.append("タスクリレーは取り込まれていません"); return
        t = d.tasks.copy()
        t = t[t["nm"].isin(set(self.allp["社員名"]))]                 # この店舗の社員のみ
        med = float(t["作業予定時間"].median()) if t["作業予定時間"].notna().any() else 1.0
        self.tr_med = med
        t["予定時間補完"] = t["作業予定時間"].isna()
        t["作業予定時間"] = t["作業予定時間"].fillna(med)
        t = t[(t["実施期間F"] <= day) & (t["実施期間TO"] >= day)].copy()
        t = t.sort_values(["nm", "実施期間TO", "作業指示ID"]).reset_index(drop=True)
        wk = self.allp[self.allp["区分"].isin(["出勤", "半休"])]
        a_, b_, al = [], [], []
        for _, x in t.iterrows():
            f, to = max(x["実施期間F"], day), x["実施期間TO"]
            a = wk[(wk["社員名"] == x["nm"]) & (wk["日付"] >= f) & (wk["日付"] <= min(to, self.PE))]["日付"].nunique()
            b = round(max(0, (to - self.PE).days) * 5 / 7, 1)
            on = self.st_day.get(x["nm"]) in ("出勤", "半休")
            a_.append(a); b_.append(b)
            al.append(int(math.ceil(x["作業予定時間"] * 60 / (a + b) / 15) * 15) if on and a + b else 0)
        t["期間内出勤日数"], t["計画外換算日数"], t["割当分"] = a_, b_, al
        self.tr = t
        self.tr_placed = {}

    def add_tasks(self, nm, items, bi):
        basev = sum(v for a, v in items if a == bi)
        cap = max(0, basev - int(self.S["tr_keep"]))
        take, used = [], 0
        for i, v in self.tr[(self.tr["nm"] == nm) & (self.tr["割当分"] > 0)][["作業指示ID", "割当分"]].values:
            x = int(min(v, cap - used) // 15 * 15)
            self.tr_placed[int(i)] = max(x, 0)
            if x > 0: take.append((f"TR:{int(i)}", x)); used += x
        if not take: return items
        return [(a, v) for a, v in items if a != bi] + take + [(bi, basev - used)]

    # ------------------------------------------------------------------ 来客予測
    def _forecast(self):
        d, S, day = self.d, self.S, self.day
        hs, rs = d.sales_hour, d.sales_reg
        self.has_sales = hs is not None and len(hs) > 0
        hours = range(self.OPEN // 60, self.CLOSE // 60 + 1)
        # 有人レジ
        hist_regs = sorted({reg_no(a) for a in self.hist["item"].unique() if isreg(a)})
        st_cfg = S["staffed_registers"]
        self.staffed = hist_regs if st_cfg in ("auto", None, "") else [int(x) for x in st_cfg]
        self.REGS = [reg_label(n) for n in self.staffed]
        # 専任レジ（レジ部門が担当しない）
        dc = S["dedicated_registers"]
        if dc in ("auto", None, ""):
            ded = []
            hh = self.hist.assign(dept=self.hist["nm"].map(dict(zip(self.allp["社員名"], self.allp["部門"]))))
            for r in self.REGS:
                x = hh[hh["item"] == r]
                if len(x) and x.loc[x["dept"] == self.REGDEPT, "s_time"].sum() < 0.2 * x["s_time"].sum():
                    ded.append(r)
            self.DED = ded
        else:
            self.DED = [reg_label(x) for x in dc]
        self.spike = {dd for dd, t in self.events.items() if any(k in str(t) for k in S["spike_keywords"])}
        if not self.has_sales:
            self.REF, self.SCALE, self.BUD = [], 1.0, None
            self.fc_h = pd.Series(0.0, index=list(hours)); self.prof_h = self.fc_h.copy()
            self.fc_r = pd.DataFrame(0.0, index=list(hours), columns=self.staffed)
            self.act_h = pd.Series(dtype=float)
            self.warnings.append("売上実績が無いため、来客予測なしで作成しました（必要レジ台数は最低台数）")
        else:
            daily = hs.groupby("取引日")[["売上高", "客数"]].sum()
            ref = [x for x in pd.date_range(day - pd.Timedelta(days=13 * 7), day - pd.Timedelta(days=1))
                   if x.weekday() == day.weekday() and not is_excluded_day(x) and x not in self.spike
                   and x in daily.index][-int(S["ref_weeks"]):]
            if not ref: raise PlanError("来客予測に使える同じ曜日の売上実績がありません")
            self.REF = ref
            self.ref_sales = float(daily.loc[ref, "売上高"].mean())
            self.BUD = None
            if d.budget is not None:
                b = d.budget.set_index("日付")["予算売上"]
                if day in b.index: self.BUD = float(b[day])
            if self.BUD is None:
                self.SCALE = 1.0
                self.warnings.append("日割予算が無いため、基準日の平均どおりで予測しました")
            else:
                self.SCALE = self.BUD / self.ref_sales
            prof = hs[hs["取引日"].isin(ref)].groupby("h")[["売上高", "客数"]].sum() / len(ref)
            self.prof_h = prof["客数"]
            self.fc_h = prof["客数"] * self.SCALE
            if rs is not None and len(rs):
                rp = rs[rs["取引日"].isin(ref)].pivot_table(index="h", columns="レジNo", values="客数",
                                                             aggfunc="sum").fillna(0) / len(ref)
                self.fc_r = rp * self.SCALE
            else:
                self.fc_r = pd.DataFrame(0.0, index=self.fc_h.index, columns=self.staffed)
                self.warnings.append("レジ別の売上実績が無いため、レジの優先順は番号順です")
            self.act_h = hs[hs["取引日"] == day].set_index("h")["客数"]
        cols = [c for c in self.fc_r.columns if c in self.staffed]
        self.fc_staffed = self.fc_r[cols].sum(axis=1) if cols else pd.Series(0.0, index=self.fc_r.index)
        cap, ml = float(S["cap"]), int(S["min_lanes"])
        self.need = {h: max(ml, math.ceil(float(self.fc_staffed.get(h, 0)) / cap))
                     for h in range(self.OPEN // 60, self.CLOSE // 60)}

    def dem(self, lab, T):
        n = reg_no(lab)
        if n not in self.fc_r.columns: return 0.0
        return float(self.fc_r[n].get(T // 4, 0)) - n * 1e-6     # 同点は番号の小さい順

    def slot_fc(self, T):
        return float(self.fc_h.get(T // 4, 0)) / 4

    # ------------------------------------------------------------------ 休憩位置
    def _place_breaks(self):
        self.BRK_AT, cnt = {}, {}
        rows = [(p["部門"], p["社員名"], m(p["開始"]), m(p["終了"])) for _, p in self.plan.iterrows()
                if p["区分"] in ("出勤", "半休") and is_time(p["開始"])]
        for dept, nm, st, en in sorted(rows, key=lambda x: (x[0] != self.REGDEPT, x[2])):
            brk, _ = self.break_for(nm, en - st)
            items, _, _ = self.estimate(nm, en - st - brk)
            ng = [(self.NOON, self.NOON + dict(items)["昼礼"])] if "昼礼" in dict(items) else []
            lo1, hi1 = (0.25, 0.8) if dept == self.REGDEPT else (0.35, 0.65)
            parts = ([(min(brk, 60), lo1, hi1)] + ([(brk - 60, 0.72, 0.92)] if brk > 60 else [])) if brk > 0 else []
            blks = []
            for L, lo, hi in parts:
                best = None
                for t0 in range(st + int((en - st) * lo) // 30 * 30, st + int((en - st) * hi) - L + 1, 30):
                    if t0 < st + 60 or t0 + L > en - 60: continue
                    if any(not (t0 + L <= a or b <= t0) for a, b in ng + blks): continue
                    Ts = range(t0 // SLOT, (t0 + L) // SLOT)
                    lam = 1000 if dept == self.REGDEPT else 30
                    cost = sum(self.slot_fc(T) for T in Ts) + lam * sum(cnt.get((dept, T), 0) for T in Ts)
                    if best is None or cost < best[0]: best = (cost, t0)
                if best is None:
                    t0 = st + round((en - st) * (lo + hi) / 2 / 30) * 30
                    t0 = max(st, min(en - L, t0))
                    best = (0, t0)
                blks.append((best[1], best[1] + L))
                for T in range(best[1] // SLOT, (best[1] + L) // SLOT): cnt[(dept, T)] = cnt.get((dept, T), 0) + 1
            self.BRK_AT[nm] = blks

    # ------------------------------------------------------------------ 1時間交代の持ち場（灯油など）
    def _kerosene(self):
        self.KERO_PLAN, self.KERO_PEOPLE = {}, set()
        if not self.kero: return
        K = self.kero
        hours = list(range(self.OPEN // 60, self.CLOSE // 60))
        units, cand = [], {}
        for _, p in self.plan.iterrows():
            nm = p["社員名"]
            if p["区分"] not in ("出勤", "半休") or not is_time(p["開始"]): continue
            st, en = m(p["開始"]), m(p["終了"])
            brk, _ = self.break_for(nm, en - st)
            items, bi, _ = self.estimate(nm, en - st - brk)
            v = dict(items).get(K, 0)
            if bi == K or not v: continue
            ng = list(self.BRK_AT.get(nm, []))
            if "昼礼" in dict(items): ng.append((self.NOON, self.NOON + dict(items)["昼礼"]))
            ok = [h for h in hours if st <= h * 60 and h * 60 + 60 <= en
                  and all(h * 60 + 60 <= a or b <= h * 60 for a, b in ng)]
            ok.sort(key=lambda h: (h * 60 < st + 30 or h * 60 + 60 > en - 30, abs(h * 60 + 30 - (st + en) / 2)))
            for k in range(max(1, round(v / 60))):
                units.append((nm, k)); cand[(nm, k)] = ok
        match = {}
        def aug(u, seen):
            for h in cand[u]:
                if h in seen: continue
                seen.add(h)
                if h not in match or aug(match[h], seen):
                    match[h] = u; return True
            return False
        for u in sorted(units, key=lambda u: len(cand[u])): aug(u, set())
        for h, (nm, _) in match.items(): self.KERO_PLAN.setdefault(nm, []).append(h * 60)
        self.KERO_PEOPLE = {nm for nm, _ in units}
        self.kero_empty = [h for h in hours if h not in match]
        if self.kero_empty:
            self.warnings.append(f"{K}の担当がいない時間帯：" + "、".join(f"{h}時" for h in self.kero_empty))
        extra = sorted(self.KERO_PEOPLE - set(self.KERO_PLAN))
        if extra: self.notes.append(f"{K}の枠に入らず基本作業に回した人：" + "、".join(extra))

    def apply_kero(self, nm, items, bi):
        K = self.kero
        v = dict(items).get(K, 0) if K else 0
        if not v or bi == K: return items, None
        hrs = self.KERO_PLAN.get(nm, [])
        left = v - 60 * len(hrs)
        items = [(k, x) for k, x in items if k != K]
        items = [(k, x + left if k == bi else x) for k, x in items]
        return items, {t: (K, 60) for t in hrs}

    # ------------------------------------------------------------------ 時間帯への配置
    def place(self, st, en, items, base, nm, fixed=None):
        n = (en - st) // SLOT
        s = [None] * n
        for t0, (a, v) in (fixed or {}).items():
            for j in range(v // SLOT): s[(t0 - st) // SLOT + j] = a
        for a, b in self.BRK_AT.get(nm, []):
            for i in range((a - st) // SLOT, (b - st) // SLOT): s[i] = "休憩"
        rest = {}
        for k, v in items: rest[k] = rest.get(k, 0) + v
        if "朝礼" in rest and st > self.OPEN:
            rest[base] = rest.get(base, 0) + rest.pop("朝礼")
        if "昼礼" in rest:
            v = rest.pop("昼礼"); i0 = (self.NOON - st) // SLOT
            for j in range(v // SLOT):
                if 0 <= i0 + j < n and s[i0 + j] is None: s[i0 + j] = "昼礼"
                else: rest[base] = rest.get(base, 0) + SLOT
        free = [i for i in range(n) if s[i] is None]
        for k in self.START:
            if k in rest and k != base:
                for _ in range(rest.pop(k) // SLOT):
                    if free: s[free.pop(0)] = k
        for k in self.END:
            if k in rest and k != base:
                for _ in range(rest.pop(k) // SLOT):
                    if free: s[free.pop()] = k
        trk = [k for k in rest if k.startswith("TR:")]
        tot = sum(rest[k] for k in trk) // SLOT
        if tot and tot <= len(free):
            best = None
            for j in range(len(free) - tot + 1):
                win = free[j:j + tot]
                gaps = sum(1 for a, b in zip(win, win[1:]) if b != a + 1)
                cost = sum(self.slot_fc(st // SLOT + i) for i in win) + 200 * gaps
                if best is None or cost < best[0]: best = (cost, win)
            win = best[1]; q = 0
            for k in trk:
                for _ in range(rest.pop(k) // SLOT): s[win[q]] = k; q += 1
            free = [i for i in range(n) if s[i] is None]
        for k in [k for k in rest if k != base] + [base]:
            for _ in range(rest.get(k, 0) // SLOT):
                if free: s[free.pop(0)] = k
        for i in free: s[i] = base          # 端数（15分未満の切捨て分など）は基本作業
        return s

    def _people(self):
        self.people, self.est_rows = {}, []
        for dept, g in self.plan.groupby("部門", sort=False):
            for _, p in g.iterrows():
                nm = p["社員名"]
                rec = dict(dept=dept, kubun=p["区分"], st=None)
                if p["区分"] in ("出勤", "半休") and is_time(p["開始"]):
                    st, en = m(p["開始"]), m(p["終了"])
                    brk, bsrc = self.break_for(nm, en - st)
                    items, bi, er = self.estimate(nm, en - st - brk)
                    items, fixed = self.apply_kero(nm, items, bi)
                    items = self.add_tasks(nm, items, bi)
                    sl = self.place(st, en, items, bi, nm, fixed)
                    self.est_rows += er
                    rec.update(st=st, en=en, base=bi, brk=brk, bsrc=bsrc,
                               slots={st // SLOT + i: a for i, a in enumerate(sl)})
                self.people[nm] = rec

    # ------------------------------------------------------------------ レジ割当
    def _registers(self):
        P, REGS, open_T = self.people, self.REGS, self.open_T
        self.occ = occ = {r: {} for r in REGS}
        free = lambda r, T: T not in occ[r]
        lanes = lambda T: sum(1 for r in REGS if T in occ[r])
        self.lanes = lanes
        habit = {}
        for nm, g in self.hist[self.hist["item"].apply(isreg)].groupby("nm"):
            s_ = g.groupby("item")["s_time"].sum()
            habit[nm] = [r for r in s_.index if s_[r] >= s_.sum() * 0.1 and r in occ]
        # 対象外のレジ番号（有人レジに無い）は基本作業へ
        for nm, p in P.items():
            if p["st"] is None: continue
            for T, a in p["slots"].items():
                if isreg(a) and a not in occ: p["slots"][T] = p["base"] if not isreg(p["base"]) else "補充"
        FIXED = {"休憩", self.kero, "朝礼", "昼礼", *self.START, *self.END}
        # ⓪ レジ部門以外のレジ専任（基本作業がレジ）
        self.dedicated = [nm for nm, r in P.items()
                          if r["st"] is not None and r["dept"] != self.REGDEPT and isreg(r.get("base"))]
        for nm in sorted(self.dedicated, key=lambda n: -P[n]["st"]):
            s = P[nm]["slots"]
            for T in sorted(s):
                a = s[T]
                if not isreg(a): continue
                if T in open_T and free(a, T):
                    occ[a][T] = nm; continue
                sw = next((U for U in sorted(s, key=lambda U: -self.dem(a, U)) if U in open_T and free(a, U)
                           and not isreg(s[U]) and s[U] not in FIXED and not str(s[U]).startswith("TR:")), None)
                if sw is not None:
                    s[T], s[sw] = s[sw], a; occ[a][sw] = nm
                else:
                    s[T] = "接客"
        # ① レジ部門
        regp = sorted([nm for nm, r in P.items() if r["dept"] == self.REGDEPT and r["st"] is not None],
                      key=lambda n: P[n]["st"])
        for nm in regp:
            for T, a in P[nm]["slots"].items():
                if isreg(a): P[nm]["slots"][T] = "REG"
        for T in range(24 * 4):
            av = [nm for nm in regp if P[nm]["slots"].get(T) == "REG"]
            if not av: continue
            if T not in open_T:
                for nm in av: P[nm]["slots"][T] = "補充"
                continue
            rank = sorted([r for r in REGS if r not in self.DED and free(r, T)], key=lambda r: -self.dem(r, T))
            target = rank[:len(av)]
            rest_p = []
            for nm in av:
                prev = P[nm]["slots"].get(T - 1)
                if prev in target:
                    P[nm]["slots"][T] = prev; occ[prev][T] = nm; target.remove(prev)
                else: rest_p.append(nm)
            for nm in rest_p:
                if target:
                    x = target.pop(0); P[nm]["slots"][T] = x; occ[x][T] = nm
                else: P[nm]["slots"][T] = "補充"
        # ② 他部門の応援
        others = [nm for nm, r in P.items() if r["st"] is not None and r["dept"] != self.REGDEPT
                  and nm not in self.dedicated and any(isreg(a) for a in r["slots"].values())]
        others.sort(key=lambda n: -sum(isreg(a) for a in P[n]["slots"].values()))
        need = self.need
        short = lambda T: (lanes(T) < need[T // 4]) if T in open_T else False
        self.moved = {}
        for nm in others:
            r = P[nm]; s = r["slots"]
            Rm = [T for T, a in s.items() if isreg(a)]
            H = habit.get(nm) or REGS
            for T in Rm: s[T] = r["base"]
            cand = sorted(T for T, a in s.items() if a == r["base"] and T in open_T)
            def pickr(T, prev=None):
                fr = [x for x in H if free(x, T)]
                if not fr and short(T): fr = [x for x in REGS if x not in self.DED and free(x, T)]
                if prev in fr: return prev
                return max(fr, key=lambda x: self.dem(x, T)) if fr else None
            runs, cur = [], []
            for T in cand:
                ok = short(T) and pickr(T)
                if ok and cur and T == cur[-1] + 1: cur.append(T)
                else:
                    if cur: runs.append(cur)
                    cur = [T] if ok else []
            if cur: runs.append(cur)
            runs.sort(key=lambda run: -sum(need[T // 4] - lanes(T) for T in run))
            placed, prev = 0, None
            for run in runs:
                for T in run:
                    if placed >= len(Rm): break
                    x = pickr(T, prev)
                    if x: s[T] = x; occ[x][T] = nm; prev = x; placed += 1
            for T in sorted(cand, key=lambda T: -max([self.dem(x, T) for x in H if free(x, T)] or [0])):
                if placed >= len(Rm): break
                if s[T] != r["base"]: continue
                x = pickr(T)
                if x and self.dem(x, T) >= 5: s[T] = x; occ[x][T] = nm; placed += 1
            self.moved[nm] = (len(Rm) * SLOT, placed * SLOT)

    # ------------------------------------------------------------------ 集計
    def _summary(self):
        hours = range(self.OPEN // 60, self.CLOSE // 60)
        self.hourly = []
        for h in hours:
            L = sum(self.lanes(T) for T in range(h * 4, h * 4 + 4)) / 4
            self.hourly.append(dict(h=h, fc=float(self.fc_h.get(h, 0)), act=self.act_h.get(h),
                                    fc_staffed=float(self.fc_staffed.get(h, 0)), need=self.need[h], lanes=L))
        short = [x for x in self.hourly if x["lanes"] + 0.25 < x["need"]]
        if short:
            self.warnings.append("レジ台数が不足する時間帯：" +
                                 "、".join(f"{x['h']}時（必要{x['need']}・配置{x['lanes']:.1f}）" for x in short))
        for r in self.DED:
            gap = [T for T in self.open_T if T not in self.occ.get(r, {}) and self.dem(r, T) >= 15]
            if gap:
                self.warnings.append(f"{r}（専任）が無人の時間帯：{hm(gap[0]*SLOT)}〜 計{len(gap)*SLOT}分")
        carry = {}
        for _, t in self.tr.iterrows():
            c = int(t["割当分"]) - self.tr_placed.get(int(t["作業指示ID"]), int(t["割当分"]) if t["割当分"] else 0)
            if c > 0: carry[t["nm"]] = carry.get(t["nm"], 0) + c
        self.carry = carry
        if carry:
            self.warnings.append("タスクリレーの持越し（1日の上限超過）：" +
                                 "、".join(f"{k} {v}分" for k, v in carry.items()))
        nohist = sorted({r[0] for r in self.est_rows if str(r[1]).startswith("実績なし")})
        if nohist: self.notes.append("実績が無く部門の主作業で推定した人：" + "、".join(nohist))
        self.tr_by_person = (self.tr[self.tr["割当分"] > 0].groupby("nm")["割当分"].sum().to_dict()
                             if len(self.tr) else {})

    # ------------------------------------------------------------------ 労働時間予算との比較
    def _labor(self):
        sd = int(self.S["month_start_day"])
        day, ym = self.day, getsudo(self.day, sd)
        work = [p for p in self.people.values() if p["st"] is not None]
        self.planned_hours = sum((p["en"] - p["st"] - p["brk"]) for p in work) / 60
        lb = self.labor_src.get(ym)
        self.labor = None
        if not lb:
            if self.labor_src: self.notes.append(f"{getsudo_label(ym, sd)}の労働時間予算がありません")
            return
        month_total = float(lb["total"])
        from .compare import labor_day_budget
        dbm, _ = labor_day_budget(self.d, self.labor_src, self.S, [day])      # 比較画面と同じ日割り
        x = dbm.get(day)
        ratio = x["ratio"] if x else 1 / ((getsudo_range(ym, sd)[1] - getsudo_range(ym, sd)[0]).days + 1)
        method = f"{getsudo_label(ym, sd)}の予算を{x['how'] if x else '日数で均等割り'}で日割り"
        day_budget = month_total * ratio
        self.labor = dict(month=month_total, ym=ym, getsudo=getsudo_label(ym, sd),
                          regular=float(lb["regular"] or 0), part=float(lb["part"] or 0),
                          arbeit=float(lb["arbeit"] or 0), ratio=ratio, method=method,
                          day=day_budget, planned=self.planned_hours, diff=self.planned_hours - day_budget)
        if self.planned_hours > day_budget * 1.05:
            self.warnings.append(f"計画実働 {self.planned_hours:.1f}h が労働時間予算（日割）{day_budget:.1f}h を "
                                 f"{self.planned_hours - day_budget:.1f}h 超えています")
        elif self.planned_hours < day_budget * 0.9:
            self.notes.append(f"計画実働 {self.planned_hours:.1f}h は労働時間予算（日割）{day_budget:.1f}h より "
                              f"{day_budget - self.planned_hours:.1f}h 少ない計画です")

    def run(self):
        self._est = {}
        self._prepare()
        self._learn_breaks()
        self._tasks()
        self._forecast()
        self._place_breaks()
        self._kerosene()
        self._people()
        self._registers()
        self._labor()
        self._summary()
        return self

    def summary_dict(self):
        work = [p for p in self.people.values() if p["st"] is not None]
        return dict(
            day=f"{self.day:%Y-%m-%d}", weekday=WD[self.day.weekday()], event=self.event_today,
            workers=len(work), budget=self.BUD, scale=round(self.SCALE, 3),
            ref_days=[f"{x:%m/%d}" for x in self.REF],
            fc_total=round(sum(x["fc"] for x in self.hourly)),
            hourly=[dict(h=x["h"], fc=round(x["fc"]), need=x["need"], lanes=round(x["lanes"], 1),
                         act=(None if x["act"] is None or pd.isna(x["act"]) else int(x["act"]))) for x in self.hourly],
            tasks=dict(count=int((self.tr["割当分"] > 0).sum()) if len(self.tr) else 0,
                       minutes=int(sum(self.tr_placed.values())) if hasattr(self, "tr_placed") else 0,
                       carry=self.carry, by_person=self.tr_by_person),
            labor=(None if not self.labor else {k: (round(v, 5 if k == "ratio" else 1) if isinstance(v, float) else v) for k, v in self.labor.items()}),
            planned_hours=round(self.planned_hours, 1),
            warnings=self.warnings, notes=self.notes,
        )
