"""
วิเคราะห์ท่าสควอท (Squat) จาก keypoints  -- ได้ทั้งกล้องด้านข้างและด้านหน้า (ระบบรู้เอง)

มุมด้านข้าง (แนะนำสำหรับความลึก/หลัง):
  - งานวิจัยวัดมุมข้อต่อด้วย pose estimation พบว่ามุมด้านข้างแม่นที่สุด
  - ตรวจ: ความลึก, หลังโน้มไปหน้ามากไป, ส้นเท้ายก, จังหวะ
มุมด้านหน้า (แนะนำสำหรับขา/เข่า):
  - ตรวจ: ความกว้างการยืน (กางขา), เข่าบิดเข้าใน, ความลึก (คร่าว ๆ), ส้นเท้ายก, จังหวะ

วิธีรู้มุม: ด้านหน้าเห็นไหล่สองข้างห่างกัน (กว้าง ~ครึ่งหนึ่งของความยาวลำตัว) ด้านข้างไหล่สองข้างแทบซ้อนกัน

ผลแต่ละครั้ง
  ไม่นับ        : ไม่ลึก (shallow), หลังโน้มมากไป (lean - ด้านข้างเท่านั้น)
  นับแต่หักคะแนน : ส้นเท้ายก (heels), เข่าบิดเข้า (knees), ยืนแคบ/กว้างไป (narrow/wide), เร็วไป (fast)
"""

import math
from collections import deque

import config
from pushup import Rep, angle

C = lambda name, default: getattr(config, name, default)
JOINTS = ("shoulder", "hip", "knee", "ankle")


class SquatAnalyzer:
    UP, GOING_DOWN, BOTTOM, GOING_UP = "UP", "GOING_DOWN", "BOTTOM", "GOING_UP"
    exercise = "squat"
    LEVEL_ADJUST = {"beginner": 10, "intermediate": 0, "advanced": -10}

    def __init__(self, level=None):
        self.level = level
        self.reset()

    @property
    def depth_bottom(self):
        return C("SQUAT_BOTTOM", 20) + self.LEVEL_ADJUST.get(self.level, 0)

    def reset(self):
        self.state = self.UP
        self.good_reps = 0
        self.attempts = 0
        self.reps = []
        self.side = None
        self.view = None           # "side" / "front"
        self.depth = None          # สะโพกสูงกว่าเข่ากี่ % ของต้นขา (100 = ยืนตรง, 0 = สะโพกระดับเข่า/ขนานพื้น)
        self.knee = None           # มุมเข่า (ด้านข้าง)
        self.lean = None           # ลำตัวเอียงจากแนวตั้ง (ด้านข้าง)
        self.heel = 0.0            # ข้อเท้ายกขึ้นจากตอนยืน (% ของหน้าแข้ง)
        self.stance = None         # ระยะข้อเท้า ÷ ความกว้างไหล่ (ด้านหน้า)
        self.knees_ratio = None    # ระยะเข่า ÷ ระยะข้อเท้า (ด้านหน้า)
        self.visible = False
        self.missing = list(JOINTS)
        self.elbow = self.body = None
        self._thigh = deque(maxlen=300)
        self._shin = deque(maxlen=300)
        self._ankle_base = {}
        self._new_rep_vars(0.0, 100.0)

    def _new_rep_vars(self, t, v):
        self._start_t = self._min_t = t
        self._min_v = v
        self._max_lean = self.lean or 0.0
        self._max_heel = 0.0
        self._min_knees = 9.9
        self._stance0 = self.stance
        self._reached_bottom = False
        self._frames = self._lean_frames = self._heel_frames = self._knee_frames = 0

    # ---------- ช่วยเหลือ ----------
    @staticmethod
    def _ok(kps, n):
        return n in kps and kps[n][2] >= config.MIN_KEYPOINT_CONF

    def _smooth(self, old, new):
        if old is None:
            return new
        a = config.ANGLE_SMOOTHING
        return a * new + (1 - a) * old

    @staticmethod
    def _ref(hist):
        s = sorted(hist)
        return s[int(len(s) * 0.9) - 1] if len(s) >= 10 else max(s)

    def _detect_view(self, kps):
        """ด้านหน้า = ไหล่สองข้างห่างกันชัดเจนเมื่อเทียบกับความยาวลำตัว"""
        if not all(self._ok(kps, f"{sd}_{n}") for sd in ("left", "right") for n in ("shoulder", "hip")):
            return self.view or "side"
        P = lambda n: kps[n][:2]
        torso = (math.dist(P("left_shoulder"), P("left_hip")) + math.dist(P("right_shoulder"), P("right_hip"))) / 2
        if torso < 1:
            return self.view or "side"
        ratio = abs(P("left_shoulder")[0] - P("right_shoulder")[0]) / torso
        if self.view == "front":
            return "side" if ratio < 0.3 else "front"
        if self.view == "side":
            return "front" if ratio > 0.5 else "side"
        return "front" if ratio > 0.4 else "side"

    def _leg_score(self, kps, side):
        names = [f"{side}_{n}" for n in ("hip", "knee", "ankle")]
        if not all(self._ok(kps, n) for n in names):
            return None
        h, k, a = (kps[n][:2] for n in names)
        return math.dist(h, k) + math.dist(k, a)

    def _pick_side(self, kps):
        valid = {sd: sc for sd in ("left", "right") if (sc := self._leg_score(kps, sd)) is not None}
        if not valid:
            return None
        best = max(valid, key=valid.get)
        cur = self.side
        if cur in valid and (self.state != self.UP or valid[best] < valid[cur] * config.SIDE_SWITCH_RATIO):
            return cur
        return best

    def _missing(self, kps, sides):
        out = []
        for j in JOINTS:
            if not any(self._ok(kps, f"{sd}_{j}") for sd in sides) if len(sides) == 1 else \
               not all(self._ok(kps, f"{sd}_{j}") for sd in sides):
                out.append(j)
        return out

    # ---------- เรียกทุกเฟรม ----------
    def update(self, kps, t):
        events = []
        if self.state == self.UP:                      # ระหว่างย่อห้ามสลับมุม
            self.view = self._detect_view(kps)

        if self.view == "front":
            self.missing = self._missing(kps, ("left", "right"))
            self.visible = not self.missing
            if any(j in self.missing for j in ("hip", "knee", "ankle")):
                return events
            self.side = None
            P = lambda n: kps[n][:2]
            hip_y = (P("left_hip")[1] + P("right_hip")[1]) / 2
            knee_y = (P("left_knee")[1] + P("right_knee")[1]) / 2
            ankle_y = (P("left_ankle")[1] + P("right_ankle")[1]) / 2
            thigh = (math.dist(P("left_hip"), P("left_knee")) + math.dist(P("right_hip"), P("right_knee"))) / 2
            shin = (math.dist(P("left_knee"), P("left_ankle")) + math.dist(P("right_knee"), P("right_ankle"))) / 2
            ankle_dx = abs(P("left_ankle")[0] - P("right_ankle")[0])
            knee_dx = abs(P("left_knee")[0] - P("right_knee")[0])
            if "shoulder" not in self.missing:
                sh_dx = abs(P("left_shoulder")[0] - P("right_shoulder")[0])
                if sh_dx > 1:
                    self.stance = self._smooth(self.stance, ankle_dx / sh_dx)
            if ankle_dx > 1:
                self.knees_ratio = self._smooth(self.knees_ratio, knee_dx / ankle_dx)
            self.lean = None
            self.knee = None
            base_key = "front"
        else:
            side = self._pick_side(kps)
            if side is None:
                best = max(("left", "right"), key=lambda sd: sum(kps.get(f"{sd}_{j}", (0, 0, 0))[2] for j in JOINTS))
                self.missing = self._missing(kps, (best,))
                self.visible = False
                return events
            self.side = side
            self.missing = self._missing(kps, (side,))
            self.visible = not self.missing
            p = lambda n: kps[f"{side}_{n}"][:2]
            hip, knee, ankle = p("hip"), p("knee"), p("ankle")
            hip_y, knee_y, ankle_y = hip[1], knee[1], ankle[1]
            thigh, shin = math.dist(hip, knee), math.dist(knee, ankle)
            self.knee = self._smooth(self.knee, angle(hip, knee, ankle))
            if self._ok(kps, f"{side}_shoulder"):
                s = p("shoulder")
                lean = math.degrees(math.atan2(abs(s[0] - hip[0]), max(1e-6, hip[1] - s[1])))
                self.lean = self._smooth(self.lean, lean)
            self.stance = self.knees_ratio = None
            base_key = side

        if thigh < 5 or shin < 5:
            return events
        self._thigh.append(thigh)
        self._shin.append(shin)
        thigh_ref, shin_ref = self._ref(self._thigh), self._ref(self._shin)
        h = 100.0 * (knee_y - hip_y) / thigh_ref
        self.depth = self._smooth(self.depth, max(-30.0, min(110.0, h)))
        # ส้นเท้า: ตอนยืนตรงจำตำแหน่งข้อเท้าไว้ ระหว่างย่อถ้าข้อเท้ายกขึ้น = ส้นเท้ายก
        if self.state == self.UP and self.depth >= C("SQUAT_UP", 85):
            b = self._ankle_base.get(base_key)
            self._ankle_base[base_key] = ankle_y if b is None else 0.9 * b + 0.1 * ankle_y
        base = self._ankle_base.get(base_key)
        self.heel = max(0.0, 100.0 * (base - ankle_y) / shin_ref) if base is not None else 0.0

        v = self.depth
        up, start, bottom = C("SQUAT_UP", 85), C("SQUAT_START_DOWN", 75), self.depth_bottom

        if self.state != self.UP:
            if v < self._min_v:
                self._min_v, self._min_t = v, t
            self._frames += 1
            if self.lean is not None:
                self._max_lean = max(self._max_lean, self.lean)
                if self.lean > C("SQUAT_MAX_LEAN", 55):
                    self._lean_frames += 1
            self._max_heel = max(self._max_heel, self.heel)
            if self.heel > C("SQUAT_HEEL_RISE", 12):
                self._heel_frames += 1
            if self.knees_ratio is not None and v < 60:        # เช็กเข่าเฉพาะช่วงย่อลึก
                self._min_knees = min(self._min_knees, self.knees_ratio)
                if self.knees_ratio < C("SQUAT_KNEE_IN", 0.8):
                    self._knee_frames += 1

        if self.state == self.UP:
            if v < start:
                self.state = self.GOING_DOWN
                self._new_rep_vars(t, v)
        elif self.state == self.GOING_DOWN:
            if v <= bottom:
                self.state = self.BOTTOM
                self._reached_bottom = True
            elif v > up:
                if t - self._start_t < config.MIN_PARTIAL_TIME:
                    self.state = self.UP
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
        many = lambda n: n >= 3 and n >= 0.25 * max(self._frames, 1)   # ต้องเป็นหลายเฟรม ไม่ใช่จุดกระตุก
        issues = []
        if not self._reached_bottom:
            issues.append("shallow")
        if many(self._lean_frames):
            issues.append("lean")
        if many(self._heel_frames):
            issues.append("heels")
        if many(self._knee_frames):
            issues.append("knees")
        if self.view == "front" and self._stance0 is not None:
            lo, hi = C("SQUAT_STANCE", (0.75, 2.0))
            if self._stance0 < lo:
                issues.append("narrow")
            elif self._stance0 > hi:
                issues.append("wide")
        if down_time + up_time < C("SQUAT_MIN_REP_TIME", 1.5):
            issues.append("fast")
        valid = "shallow" not in issues and "lean" not in issues
        if valid:
            self.good_reps += 1
        rep = Rep(self.attempts, valid, round(self.knee or 0, 1), round(self._max_lean, 1),
                  round(down_time, 2), round(up_time, 2), issues, round(self._min_v, 1))
        rep.max_lean, rep.max_heel, rep.view = round(self._max_lean, 1), round(self._max_heel, 1), self.view
        rep.t0, rep.t1 = self._start_t, t
        rep.min_knees = round(self._min_knees, 2) if self._min_knees < 9 else None
        self._score(rep)
        self.reps.append(rep)
        self.state = self.UP
        return [("rep", rep)]

    def _score(self, rep):
        """ความลึก 40% + ลำตัว/ขา 35% + จังหวะ 25%  ครั้งที่ไม่นับได้ไม่เกิน 59"""
        clamp = lambda v: int(max(0, min(100, round(v))))
        b, d = self.depth_bottom, rep.min_depth
        depth = 100 if d <= b - 15 else (100 - (d - b + 15) * 1.0 if d <= b else 85 - (d - b) * 2.5)
        if rep.view == "side":
            ml = C("SQUAT_MAX_LEAN", 55)
            body = 100 if rep.max_lean <= ml - 10 else (100 - (rep.max_lean - ml + 10) * 2 if rep.max_lean <= ml
                                                       else 80 - (rep.max_lean - ml) * 3)
        else:
            body = 100
        body -= 25 * ("heels" in rep.issues) + 35 * ("knees" in rep.issues) + \
            15 * ("narrow" in rep.issues or "wide" in rep.issues)
        tut, lo, hi = rep.tut, 2.0, 4.0
        tempo = 100 if lo <= tut <= hi else (100 - (lo - tut) * 40 if tut < lo else 100 - (tut - hi) * 10)
        rep.depth_score, rep.body_score, rep.tempo_score = clamp(depth), clamp(body), clamp(max(tempo, 40))
        total = 0.4 * rep.depth_score + 0.35 * rep.body_score + 0.25 * rep.tempo_score
        rep.score = clamp(total if rep.valid else min(total, 59))

    def metrics(self):
        r = lambda v, n=1: round(v, n) if v is not None else None
        return {"view": self.view, "knee": r(self.knee), "lean": r(self.lean), "heel": r(self.heel),
                "stance": r(self.stance, 2), "knees": r(self.knees_ratio, 2),
                "visible": self.visible, "missing": self.missing}

    def summary(self):
        good = [r for r in self.reps if r.valid]
        avg_tut = sum(r.tut for r in good) / len(good) if good else 0.0
        return {
            "good_reps": self.good_reps, "attempts": self.attempts, "bad_reps": self.attempts - self.good_reps,
            "avg_tut": round(avg_tut, 2), "total_tut": round(sum(r.tut for r in good), 2),
            "shallow": sum("shallow" in r.issues for r in self.reps),
            "body": sum(bool({"lean", "knees"} & set(r.issues)) for r in self.reps),
            "fast": sum("fast" in r.issues for r in self.reps),
            "form_score": round(sum(r.score for r in self.reps) / len(self.reps)) if self.reps else 0,
        }
