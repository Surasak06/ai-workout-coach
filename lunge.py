"""
วิเคราะห์ท่าลันจ์ (Lunge) จาก keypoints — แนะนำกล้องด้านข้าง (เห็นมุมเข่าชัด)

นับ: ข้างละ 1 ครั้ง (ก้าวลงแล้วกลับขึ้นยืน = 1 ครั้ง ไม่ว่าขาไหน)
สัญญาณที่ใช้นับ: มุมเข่าที่งอมากที่สุดของสองขา (ยืน ~170°, ลงสุด ~90°)
ผลแต่ละครั้ง
  ไม่นับ        : ลงไม่ลึก (shallow) — เข่าหน้างอไม่ถึงเกณฑ์ / ลำตัวโน้มไปหน้ามาก (lean)
  นับแต่หักคะแนน : เร็วไป (fast)
"""
from collections import deque

import config
from pushup import Rep, angle

C = lambda name, default: getattr(config, name, default)


class LungeAnalyzer:
    UP, GOING_DOWN, BOTTOM, GOING_UP = "UP", "GOING_DOWN", "BOTTOM", "GOING_UP"
    exercise = "lunge"
    LEVEL_ADJUST = {"beginner": 12, "intermediate": 0, "advanced": -6}    # มือใหม่: ไม่ต้องลงลึกเท่า

    def __init__(self, level=None):
        self.level = level
        self.reset()

    @property
    def bottom_angle(self):
        return C("LUNGE_BOTTOM_KNEE", 105) + self.LEVEL_ADJUST.get(self.level, 0)

    @property
    def depth_bottom(self):            # ตำแหน่งเส้น "ลึกพอ" บนแถบความลึกในเว็บ (0-100)
        return self._depth_of(self.bottom_angle)

    @staticmethod
    def _depth_of(knee):               # มุมเข่า -> % (100 = ยืน 170°, 0 = 70°)
        return max(0.0, min(100.0, (knee - 70) / 100 * 100))

    def reset(self):
        self.state = self.UP
        self.good_reps = self.attempts = 0
        self.reps = []
        self.side = None               # ขาหน้า (ขาที่งอมากกว่า)
        self.knee = self.lean = self.depth = None
        self.elbow = self.body = None
        self.visible, self.missing = False, ["hip", "knee", "ankle"]
        self._lean_hist = deque(maxlen=60)
        self._new_rep(0.0)

    def _new_rep(self, t):
        self._start_t = self._min_t = t
        self._min_knee = 180.0
        self._max_lean = 0.0
        self._lean_frames = self._frames = 0
        self._reached_bottom = False
        self._leg = None
        self._up_v = None

    @staticmethod
    def _ok(kps, n):
        return n in kps and kps[n][2] >= config.MIN_KEYPOINT_CONF

    def _smooth(self, old, new):
        if old is None:
            return new
        a = config.ANGLE_SMOOTHING
        return a * new + (1 - a) * old

    def update(self, kps, t):
        events = []
        knees = {}
        for s in ("left", "right"):
            if all(self._ok(kps, f"{s}_{j}") for j in ("hip", "knee", "ankle")):
                knees[s] = angle(kps[f"{s}_hip"][:2], kps[f"{s}_knee"][:2], kps[f"{s}_ankle"][:2])
        self.visible = bool(knees)
        self.missing = [] if knees else ["hip", "knee", "ankle"]
        if not knees:
            return events
        front = min(knees, key=knees.get)
        self.side = front
        self.knee = self._smooth(self.knee, knees[front])
        self.depth = self._depth_of(self.knee)
        sh, hp = f"{front}_shoulder", f"{front}_hip"
        if self._ok(kps, sh):
            dx, dy = kps[sh][0] - kps[hp][0], kps[hp][1] - kps[sh][1]
            if dy > 0:
                import math
                self.lean = self._smooth(self.lean, abs(math.degrees(math.atan2(dx, dy))))
                self.body = 180 - self.lean

        v, up, start, bottom = self.knee, C("LUNGE_UP_KNEE", 150), C("LUNGE_START_KNEE", 140), self.bottom_angle
        # ตอนกลับขึ้น ดูเข่าของ "ขาที่ทำงาน" (ขาหน้าตอนลงสุด) — ขาหลังอาจงอค้างนิด ๆ ในลันจ์แบบยืนแยกขา
        if self.state in (self.BOTTOM, self.GOING_UP) and self._leg in knees:
            self._up_v = self._smooth(self._up_v, knees[self._leg])
        else:
            self._up_v = v
        if self.state != self.UP:
            if v < self._min_knee:
                self._min_knee, self._min_t = v, t
            if self.lean is not None:
                self._frames += 1
                self._max_lean = max(self._max_lean, self.lean)
                if self.lean > C("LUNGE_MAX_LEAN", 35):
                    self._lean_frames += 1

        if self.state == self.UP:
            if v < start:
                self.state = self.GOING_DOWN
                self._new_rep(t)
                self._min_knee, self._leg = v, front
        elif self.state == self.GOING_DOWN:
            if v <= bottom:
                self.state, self._reached_bottom, self._leg = self.BOTTOM, True, front
            elif v > up:
                if t - self._start_t < config.MIN_PARTIAL_TIME:
                    self.state = self.UP
                else:
                    events += self._finish(t)
        elif self.state == self.BOTTOM:
            if self._up_v > bottom + 12:
                self.state = self.GOING_UP
        elif self.state == self.GOING_UP:
            if self._up_v >= up:
                events += self._finish(t)
            elif self._up_v <= bottom:
                self.state = self.BOTTOM
        return events

    def _finish(self, t):
        self.attempts += 1
        down, upt = max(0.0, self._min_t - self._start_t), max(0.0, t - self._min_t)
        issues = []
        if not self._reached_bottom:
            issues.append("shallow")
        if self._lean_frames >= 3 and self._lean_frames >= 0.4 * max(self._frames, 1):
            issues.append("lean")
        if down + upt < C("LUNGE_MIN_REP_TIME", 1.2):
            issues.append("fast")
        valid = "shallow" not in issues and "lean" not in issues
        if valid:
            self.good_reps += 1
        rep = Rep(self.attempts, valid, 0.0, round(180 - self._max_lean, 1), round(down, 2), round(upt, 2), issues,
                  round(self._depth_of(self._min_knee), 1))
        rep.t0, rep.t1 = self._start_t, t
        clamp = lambda x: int(max(0, min(100, round(x))))
        b = self.bottom_angle
        d = self._min_knee
        depth = 100 if d <= b - 15 else 100 - 30 * (d - (b - 15)) / 15 if d <= b else 60 - 2 * (d - b)
        body = 100 - max(0.0, self._max_lean - 15) * 3
        tut = rep.tut
        tempo = 100 if 1.5 <= tut <= 4 else (100 - (1.5 - tut) * 40 if tut < 1.5 else 100 - (tut - 4) * 10)
        rep.depth_score, rep.body_score, rep.tempo_score = clamp(depth), clamp(body), clamp(max(tempo, 40))
        total = 0.45 * rep.depth_score + 0.3 * rep.body_score + 0.25 * rep.tempo_score
        rep.score = clamp(total if valid else min(total, 59))
        self.reps.append(rep)
        self.state = self.UP
        return [("rep", rep)]

    def metrics(self):
        r = lambda v: round(v, 1) if v is not None else None
        return {"view": "side", "knee": r(self.knee), "lean": r(self.lean), "front_leg": self.side,
                "visible": self.visible, "missing": self.missing}

    def summary(self):
        good = [r for r in self.reps if r.valid]
        return {"good_reps": self.good_reps, "attempts": self.attempts, "bad_reps": self.attempts - self.good_reps,
                "avg_tut": round(sum(r.tut for r in good) / len(good), 2) if good else 0.0,
                "total_tut": round(sum(r.tut for r in good), 2),
                "shallow": sum("shallow" in r.issues for r in self.reps), "body": sum("lean" in r.issues for r in self.reps),
                "fast": sum("fast" in r.issues for r in self.reps),
                "form_score": round(sum(r.score for r in self.reps) / len(self.reps)) if self.reps else 0}
