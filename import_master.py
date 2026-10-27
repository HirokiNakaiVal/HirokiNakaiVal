"""会社・事業部・店舗マスタをコマンドで取り込む

使い方：python import_master.py 会社事業部.xlsx 会社事業部店舗.xlsx
（保存先は KADOU_DATA_DIR、未設定なら data\\）
"""
import sys, os
from app import db, master

if __name__ == "__main__":
    paths = sys.argv[1:]
    if not paths:
        print(__doc__); sys.exit(1)
    db.init()
    st = master.import_master([(os.path.basename(p), open(p, "rb").read()) for p in paths])
    label = {"company": "会社", "division": "事業部", "store": "店舗"}
    for k, v in st.items():
        print(f"{label[k]}：追加{v['追加']}件・更新{v['更新']}件")
