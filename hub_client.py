"""
ส่งผลการฝึกจาก CiRA ไปที่ HUB (hub/server.py) ผ่าน Wi-Fi

- ส่งผลแต่ละเซตเมื่อจบเซต -> HUB คิดคะแนน XP / แรงค์ แล้วตอบกลับมา
- ส่งสถานะสด (จำนวนครั้งตอนนี้) ทุก ~1 วินาที ให้มือถือเห็นแบบเรียลไทม์
- ทุกอย่างส่งใน thread แยก ภาพไม่กระตุก
- ถ้าส่งไม่ได้ (Wi-Fi หลุด / HUB ปิด) เก็บไว้ใน data/pending_uploads.jsonl แล้วส่งใหม่รอบหน้า
ใช้แค่ไลบรารีมาตรฐานของ Python ไม่ต้องติดตั้งเพิ่ม
"""

import json
import os
import threading
import time
import urllib.request
import uuid


class HubClient:
    def __init__(self, url, player, data_dir):
        self.url = url.rstrip("/")
        self.player = player
        self.pending_path = os.path.join(data_dir, "pending_uploads.jsonl")
        self.last_result = None        # ผลตอบกลับล่าสุดจาก HUB (XP ที่ได้, แรงค์)
        self.last_result_time = 0.0
        self.status = "ยังไม่ได้ส่ง"
        self._last_live = 0.0
        self._lock = threading.Lock()

    # ---------- ภายใน ----------
    def _post(self, path, payload, timeout=3.0):
        req = urllib.request.Request(
            self.url + path, data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
            headers={"Content-Type": "application/json"}, method="POST")
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.loads(r.read().decode("utf-8"))

    def _save_pending(self, item):
        with self._lock, open(self.pending_path, "a", encoding="utf-8") as f:
            f.write(json.dumps(item, ensure_ascii=False) + "\n")

    def _take_pending(self):
        with self._lock:
            if not os.path.exists(self.pending_path):
                return []
            with open(self.pending_path, encoding="utf-8") as f:
                items = [json.loads(line) for line in f if line.strip()]
            os.remove(self.pending_path)
            return items

    def _send_sessions(self, items):
        failed = []
        for item in items:
            try:
                res = self._post("/api/session", item)
                self.last_result, self.last_result_time = res, time.time()
                self.status = "ส่งแล้ว"
                print(f"[hub] ส่งเซตแล้ว: +{res.get('xp')} XP รวม {res.get('total_xp')} ({res.get('rank', {}).get('name')})")
            except Exception as e:
                failed.append(item)
                self.status = f"ส่งไม่ได้ ({e.__class__.__name__}) เก็บไว้ส่งทีหลัง"
                print("[hub] ส่งไม่ได้ เก็บไว้ส่งทีหลัง:", e)
        for item in failed:
            self._save_pending(item)

    # ---------- เรียกจาก cira_app ----------
    def send_session(self, row, reps, exercise="pushup"):
        item = {
            "client_id": uuid.uuid4().hex,          # กันนับซ้ำถ้าส่งซ้ำ
            "player": self.player, "exercise": exercise,
            "good_reps": row.get("good_reps", 0), "attempts": row.get("attempts", 0),
            "bad_reps": row.get("bad_reps", 0), "avg_tut": row.get("avg_tut", 0),
            "shallow": row.get("shallow", 0), "body": row.get("body", 0), "fast": row.get("fast", 0),
            "form_score": row.get("form_score", 0),
            "reps": [{"tut": round(r.tut, 2), "valid": bool(r.valid), "issues": list(r.issues), "score": r.score}
                     for r in reps],
            "local_time": f"{row.get('date', '')} {row.get('time', '')}",
        }
        items = self._take_pending() + [item]
        threading.Thread(target=self._send_sessions, args=(items,), daemon=True).start()

    def send_live(self, status, every=1.0):
        now = time.time()
        if now - self._last_live < every:
            return
        self._last_live = now
        payload = dict(status, player=self.player)

        def go():
            try:
                self._post("/api/live", payload, timeout=1.0)
            except Exception:
                pass                                 # สถานะสดหายไปบ้างไม่เป็นไร
        threading.Thread(target=go, daemon=True).start()


class WebWorker:
    """
    โหมดเว็บ: CiRA เป็นหลังบ้าน
    ไปขอภาพที่เว็บส่งมาจาก HUB -> ประมวลผล -> ส่งผลกลับให้เว็บแสดง
    """

    def __init__(self, url):
        self.url = url.rstrip("/")
        self.last_error = None

    def fetch(self, timeout=0.5):
        """คืนค่า None (ไม่มีงาน), ("cmd", dict) หรือ ("frame", meta, jpeg_bytes)"""
        with urllib.request.urlopen(self.url + "/api/work", timeout=timeout) as r:
            if r.status == 204:
                return None
            body = r.read()
            if r.headers.get("Content-Type", "").startswith("application/json"):
                return ("cmd", json.loads(body.decode("utf-8")))
            h = r.headers
            meta = {
                "stream": h.get("X-Stream"),
                "player": bytes.fromhex(h.get("X-Player", "")).decode("utf-8", "replace"),
                "exercise": h.get("X-Exercise", "pushup"),
                "seq": int(h.get("X-Seq", 0)),
                "t": float(h.get("X-Capture-Ms", 0)) / 1000.0,
                "local": h.get("X-Local") == "1",
                "level": h.get("X-Level", "beginner"),
            }
            return ("frame", meta, body)

    def _post(self, path, payload, timeout):
        req = urllib.request.Request(
            self.url + path, data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
            headers={"Content-Type": "application/json"}, method="POST")
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.loads(r.read().decode("utf-8"))

    def result(self, stream, data):
        try:
            self._post(f"/api/work/{stream}/result", data, 0.5)
        except Exception as e:
            self.last_error = str(e)

    def finish(self, stream, data):
        try:
            return self._post(f"/api/work/{stream}/finish", data, 3.0)
        except Exception as e:
            self.last_error = str(e)
            return None
