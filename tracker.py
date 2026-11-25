"""
คำนวณมุม Pan/Tilt ให้กล้องหันตามผู้ฝึก แล้วส่งไป ESP32 ทาง USB Serial เป็น JSON

ส่งทีละบรรทัด เช่น  {"pan": 92, "tilt": 85}\n
ใช้ตัวควบคุมแบบ P (proportional) อย่างง่าย + deadband กันกล้องสั่น
"""

import json
import time

import config

TORSO = ("left_shoulder", "right_shoulder", "left_hip", "right_hip", "nose")


class PanTiltTracker:
    def __init__(self, port=config.SERIAL_PORT):
        self.pan = float(config.PAN_CENTER)
        self.tilt = float(config.TILT_CENTER)
        self._last_send = 0.0
        self._last_sent = None
        self.ser = None
        if port:
            try:
                import serial
                self.ser = serial.Serial(port, config.SERIAL_BAUD, timeout=0)
                time.sleep(2)  # ESP32 รีเซ็ตตัวเองตอนเปิดพอร์ต รอให้บูตเสร็จ
                print(f"[tracker] เชื่อม ESP32 ที่ {port} แล้ว")
            except Exception as e:
                print(f"[tracker] เปิดพอร์ต {port} ไม่ได้ ({e}) -> ใช้โหมดจำลอง")
        self._send(force=True)

    def target_point(self, kps):
        pts = [kps[n] for n in TORSO if n in kps and kps[n][2] >= config.MIN_KEYPOINT_CONF]
        if not pts:
            return None
        return (sum(p[0] for p in pts) / len(pts), sum(p[1] for p in pts) / len(pts))

    def update(self, kps, frame_w, frame_h):
        target = self.target_point(kps)
        if target is None:
            return None  # ไม่เห็นคน กล้องค้างไว้ที่เดิม

        # error เป็นสัดส่วน -1..1 จากกลางภาพ
        ex = (target[0] - frame_w / 2) / (frame_w / 2)
        ey = (target[1] - frame_h / 2) / (frame_h / 2)

        if abs(ex) > config.TRACK_DEADBAND:
            step = config.TRACK_GAIN * ex * (-1 if config.INVERT_PAN else 1)
            self.pan = min(config.PAN_MAX, max(config.PAN_MIN, self.pan - step))
        if abs(ey) > config.TRACK_DEADBAND:
            step = config.TRACK_GAIN * ey * (-1 if config.INVERT_TILT else 1)
            self.tilt = min(config.TILT_MAX, max(config.TILT_MIN, self.tilt + step))

        self._send()
        return target

    def center(self):
        self.pan, self.tilt = float(config.PAN_CENTER), float(config.TILT_CENTER)
        self._send(force=True)

    def _send(self, force=False):
        now = time.time()
        cmd = (round(self.pan), round(self.tilt))
        if not force:
            if now - self._last_send < 1.0 / config.SERIAL_SEND_HZ or cmd == self._last_sent:
                return
        self._last_send, self._last_sent = now, cmd
        if self.ser:
            line = json.dumps({"pan": cmd[0], "tilt": cmd[1]}) + "\n"
            try:
                self.ser.write(line.encode())
            except Exception as e:
                print("[tracker] ส่งข้อมูลไม่สำเร็จ:", e)

    def close(self):
        if self.ser:
            self.center()
            self.ser.close()
