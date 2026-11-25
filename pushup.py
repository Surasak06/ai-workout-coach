"""
หัวใจของระบบ: วิเคราะห์ท่าวิดพื้นจาก keypoints

ทำ 4 อย่าง
  1. นับครั้ง (state machine: บน -> ลง -> ล่าง -> ขึ้น -> บน = 1 ครั้ง)
  2. แยกครั้งที่ "โกงท่า" ออก: ลงไม่ลึก / ลำตัวไม่ตรง (สะโพกตกหรือโก่ง) / เร็วเกิน
  3. วัด Time Under Tension (TUT) ของแต่ละครั้ง แยกช่วงลง (eccentric) และช่วงขึ้น (concentric)
  4. ส่ง event ออกไปให้ระบบเสียง/บันทึกสถิติใช้
"""

import math
from collections import deque
from dataclasses import dataclass, field

import config


def angle(a, b, c):
    """มุมที่จุด b (องศา) ระหว่างเส้น b->a กับ b->c"""
    ang = math.degrees(
        math.atan2(c[1] - b[1], c[0] - b[0]) - math.atan2(a[1] - b[1], a[0] - b[0])
    )
    ang = abs(ang)
    return 360 - ang if ang > 180 else ang


@dataclass
class Rep:
    number: int            # ลำดับครั้งที่พยายามทำ
    valid: bool            # นับเป็นครั้งที่ถูกต้องไหม
    min_elbow: float       # มุมศอกต่ำสุด (ยิ่งน้อย = ลงลึก)
    min_body: float        # มุมลำตัวต่ำสุด (ใกล้ 180 = ตรง)
    down_time: float       # วินาทีช่วงลง
    up_time: float         # วินาทีช่วงขึ้น
    issues: list = field(default_factory=list)
    min_depth: float = None  # ความสูงไหล่ต่ำสุด เทียบท่าบน (100 = ท่าบน, 0 = ไหล่เท่าระดับมือ)
    score: int = 0           # คะแนนท่าครั้งนี้ 0-100
    depth_score: int = 0     # คะแนนย่อย: ความลึก
    body_score: int = 0      # คะแนนย่อย: ลำตัวตรง
    tempo_score: int = 0     # คะแนนย่อย: จังหวะ (TUT)
    t0: float = 0.0          # เวลาเริ่มลง (วินาที) ใช้คำนวณความเร็วต่อนาที
    t1: float = 0.0          # เวลาจบครั้งนี้

    @property
    def tut(self):
        return self.down_time + self.up_time


class PushUpAnalyzer:
    UP, GOING_DOWN, BOTTOM, GOING_UP = "UP", "GOING_DOWN", "BOTTOM", "GOING_UP"
    exercise = "pushup"

    LEVEL_ADJUST = {"beginner": 5, "intermediate": 0, "advanced": -5}   # มือใหม่: ลงไม่ต้องลึกเท่า

    @property
    def depth_bottom(self):
        return config.DEPTH_BOTTOM + self.LEVEL_ADJUST.get(self.level, 0)

    def metrics(self):
        r = lambda v: round(v, 1) if v is not None else None
        return {"elbow": r(self.elbow), "body": r(self.body), "visible": self.side is not None, "view": "side", "missing": []}

    def __init__(self, level=None):
        self.level = level
        self.reset()

    def reset(self):
        self.state = self.UP
        self.good_reps = 0
        self.attempts = 0
        self.reps = []
        self.elbow = None
        self.body = None
        self.depth = None            # ความสูงไหล่ (% ของท่าบน) ใช้นับครั้งเมื่อ COUNT_BY = "shoulder"
        self.side = None
        self._depth_hist = deque(maxlen=300)
        self._start_t = self._min_t = None
        self._min_v = 999.0
        self._min_depth = 999.0
        self._min_elbow = 180.0
        self._min_body = 180.0
        self._reached_bottom = False
        self._body_frames = self._body_bad_frames = 0

    # ---------- เลือกข้างที่หันเข้ากล้อง ----------
    def _arm_score(self, kps, side):
        """
        คะแนนของแขนข้างนี้ = ความยาวแขนในภาพ (ไหล่->ศอก->ข้อมือ, พิกเซล)
        แขนข้างที่ถูกลำตัวบัง โมเดลมักเดาจุดให้กองรวมกัน -> แขนดูสั้นผิดปกติ -> คะแนนต่ำ
        คืน None ถ้าจุดไม่ครบหรือความมั่นใจต่ำ
        """
        names = [f"{side}_{n}" for n in ("shoulder", "elbow", "wrist", "hip", "ankle")]
        if not all(n in kps for n in names):
            return None
        if min(kps[n][2] for n in names[:3]) < config.MIN_KEYPOINT_CONF:
            return None
        s, e, w = (kps[n][:2] for n in names[:3])
        return math.dist(s, e) + math.dist(e, w)

    def _pick_side(self, kps):
        scores = {sd: self._arm_score(kps, sd) for sd in ("left", "right")}
        valid = {sd: sc for sd, sc in scores.items() if sc is not None}
        if not valid:
            return None
        best = max(valid, key=valid.get)
        cur = self.side
        if cur in valid:
            # ระหว่างทำ 1 ครั้ง ห้ามสลับข้าง (กันมุมกระโดด)
            if self.state != self.UP:
                return cur
            # ตอนอยู่ท่าบน สลับเมื่ออีกข้างชัดกว่ามาก
            if valid[best] < valid[cur] * config.SIDE_SWITCH_RATIO:
                return cur
        return best

    def _smooth(self, old, new):
        if old is None:
            return new
        a = config.ANGLE_SMOOTHING
        return a * new + (1 - a) * old

    def _shoulder_height(self, kps):
        """
        ไหล่สูงกว่ามือแค่ไหน หารด้วยความยาวลำตัว (ไหล่-สะโพก) ใช้ไหล่/มือทั้งสองข้างเฉลี่ยกัน
        ไม่ต้องพึ่งจุดข้อศอก จึงไม่เพี้ยนเวลาแขนถูกบัง
        """
        ok = lambda n: n in kps and kps[n][2] >= config.MIN_KEYPOINT_CONF
        sides = ("left", "right")
        sh = [kps[f"{sd}_shoulder"][1] for sd in sides if ok(f"{sd}_shoulder")]
        wr = [kps[f"{sd}_wrist"][1] for sd in sides if ok(f"{sd}_wrist")]
        torso = [math.dist(kps[f"{sd}_shoulder"][:2], kps[f"{sd}_hip"][:2])
                 for sd in sides if ok(f"{sd}_shoulder") and ok(f"{sd}_hip")]
        if not (sh and wr and torso) or max(torso) < 1:
            return None
        return (sum(wr) / len(wr) - sum(sh) / len(sh)) / (sum(torso) / len(torso))

    def _depth_percent(self, h):
        """เทียบกับความสูงไหล่ตอนท่าบน (ค่าสูงสุดช่วงหลัง ๆ) -> 100 = ท่าบน, 0 = ไหล่ระดับมือ"""
        self._depth_hist.append(h)
        hist = sorted(self._depth_hist)
        top = hist[int(len(hist) * 0.9) - 1] if len(hist) >= 10 else max(hist)
        if top <= 0.05:
            return None
        return max(0.0, 100.0 * h / top)

    # ---------- เรียกทุกเฟรม ----------
    def update(self, kps, t):
        """
        kps : dict ของ keypoints (ดู pose_source.py)
        t   : เวลาปัจจุบันเป็นวินาที
        คืนค่า list ของ event เช่น [("rep", Rep), ("warn", "ข้อความ")]
        """
        events = []
        side = self._pick_side(kps)
        if side is not None:
            self.side = side
            p = lambda n: kps[f"{side}_{n}"][:2]
            self.elbow = self._smooth(self.elbow, angle(p("shoulder"), p("elbow"), p("wrist")))
            if min(kps[f"{side}_ankle"][2], kps[f"{side}_hip"][2]) >= config.MIN_KEYPOINT_CONF:
                self.body = self._smooth(self.body, angle(p("shoulder"), p("hip"), p("ankle")))

        h = self._shoulder_height(kps)
        if h is not None:
            d = self._depth_percent(h)
            if d is not None:
                self.depth = self._smooth(self.depth, d)

        # ---- เลือกสัญญาณที่ใช้นับ (ค่าน้อย = ลงลึก ทั้งสองแบบ) ----
        if config.COUNT_BY == "shoulder":
            if h is None or self.depth is None:
                return events
            v = self.depth
            up, start, bottom = config.DEPTH_UP, config.DEPTH_START_DOWN, self.depth_bottom
        else:
            if side is None:
                return events
            v = self.elbow
            up, start, bottom = config.ELBOW_UP_ANGLE, config.ELBOW_START_DOWN, config.ELBOW_DOWN_ANGLE

        # ระหว่างทำ 1 ครั้ง เก็บค่าที่แย่ที่สุดไว้ตัดสิน
        if self.state != self.UP:
            if v < self._min_v:              # จุดต่ำสุดของครั้งนี้ = รอยต่อช่วงลง/ช่วงขึ้น
                self._min_v, self._min_t = v, t
            if self.elbow is not None:
                self._min_elbow = min(self._min_elbow, self.elbow)
            if self.depth is not None:
                self._min_depth = min(self._min_depth, self.depth)
            if self.body is not None:
                self._min_body = min(self._min_body, self.body)
                self._body_frames += 1
                if self.body < config.BODY_STRAIGHT_MIN:
                    self._body_bad_frames += 1

        if self.state == self.UP:
            if v < start:
                self.state = self.GOING_DOWN
                self._start_t = self._min_t = t
                self._min_v = v
                self._min_elbow = self.elbow if self.elbow is not None else 180.0
                self._min_depth = self.depth if self.depth is not None else 999.0
                self._min_body = self.body or 180.0
                self._reached_bottom = False
                self._body_frames = self._body_bad_frames = 0

        elif self.state == self.GOING_DOWN:
            if v <= bottom:
                self.state = self.BOTTOM
                self._reached_bottom = True
            elif v > up:                             # ลงไม่สุดแล้วดันกลับขึ้น
                if t - self._start_t < config.MIN_PARTIAL_TIME:
                    self.state = self.UP             # แค่ค่ากระตุกชั่วครู่ ไม่นับ
                else:
                    events += self._finish_rep(t)

        elif self.state == self.BOTTOM:
            if v > bottom + 10:
                self.state = self.GOING_UP

        elif self.state == self.GOING_UP:
            if v >= up:
                events += self._finish_rep(t)
            elif v <= bottom:
                self.state = self.BOTTOM

        return events

    def _finish_rep(self, t):
        self.attempts += 1
        down_time = max(0.0, self._min_t - self._start_t)
        up_time = max(0.0, t - self._min_t)

        issues = []
        if not self._reached_bottom:
            issues.append("shallow")
        # ลำตัวไม่ตรง ต้องเป็นหลายเฟรม ไม่ใช่แค่ keypoint กระตุกเฟรมเดียว
        if (self._body_bad_frames >= 3 and
                self._body_bad_frames >= config.BODY_BAD_FRACTION * max(self._body_frames, 1)):
            issues.append("body")
        if down_time + up_time < config.MIN_REP_TIME:
            issues.append("fast")

        valid = "shallow" not in issues and "body" not in issues
        if valid:
            self.good_reps += 1

        rep = Rep(self.attempts, valid, round(self._min_elbow, 1), round(self._min_body, 1),
                  round(down_time, 2), round(up_time, 2), issues,
                  round(self._min_depth, 1) if self._min_depth < 999 else None)
        rep.t0, rep.t1 = self._start_t, t
        self._score(rep)
        self.reps.append(rep)

        self.state = self.UP
        return [("rep", rep)]

    # ---------- คะแนนท่า 0-100 ----------
    def _score(self, rep):
        """
        ความลึก 40% + ลำตัวตรง 35% + จังหวะ 25%
        ครั้งที่โกงท่า (ลงไม่ลึก / ลำตัวไม่ตรง) ได้ไม่เกิน 59
        """
        clamp = lambda v: int(max(0, min(100, round(v))))
        bottom = self.depth_bottom if config.COUNT_BY == "shoulder" else config.ELBOW_DOWN_ANGLE
        d = rep.min_depth if (config.COUNT_BY == "shoulder" and rep.min_depth is not None) else rep.min_elbow
        good_depth = bottom - 20            # ลงลึกกว่านี้ = เต็ม 100
        if d <= good_depth:
            depth = 100
        elif d <= bottom:
            depth = 100 - 30 * (d - good_depth) / 20
        else:
            depth = 60 - 2 * (d - bottom)
        frac = self._body_bad_frames / max(self._body_frames, 1)
        body = 100 - max(0.0, 165 - rep.min_body) * 2 - frac * 60
        tut = rep.tut
        lo, hi = 2.0, 4.0
        tempo = 100 if lo <= tut <= hi else (100 - (lo - tut) * 40 if tut < lo else 100 - (tut - hi) * 10)
        rep.depth_score, rep.body_score, rep.tempo_score = clamp(depth), clamp(body), clamp(max(tempo, 40))
        total = 0.4 * rep.depth_score + 0.35 * rep.body_score + 0.25 * rep.tempo_score
        rep.score = clamp(total if rep.valid else min(total, 59))

    # ---------- สรุปทั้งเซต ----------
    def summary(self):
        good = [r for r in self.reps if r.valid]
        avg_tut = sum(r.tut for r in good) / len(good) if good else 0.0
        return {
            "good_reps": self.good_reps,
            "attempts": self.attempts,
            "bad_reps": self.attempts - self.good_reps,
            "avg_tut": round(avg_tut, 2),
            "total_tut": round(sum(r.tut for r in good), 2),
            "shallow": sum("shallow" in r.issues for r in self.reps),
            "body": sum("body" in r.issues for r in self.reps),
            "fast": sum("fast" in r.issues for r in self.reps),
            "form_score": round(sum(r.score for r in self.reps) / len(self.reps)) if self.reps else 0,
        }
