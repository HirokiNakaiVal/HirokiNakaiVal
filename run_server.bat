@echo off
rem 日別稼働計画アプリ 起動（社内サーバー用）
rem   ポート番号を変える場合は PORT を変更
rem   ログインは必須（初期ID/パスワード 999999/999999。ログイン後すぐに変更してください）
rem   データの保存先を変える場合は KADOU_DATA_DIR を設定
cd /d %~dp0
set PORT=8000
python -m uvicorn app.main:app --host 0.0.0.0 --port %PORT%
