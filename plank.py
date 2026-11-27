"""
วิเคราะห์ท่าแพลงก์ (Plank) จาก keypoints — กล้องด้านข้างระดับพื้น

นับเป็นวินาที: ทุก 1 วินาทีที่ค้างท่าถูก = 1 "ครั้ง" ที่ถูกต้อง (good_reps = จำนวนวินาที)
ท่าถูก: ลำตัวแนวนอน (ไหล่-เท้าเอียงไม่เกิน ~40°) และ ไหล่-สะโพก-ข้อเท้า เกือบเป็นเส้นตรง
ท่าผิด: สะโพกตก (sag) / สะโพกโก่งสูง (pike) ติดต่อกันเกิน 0.7 วิ -> บันทึก 1 ครั้งที่ไม่นับ แล้วหยุดนับเวลาจนกว่าจะแก้ท่า
ลุกขึ้น/คุกเข่า (ลำตัวไม่แนวนอน) = หยุดนับเวลาเฉย ๆ
"""
import math

import config
from pushup import Rep, angle

C = lambda name, default: getattr(config, name, default)


class PlankAnalyzer:
    OFF, HOLD, BAD = "OFF", "HOLD", "BAD"
    exercise = "plank"
    unit = "sec"
    LEVEL_STRAIGHT = {"beginner": 150, "intermediate": 158, "advanced": 165}

    def __init__(self, level=None):
        self.level = level
        self.reset()

    @property
    def straight_min(self):
        return C("PLANK_STRAIGHT_MIN", self.LEVEL_STRAIGHT.get(self.level, 158))

    def reset(self):
        self.state = self.OFF
        self.good_reps = self.attempts = 0
        self.reps = []
        self.side = None
        self.body = self.elbow = self.depth = self.knee = self.lean = None
        self.depth_bottom = 0
        self.tilt = None
        self.fault = None
        self.visible, self.missing = False, ["shoulder", "hip", "ankle"]
        self._hold = 0.0              # เวลาที่ค้างท่าถูกสะสมของวินาทีปัจจุบัน
        self._last_t = None
        self._bad_since = None
        self._bad_logged = False
        self._sec_min_body = 180.0

    @staticmethod
    def _ok(kps, n):
        return n in kps and kps[n][2] >= config.MIN_KEYPOINT_CONF

    def _pick(self, kps):
        best = None
        for s in ("left", "right"):
            foot = f"{s}_ankle" if self._ok(kps, f"{s}_ankle") else f"{s}_knee" if self._ok(kps, f"{s}_knee") else None
            if foot and self._ok(kps, f"{s}_shoulder") and self._ok(kps, f"{s}_hip"):
                conf = kps[f"{s}_shoulder"][2] + kps[f"{s}_hip"][2] + kps[foot][2]
                if best is None or conf > best[0]:
                    best = (conf, s, foot)
        return best

    def update(self, kps, t):
        events = []
        dt = 0.0 if self._last_t is None else min(0.5, max(0.0, t - self._last_t))
        self._last_t = t
        pick = self._pick(kps)
        self.visible = pick is not None
        self.missing = [] if pick else ["shoulder", "hip", "ankle"]
        if not pick:
            self.state = self.OFF
            return events
        _, s, foot = pick
        S, H, F = kps[f"{s}_shoulder"][:2], kps[f"{s}_hip"][:2], kps[foot][:2]
        self.side = s
        body = angle(S, H, F)
        self.body = body if self.body is None else 0.6 * body + 0.4 * self.body
        self.tilt = abs(math.degrees(math.atan2(abs(F[1] - S[1]), abs(F[0] - S[0]) + 1e-6)))
        # สะโพกอยู่ใต้หรือเหนือเส้นไหล่-เท้า (ภาพ y ลงล่าง): บวก = ตก, ลบ = โก่ง
        cross = (F[0] - S[0]) * (H[1] - S[1]) - (F[1] - S[1]) * (H[0] - S[0])
        below = cross * (1 if F[0] > S[0] else -1) > 0
        if self.tilt > C("PLANK_MAX_TILT", 40):
            self.state, self._bad_since = self.OFF, None              # ยืน/นั่ง/คุกเข่า: ไม่นับ
            return events
        if self.body >= self.straight_min:
            self.state, self._bad_since, self._bad_logged, self.fault = self.HOLD, None, False, None
            self._hold += dt
            self._sec_min_body = min(self._sec_min_body, self.body)
            while self._hold >= 1.0:                                   # ครบ 1 วินาที = นับ 1
                self._hold -= 1.0
                events += self._second(t)
        else:
            self.fault = "sag" if below else "pike"
            if self._bad_since is None:
                self._bad_since = t
            if t - self._bad_since >= C("PLANK_BAD_GRACE", 0.7):
                self.state = self.BAD
                if not self._bad_logged:                               # เตือน/บันทึกครั้งเดียวต่อการเสียท่า
                    self._bad_logged = True
                    self.attempts += 1
                    rep = Rep(self.attempts, False, 0.0, round(self.body, 1), 0.0, 0.0, [self.fault])
                    rep.t0 = rep.t1 = t
                    rep.body_score = rep.score = int(max(0, min(59, 100 - (self.straight_min - self.body) * 4)))
                    rep.depth_score, rep.tempo_score = 100, 100
                    self.reps.append(rep)
                    events.append(("rep", rep))
        return events

    def _second(self, t):
        self.attempts += 1
        self.good_reps += 1
        b = self._sec_min_body
        rep = Rep(self.attempts, True, 0.0, round(b, 1), 1.0, 0.0, [])
        rep.t0, rep.t1 = t - 1.0, t
        score = int(max(60, min(100, 100 - max(0.0, 172 - b) * 2.5)))
        rep.depth_score, rep.body_score, rep.tempo_score, rep.score = 100, score, 100, score
        self.reps.append(rep)
        self._sec_min_body = 180.0
        return [("rep", rep)]

    def metrics(self):
        r = lambda v: round(v, 1) if v is not None else None
        return {"view": "side", "body": r(self.body), "tilt": r(self.tilt), "fault": self.fault, "hold": self.state == self.HOLD,
                "visible": self.visible, "missing": self.missing, "unit": "sec"}

    def summary(self):
        good = [r for r in self.reps if r.valid]
        return {"good_reps": self.good_reps, "attempts": self.attempts, "bad_reps": self.attempts - self.good_reps,
                "avg_tut": 0.0, "total_tut": float(self.good_reps),
                "shallow": 0, "body": sum(not r.valid for r in self.reps), "fast": 0,
                "form_score": round(sum(r.score for r in good) / len(good)) if good else 0}
