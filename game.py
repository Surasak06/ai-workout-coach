"""
เกม 3 เลน: ในแต่ละเฟรมบอกว่าผู้เล่น "ยืนเลนไหน" และ "ทำท่าอะไรอยู่"  (กล้องหันหน้าเข้าหาผู้เล่น)
HUB เป็นคนเอาผลนี้ไปตัดสินว่าโดนการ์ดที่ตกลงมาหรือไม่ ; การคำนวณท่าทั้งหมดอยู่ที่นี่ (ใน CiRA)

ท่าที่รู้จัก
  up    ชูมือ       : ข้อมือทั้งสองข้างสูงกว่าหัว (จมูก)
  t     กางแขน      : ข้อมือสองข้างอยู่ระดับไหล่ และกางออกกว้าง (เกิน 2 เท่าของความกว้างไหล่)
  hips  มือเท้าเอว  : ข้อมือแต่ละข้างอยู่ใกล้สะโพกข้างเดียวกัน และศอกกางออกนอกลำตัว
  squat ย่อ        : สะโพกลงมาใกล้ระดับเข่า (วัดเหมือน squat.py มุมหน้า)
  stand ยืนเฉย ๆ

เลน (ตามภาพจากกล้อง ยังไม่กลับซ้ายขวา):  L = ซ้ายของภาพ, C = กลาง, R = ขวาของภาพ
"""

import math
from collections import deque

import config

C = lambda name, default: getattr(config, name, default)


class GameAnalyzer:
    exercise = "game"
    UP = "UP"

    def __init__(self, level=None):
        self.level = level
        self.frame_size = (640, 480)      # cira_app ใส่ขนาดภาพจริงให้ก่อนเรียก update
        self.reset()

    def reset(self):
        self.state = self.UP
        self.good_reps = self.attempts = 0
        self.reps = []
        self.side = None
        self.depth = self.knee = self.lean = self.elbow = self.body = None
        self.pose, self.lane, self.x = "none", None, None
        self.visible = False
        self._thigh = deque(maxlen=200)
        self._pose_hist = deque(maxlen=3)

    @property
    def depth_bottom(self):
        return 45

    @staticmethod
    def _ok(kps, n):
        return n in kps and kps[n][2] >= config.MIN_KEYPOINT_CONF

    def _classify(self, kps):
        ok = lambda *ns: all(self._ok(kps, n) for n in ns)
        P = lambda n: kps[n][:2]
        if not ok("left_shoulder", "right_shoulder", "left_hip", "right_hip"):
            return "none"
        ls, rs, lh, rh = P("left_shoulder"), P("right_shoulder"), P("left_hip"), P("right_hip")
        sh_w = max(1.0, abs(ls[0] - rs[0]))
        torso = max(1.0, (math.dist(ls, lh) + math.dist(rs, rh)) / 2)
        sh_y = (ls[1] + rs[1]) / 2

        # ย่อ: สะโพกลงใกล้เข่า
        if ok("left_knee", "right_knee"):
            hip_y = (lh[1] + rh[1]) / 2
            knee_y = (P("left_knee")[1] + P("right_knee")[1]) / 2
            thigh = (math.dist(lh, P("left_knee")) + math.dist(rh, P("right_knee"))) / 2
            self._thigh.append(thigh)
            ref = sorted(self._thigh)[int(len(self._thigh) * 0.9) - 1] if len(self._thigh) >= 10 else max(self._thigh)
            self.depth = 100 * (knee_y - hip_y) / max(1.0, ref)
            if self.depth < C("GAME_SQUAT_DEPTH", 45):
                return "squat"
        if ok("left_wrist", "right_wrist"):
            lw, rw = P("left_wrist"), P("right_wrist")
            head_y = P("nose")[1] if self._ok(kps, "nose") else sh_y - 0.45 * torso
            # ชูมือ
            if lw[1] < head_y and rw[1] < head_y:
                return "up"
            # กางแขน
            if abs(lw[1] - sh_y) < 0.35 * torso and abs(rw[1] - sh_y) < 0.35 * torso and abs(lw[0] - rw[0]) > 2.0 * sh_w:
                return "t"
            # มือเท้าเอว (ต้องเห็นทั้งสองมือ)
            near_l = min(math.dist(lw, lh), math.dist(lw, rh)) < 0.45 * torso
            near_r = min(math.dist(rw, lh), math.dist(rw, rh)) < 0.45 * torso
            if near_l and near_r and ok("left_elbow", "right_elbow"):
                if abs(P("left_elbow")[0] - P("right_elbow")[0]) > 1.25 * sh_w:
                    return "hips"
        else:
            # เห็นแขนข้างเดียว (อีกข้างหลุดขอบภาพ / CiRA จับไม่ได้) -> ใช้ข้างที่เห็น
            head_y = P("nose")[1] if self._ok(kps, "nose") else sh_y - 0.45 * torso
            for sd in ("left", "right"):
                if ok(f"{sd}_wrist"):
                    wr, sh = P(f"{sd}_wrist"), P(f"{sd}_shoulder")
                    if wr[1] < head_y:
                        return "up"
                    if abs(wr[1] - sh_y) < 0.35 * torso and abs(wr[0] - sh[0]) > 1.0 * sh_w:
                        return "t"
        return "stand"

    def update(self, kps, t):
        w, _ = self.frame_size
        if not kps:
            self.visible, self.pose, self.lane = False, "none", None
            return []
        pose = self._classify(kps)
        self._pose_hist.append(pose)
        # กันกระพริบ: ใช้ท่าที่เจอ 2 ใน 3 เฟรมล่าสุด
        self.pose = max(set(self._pose_hist), key=list(self._pose_hist).count) if len(self._pose_hist) >= 2 else pose
        x = self._center_x(kps)
        if x is not None:
            self.x = x / w
            # เลนกลางแคบ: ขยับตัวออกจากกลางนิดเดียว (เลยเส้นแบ่ง) ก็เปลี่ยนเลนทันที ไม่ต้องเดินไปไกล
            # ยืนใกล้กลางภาพเสมอ -> กางแขนแล้วกล้องยังเห็นแขนทั้งสองข้าง
            lo, hi = C("GAME_LANE_SPLIT", (0.44, 0.56))
            m = 0.012 if self.lane else 0.0                    # กันกระพริบตอนยืนคร่อมเส้นพอดี (น้อยมาก)
            lane = "L" if self.x < lo else "R" if self.x > hi else "C"
            if lane != self.lane and self.lane is not None and min(abs(self.x - lo), abs(self.x - hi)) < m:
                lane = self.lane
            self.lane = lane
        self.visible = self.pose != "none"
        return []

    def _center_x(self, kps):
        """กึ่งกลางลำตัว: สะโพก -> ไหล่ -> จมูก (ใช้อันที่เห็น)"""
        for a, b in (("left_hip", "right_hip"), ("left_shoulder", "right_shoulder")):
            if self._ok(kps, a) and self._ok(kps, b):
                return (kps[a][0] + kps[b][0]) / 2
        return kps["nose"][0] if self._ok(kps, "nose") else None

    def metrics(self):
        return {"pose": self.pose, "lane": self.lane, "x": round(self.x, 3) if self.x is not None else None,
                "visible": self.visible, "view": "front", "missing": []}

    def summary(self):
        return {"good_reps": 0, "attempts": 0, "bad_reps": 0, "avg_tut": 0, "total_tut": 0,
                "shallow": 0, "body": 0, "fast": 0, "form_score": 0}
