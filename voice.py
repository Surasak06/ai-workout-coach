"""
เสียงพูดแจ้งเตือนแบบ Hands-free (TTS) ทำงานใน thread แยก ไม่ให้ภาพกระตุก

ใช้ pyttsx3 ซึ่งทำงานออฟไลน์ ใช้เสียงที่ติดตั้งในเครื่อง
- Windows: ถ้าอยากได้เสียงภาษาไทย ต้องติดตั้ง Thai language pack (Speech) ใน Settings ก่อน
  ถ้าไม่มีเสียงไทย โปรแกรมจะพยายามอ่านด้วยเสียงอังกฤษ (ฟังไม่รู้เรื่อง) -> ตั้ง LANG = "en"
"""

import queue
import threading
import time

import config

LANG = "th"   # "th" หรือ "en"

MESSAGES = {
    "th": {
        "count": "{n}",
        "shallow": "ลงให้ลึกกว่านี้",
        "body": "เกร็งลำตัวให้ตรง",
        "fast": "ช้าลงหน่อย",
        "start": "พร้อมแล้ว เริ่มวิดพื้นได้เลย",
        "lost": "มองไม่เห็นตัว ขยับเข้ามาในกล้อง",
        "done": "จบเซต ทำได้ {n} ครั้ง",
    },
    "en": {
        "count": "{n}",
        "shallow": "Go lower",
        "body": "Keep your body straight",
        "fast": "Slow down",
        "start": "Ready. Start your push ups",
        "lost": "I can't see you",
        "done": "Set complete. {n} reps",
    },
}


class Voice:
    def __init__(self, enabled=config.VOICE_ENABLED):
        self.enabled = enabled
        self.q = queue.Queue()
        self._last_said = {}
        if enabled:
            threading.Thread(target=self._worker, daemon=True).start()

    def _worker(self):
        try:
            import pyttsx3
            engine = pyttsx3.init()
            engine.setProperty("rate", config.VOICE_RATE)
            if LANG == "th":
                for v in engine.getProperty("voices"):
                    if "thai" in v.name.lower() or "th" in str(getattr(v, "languages", "")).lower():
                        engine.setProperty("voice", v.id)
                        break
        except Exception as e:
            print("[voice] เปิดระบบเสียงไม่ได้:", e)
            self.enabled = False
            return
        while True:
            text = self.q.get()
            try:
                engine.say(text)
                engine.runAndWait()
            except Exception as e:
                print("[voice] พูดไม่สำเร็จ:", e)

    def say(self, key, **kw):
        text = MESSAGES[LANG][key].format(**kw)
        print("[voice]", text)
        if not self.enabled:
            return
        now = time.time()
        if key != "count" and now - self._last_said.get(key, 0) < config.VOICE_COOLDOWN:
            return  # กันพูดเตือนซ้ำรัว ๆ
        self._last_said[key] = now
        # ถ้าคิวค้าง (พูดไม่ทัน) ทิ้งของเก่า เอาข้อความล่าสุด
        while self.q.qsize() > 1:
            try:
                self.q.get_nowait()
            except queue.Empty:
                break
        self.q.put(text)
