import datetime as dt
import pandas as pd

SLOT = 15                       # 分
WD = "月火水木金土日"
FW = str.maketrans("0123456789", "０１２３４５６７８９")
HW = str.maketrans("０１２３４５６７８９", "0123456789")

# 祝日・お盆・年末年始（来客予測の基準日から除く）
HOLIDAYS = pd.to_datetime([
    "2025-01-01", "2025-01-13", "2025-02-11", "2025-02-23", "2025-02-24", "2025-03-20", "2025-04-29",
    "2025-05-03", "2025-05-04", "2025-05-05", "2025-05-06", "2025-07-21", "2025-08-11", "2025-09-15",
    "2025-09-23", "2025-10-13", "2025-11-03", "2025-11-23", "2025-11-24",
    "2026-01-01", "2026-01-12", "2026-02-11", "2026-02-23", "2026-03-20", "2026-04-29", "2026-05-03",
    "2026-05-04", "2026-05-05", "2026-05-06", "2026-07-20", "2026-08-11", "2026-09-21", "2026-09-22",
    "2026-09-23", "2026-10-12", "2026-11-03", "2026-11-23",
    "2027-01-01", "2027-01-11", "2027-02-11", "2027-02-23", "2027-03-21", "2027-03-22", "2027-04-29",
    "2027-05-03", "2027-05-04", "2027-05-05", "2027-07-19", "2027-08-11", "2027-09-20", "2027-09-23",
    "2027-10-11", "2027-11-03", "2027-11-23",
])

def getsudo(d, start=16):
    """日付 → 月度（"YYYY-MM"）。start=16 なら 2/16〜3/15 が 3月度"""
    d = pd.Timestamp(d)
    if d.day >= int(start):
        n = d + pd.offsets.MonthBegin(1)
        return f"{n:%Y-%m}"
    return f"{d:%Y-%m}"

def getsudo_range(ym, start=16):
    """月度 "YYYY-MM" → (開始日, 終了日)。start=16 なら 3月度 = 前月16日〜当月15日"""
    first = pd.Timestamp(ym + "-01")
    if int(start) <= 1:
        return first, first + pd.offsets.MonthEnd(0)
    d0 = (first - pd.offsets.MonthBegin(1)).replace(day=int(start))
    d1 = first.replace(day=int(start) - 1)
    return d0, d1

def getsudo_label(ym, start=16):
    d0, d1 = getsudo_range(ym, start)
    return f"{int(ym[5:7])}月度（{d0.month}/{d0.day}〜{d1.month}/{d1.day}）"

def is_excluded_day(d):
    """祝日・お盆(8/13-16)・年末年始(12/29-1/3)"""
    if d in HOLIDAYS: return True
    if d.month == 8 and 13 <= d.day <= 16: return True
    if (d.month == 12 and d.day >= 29) or (d.month == 1 and d.day <= 3): return True
    return False

def m(t):
    return t.hour * 60 + t.minute

def hm(x):
    return f"{x // 60}:{x % 60:02d}"

def to_min(s):
    h, mi = str(s).split(":")[:2]
    return int(h) * 60 + int(mi)

def isreg(a):
    """KADOUのレジ作業（１R, １０R など）"""
    return isinstance(a, str) and a.endswith("R") and len(a) > 1 and a[:-1].translate(HW).isdigit()

def reg_label(n):
    return f"{str(int(n)).translate(FW)}R"

def reg_no(label):
    return int(label[:-1].translate(HW))

def is_time(x):
    return isinstance(x, dt.time)

DEFAULT_SETTINGS = {
    "open": "09:00",                 # 営業開始
    "close": "20:00",                # 営業終了
    "register_dept": "レジ",          # レジ部門の名称（月間稼働表の部門名）
    "staffed_registers": "auto",     # 有人レジ番号（auto＝KADOUに○Rの作業があるレジ）
    "dedicated_registers": "auto",   # レジ部門以外が専任で担当するレジ（auto＝実績でレジ部門の担当が2割未満）
    "cap": 30,                       # 有人レジ1台が1時間にさばく客数の目安
    "min_lanes": 2,                  # 最低レジ台数
    "kerosene_item": "灯油1",         # 1時間交代の持ち場（実績に無ければ使わない）
    "tr_keep": 60,                   # タスクリレーを入れても残す基本作業の分数
    "history_days": 35,              # 作業推定に使う実績の日数（指定日の直前）
    "ref_weeks": 8,                  # 来客予測に使う同じ曜日の週数
    "start_items": ["開店業務", "釣銭準備", "検収", "朝礼"],
    "end_items": ["清掃", "精算", "閉店業務"],
    "exclude_items": ["研修", "会議"],
    "spike_keywords": ["5％デー", "5%デー"],
    "noon_meeting": "13:00",         # 昼礼の時刻
    "month_start_day": 16,           # 月度の開始日（16＝前月16日〜当月15日。労働時間予算の「3月」＝2/16〜3/15）
    "leave_hours": 0,                # 労働時間予算と比べるとき、有休・会議・研修の日を1日何時間とみなすか（0＝含めない）
    "leave_kinds": ["有休", "会議", "研修"],
}

def merged_settings(s):
    out = dict(DEFAULT_SETTINGS)
    for k, v in (s or {}).items():
        if v not in (None, ""): out[k] = v
    return out
