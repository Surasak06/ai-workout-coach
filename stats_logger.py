"""
บันทึกสถิติการฝึก
  - data/reps.csv     : ข้อมูลรายครั้ง (มุม, TUT, ปัญหาที่พบ)
  - data/sessions.csv : สรุปรายเซต (ใช้ทำ Dashboard)
  - Google Sheets     : ส่งสรุปรายเซตขึ้นชีต (ถ้าเปิดใน config)
"""

import csv
import os
from datetime import datetime

import config

REP_FIELDS = ["session_id", "rep", "valid", "min_elbow", "min_body",
              "down_time", "up_time", "tut", "issues", "score"]
SESSION_FIELDS = ["session_id", "date", "time", "exercise", "good_reps", "attempts",
                  "bad_reps", "avg_tut", "total_tut", "shallow", "body", "fast", "duration_s", "form_score"]


def _append(path, fields, row):
    new = not os.path.exists(path)
    with open(path, "a", newline="", encoding="utf-8-sig") as f:  # utf-8-sig ให้ Excel อ่านไทยได้
        w = csv.DictWriter(f, fieldnames=fields, extrasaction="ignore")
        if new:
            w.writeheader()
        w.writerow(row)


class StatsLogger:
    def __init__(self, exercise="pushup"):
        self.data_dir = config.DATA_DIR      # จำโฟลเดอร์ไว้ตั้งแต่ตอนสร้าง (กันค่า config ถูกโหลดใหม่ระหว่างทาง)
        os.makedirs(self.data_dir, exist_ok=True)
        self.exercise = exercise
        self.started = datetime.now()
        self.session_id = self.started.strftime("%Y%m%d-%H%M%S")

    def log_rep(self, rep):
        _append(os.path.join(self.data_dir, "reps.csv"), REP_FIELDS, {
            "session_id": self.session_id, "rep": rep.number, "valid": int(rep.valid),
            "min_elbow": rep.min_elbow, "min_body": rep.min_body,
            "down_time": rep.down_time, "up_time": rep.up_time,
            "tut": round(rep.tut, 2), "issues": "|".join(rep.issues), "score": rep.score,
        })

    def log_session(self, summary):
        row = {
            "session_id": self.session_id,
            "date": self.started.strftime("%Y-%m-%d"),
            "time": self.started.strftime("%H:%M"),
            "exercise": self.exercise,
            "duration_s": round((datetime.now() - self.started).total_seconds()),
            **summary,
        }
        _append(os.path.join(self.data_dir, "sessions.csv"), SESSION_FIELDS, row)
        if config.GOOGLE_SHEETS_ENABLED:
            self._to_google_sheets(row)
        return row

    def _to_google_sheets(self, row):
        """
        ต้องทำก่อนครั้งเดียว:
          1. สร้าง Service Account ใน Google Cloud แล้วดาวน์โหลดไฟล์ JSON มาวางชื่อ service_account.json
          2. เปิด Google Sheets API และ Google Drive API
          3. สร้างชีตชื่อตาม GOOGLE_SHEET_NAME แล้วแชร์ให้อีเมลของ Service Account (สิทธิ์ Editor)
        """
        try:
            import gspread
            gc = gspread.service_account(filename=config.GOOGLE_SERVICE_ACCOUNT_JSON)
            ws = gc.open(config.GOOGLE_SHEET_NAME).sheet1
            if not ws.row_values(1):
                ws.append_row(SESSION_FIELDS)
            ws.append_row([row[k] for k in SESSION_FIELDS])
            print("[log] ส่งขึ้น Google Sheets แล้ว")
        except Exception as e:
            print("[log] ส่ง Google Sheets ไม่สำเร็จ (ข้อมูลยังอยู่ใน CSV):", e)
