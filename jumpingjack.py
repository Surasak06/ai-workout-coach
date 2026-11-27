"""
วิเคราะห์ท่ากระโดดตบ (Jumping Jack) จาก keypoints — ต้องใช้กล้องด้านหน้า (เห็นไหล่สองข้างห่างกัน)

นับ: ยืนชิด -> กระโดดกางขา+ชูมือเหนือหัว -> กลับมายืนชิด แขนลง = 1 ครั้ง
วัดเทียบสัดส่วนร่างกาย (ไม่ขึ้นกับระยะกล้อง)
  แขน : ข้อมือสูงกว่าไหล่กี่เท่าของความยาวลำตัว (เหนือหัว ~0.5 ขึ้นไป)
  ขา  : ระยะข้อเท้าซ้าย-ขวา ÷ ความกว้างไหล่ (ยืนชิด ~1, กางขา ~1.6 ขึ้นไป)
ไม่นับ: มือไม่ถึงเหนือหัว (arms) / ไม่กางขา (legs)
"""
import config
from pushup import Rep

C = lambda name, default: getattr(config, name, default)


class JumpingJackAnalyzer:
    CLOSED, OPEN = "CLOSED", "OPEN"
    exercise = "jumpingjack"
    LEVEL_ADJUST = {"beginner": (-0.15, -0.15), "intermediate": (0, 0), "advanced": (0.05, 0.1)}

    def __init__(self, level=None):
        self.level = level
        self.reset()

    @property
    def need(self):        # (แขนต้องสูงกว่าไหล่กี่เท่าลำตัว, ขาต้องกางกี่เท่าไหล่)
        a, l = self.LEVEL_ADJUST.get(self.level, (0, 0))
        return C("JJ_ARM_UP", 0.45) + a, C("JJ_LEG_OPEN", 1.55) + l

    @property
    def depth_bottom(self):
        return max(0.0, 100 - self._arm_pct(self.need[0]))

    @staticmethod
    def _arm_pct(a):       # แขน: -0.9 (แนบตัว) -> 0% ... 0.6 (เหนือหัว) -> 100%
        return max(0.0, min(100.0, (a + 0.9) / 1.5 * 100))

    def reset(self):
        self.state = self.CLOSED
        self.good_reps = self.attempts = 0
        self.reps = []
        self.side = "front"
        self.arm = self.leg = self.depth = None
        self.knee = self.lean = self.elbow = self.body = None
        self.visible, self.missing = False, ["shoulder", "wrist", "hip", "ankle"]
        self._start_t = 0.0
        self._max_arm = -9.0
        self._max_leg = 0.0

    @staticmethod
    def _ok(kps, n):
        return n in kps and kps[n][2] >= config.MIN_KEYPOINT_CONF

    def _smooth(self, old, new):
        return new if old is None else 0.6 * new + 0.4 * old

    def update(self, kps, t):
        events = []
        need = ("left_shoulder", "right_shoulder", "left_hip", "right_hip")
        if not all(self._ok(kps, n) for n in need):
            self.visible, self.missing = False, ["shoulder", "hip"]
            return events
        ls, rs, lh, rh = (kps[n] for n in need)
        sw = abs(ls[0] - rs[0])
        torso = ((lh[1] + rh[1]) - (ls[1] + rs[1])) / 2
        if torso <= 1 or sw < 0.25 * torso:              # หันข้าง: ไหล่ซ้อนกัน ใช้ท่านี้ไม่ได้
            self.visible, self.missing = False, ["หันหน้าเข้ากล้อง"]
            return events
        sy = (ls[1] + rs[1]) / 2
        wr = [kps[n] for n in ("left_wrist", "right_wrist") if self._ok(kps, n)]
        an = [kps[n] for n in ("left_ankle", "right_ankle") if self._ok(kps, n)]
        self.visible = len(wr) == 2 and len(an) == 2
        self.missing = [] if self.visible else (["wrist"] if len(wr) < 2 else []) + (["ankle"] if len(an) < 2 else [])
        if not self.visible:
            return events
        arm = min((sy - w[1]) / torso for w in wr)       # มือข้างที่ต่ำกว่า (ต้องขึ้นทั้งสองข้าง)
        leg = abs(an[0][0] - an[1][0]) / sw
        self.arm, self.leg = self._smooth(self.arm, arm), self._smooth(self.leg, leg)
        self.depth = 100 - self._arm_pct(self.arm)

        if self.state == self.CLOSED:
            if self.arm > -0.2 or self.leg > 1.35:         # เริ่มกระโดดออก
                self.state, self._start_t = self.OPEN, t
                self._max_arm, self._max_leg = self.arm, self.leg
        else:
            self._max_arm, self._max_leg = max(self._max_arm, self.arm), max(self._max_leg, self.leg)
            if self.arm < -0.45 and self.leg < 1.3:          # กลับมายืนชิด แขนลง
                if t - self._start_t >= 0.25:
                    events += self._finish(t)
                else:
                    self.state = self.CLOSED
            elif t - self._start_t > C("JJ_TIMEOUT", 4.0):   # ค้างนานเกิน = ไม่ใช่กระโดดตบ
                self.state = self.CLOSED
        return events

    def _finish(self, t):
        self.attempts += 1
        need_arm, need_leg = self.need
        issues = []
        if self._max_arm < need_arm:
            issues.append("arms")
        if self._max_leg < need_leg:
            issues.append("legs")
        valid = not issues
        if valid:
            self.good_reps += 1
        dur = max(0.0, t - self._start_t)
        rep = Rep(self.attempts, valid, 0.0, 0.0, round(dur / 2, 2), round(dur / 2, 2), issues,
                  round(100 - self._arm_pct(self._max_arm), 1))
        rep.t0, rep.t1 = self._start_t, t
        clamp = lambda x: int(max(0, min(100, round(x))))
        arms = 100 if self._max_arm >= need_arm + 0.15 else 100 - (need_arm + 0.15 - self._max_arm) * 150
        legs = 100 if self._max_leg >= need_leg + 0.2 else 100 - (need_leg + 0.2 - self._max_leg) * 120
        rep.depth_score, rep.body_score, rep.tempo_score = clamp(arms), clamp(legs), 100
        total = 0.5 * rep.depth_score + 0.5 * rep.body_score
        rep.score = clamp(total if valid else min(total, 59))
        self.reps.append(rep)
        self.state = self.CLOSED
        return [("rep", rep)]

    def metrics(self):
        r = lambda v: round(v, 2) if v is not None else None
        return {"view": "front", "arm": r(self.arm), "leg": r(self.leg), "visible": self.visible, "missing": self.missing}

    def summary(self):
        good = [r for r in self.reps if r.valid]
        return {"good_reps": self.good_reps, "attempts": self.attempts, "bad_reps": self.attempts - self.good_reps,
                "avg_tut": round(sum(r.tut for r in good) / len(good), 2) if good else 0.0,
                "total_tut": round(sum(r.tut for r in good), 2),
                "shallow": sum("arms" in r.issues for r in self.reps), "body": sum("legs" in r.issues for r in self.reps),
                "fast": 0, "form_score": round(sum(r.score for r in self.reps) / len(self.reps)) if self.reps else 0}
