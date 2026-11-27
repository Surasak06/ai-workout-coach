"""
ตัวประมวลผลหลักเมื่อรันใน CiRA CORE (ถูกเรียกจาก Python+ node ผ่าน cira_node.py)

แยกออกมาเป็นไฟล์ในโฟลเดอร์โปรเจค เพื่อให้แก้ไข/อัปเดตได้โดยไม่ต้อง copy โค้ดไปวางใน Python+ ใหม่
ค่าที่ต้องปรับ (พอร์ต ESP32, FPS วิดีโอ, เกณฑ์มุม) อยู่ใน config.py
"""

import os
import sys
import time

import cv2
import numpy as np

import config
from pose_source import COCO_NAMES, SKELETON, YoloPose
from pushup import PushUpAnalyzer
import importlib as _il, squat as _squat, game as _game, plank as _plank, lunge as _lunge, jumpingjack as _jj
_il.reload(_squat)                      # โหนด CiRA reload แค่ไฟล์เดิม ๆ -> reload ไฟล์ท่าใหม่ตรงนี้แทน
_il.reload(_game)
_il.reload(_plank)
_il.reload(_lunge)
_il.reload(_jj)
ANALYZERS = {"pushup": PushUpAnalyzer, "squat": _squat.SquatAnalyzer, "game": _game.GameAnalyzer,
             "plank": _plank.PlankAnalyzer, "lunge": _lunge.LungeAnalyzer, "jumpingjack": _jj.JumpingJackAnalyzer}
CODE_VERSION = 8                        # เว็บใช้เช็กว่า CiRA โหลดโค้ดใหม่แล้ว (ถ้ายังเก่าจะขึ้นเตือนให้ Stop→Run)
from stats_logger import StatsLogger
from tracker import PanTiltTracker
from voice import Voice
import importlib
import uuid
import hub_client
importlib.reload(hub_client)

SESSION_END_AFTER = 10   # ไม่เห็นคนกี่วินาที (หลังทำไปแล้ว) ถือว่าจบเซต -> บันทึกสถิติ
USE_YOLO_IN_NODE = True  # True = หาข้อต่อเองด้วย YOLO ; False = ใช้ผลจาก node Pose ของ CiRA (ถ้ามี)


def _find_keypoint_list(obj):
    """ค้นหา list ของ 17 จุด [[x, y, (conf)], ...] ที่ซ้อนอยู่ใน payload ไม่ว่าจะอยู่ชั้นไหน"""
    if isinstance(obj, (list, tuple)):
        if len(obj) == 17 and all(isinstance(p, (list, tuple)) and len(p) >= 2
                                  and all(isinstance(v, (int, float)) for v in p[:2]) for p in obj):
            return obj
        if len(obj) == 51 and all(isinstance(v, (int, float)) for v in obj):
            return [obj[i:i + 3] for i in range(0, 51, 3)]
        for item in obj:
            found = _find_keypoint_list(item)
            if found:
                return found
    elif isinstance(obj, dict):
        for v in obj.values():
            found = _find_keypoint_list(v)
            if found:
                return found
    return None


def parse_keypoints(payload, img):
    pts = _find_keypoint_list(payload)
    if not pts:
        return {}
    h, w = (img.shape[:2] if img is not None else (1, 1))
    normalized = all(abs(p[0]) <= 1.5 and abs(p[1]) <= 1.5 for p in pts)
    kps = {}
    for name, p in zip(COCO_NAMES, pts):
        x, y = float(p[0]), float(p[1])
        if normalized:
            x, y = x * w, y * h
        kps[name] = (x, y, float(p[2]) if len(p) >= 3 else 1.0)
    return kps


class CoachApp:
    def __init__(self, project_dir):
        # ปิดตัวเก่า (ถ้ากด Stop/Start) เพื่อคืนพอร์ต ESP32
        old = getattr(sys, "_workout_coach_app", None)
        if old is not None:
            try:
                old.close()
            except Exception:
                pass
        sys._workout_coach_app = self

        config.DATA_DIR = os.path.join(project_dir, "data")
        self.local_ex = getattr(config, "LOCAL_EXERCISE", "pushup") if getattr(config, "LOCAL_EXERCISE", "pushup") in ANALYZERS else "pushup"
        self.analyzer = ANALYZERS[self.local_ex]()
        self.tracker = PanTiltTracker(config.SERIAL_PORT)
        self.voice = Voice()
        self.logger = StatsLogger(self.local_ex)
        self.yolo = YoloPose(os.path.join(project_dir, "yolo11n-pose.pt")) if USE_YOLO_IN_NODE else None
        self.frame_idx = 0
        self.last_seen = None
        self.project_dir = project_dir
        self.sessions_saved = 0
        self.last_session = None
        self.dashboard_path = None
        self.hub = None
        self.web = None
        self.web_streams = {}
        if getattr(config, "INPUT_SOURCE", "cira") == "web" and getattr(config, "HUB_URL", None):
            self.web = hub_client.WebWorker(config.HUB_URL)
            if old is not None and getattr(old, "web_streams", None):
                self.web_streams = old.web_streams        # กด Stop -> Start แล้วเซตที่เล่นอยู่ไม่หาย
        if getattr(config, "HUB_URL", None):
            prev = getattr(old, "hub", None) if old is not None else None
            if prev is not None and prev.url == config.HUB_URL.rstrip("/") and prev.player == config.PLAYER_NAME:
                self.hub = prev      # ใช้ตัวเดิมต่อ จะได้เห็น +XP ของเซตที่เพิ่งจบตอนกด Stop -> Start
            else:
                self.hub = hub_client.HubClient(config.HUB_URL, config.PLAYER_NAME, self.logger.data_dir)
        # สำหรับนับเวลาจากคลิปวิดีโอ (ดู _video_time)
        self.vf_remain = None        # remain_sec ล่าสุดที่ node VideoFile ส่งมา
        self.vf_frames = 0           # จำนวนเฟรมตั้งแต่ remain_sec เปลี่ยนครั้งล่าสุด
        self.vf_seconds = 0          # จำนวนวินาทีของคลิปที่ผ่านไปแล้ว (เต็มวินาที)
        self.vf_fps_samples = []     # เฟรมต่อ 1 วินาทีของคลิป ที่วัดได้
        self.vf_state = None
        self.voice.say("start")

    def close(self):
        # ถูกเรียกเมื่อกด Stop -> Start ใน CiRA: บันทึกเซตที่ค้างอยู่ก่อนเริ่มใหม่
        try:
            self._end_session(None, "stopped")
        finally:
            self.tracker.close()

    @staticmethod
    def _find_video_info(payload):
        """หา {"remain_sec": .., "state": ..} ที่ node VideoFile ใส่ไว้ใน payload"""
        for v in payload.values():
            if isinstance(v, dict) and "remain_sec" in v:
                return v
        return None

    @property
    def fps_estimate(self):
        if self.vf_fps_samples:
            recent = sorted(self.vf_fps_samples[-5:])
            return recent[len(recent) // 2]          # median กันค่าเพี้ยน
        return float(config.VIDEO_FPS or 30)

    def _video_time(self, info):
        """
        เวลาในคลิป (วินาที) โดยไม่ต้องรู้ FPS ล่วงหน้า:
        node VideoFile บอก remain_sec (เหลือกี่วินาที) ทุกเฟรม
        นับว่ามีกี่เฟรมต่อ 1 วินาทีของคลิป -> ได้ FPS ที่ CiRA ส่งมาจริง (แม้คอมช้าหรือข้ามเฟรม)
        คืนค่า (เวลา, คลิปเริ่มใหม่หรือไม่)
        """
        remain = info.get("remain_sec")
        restarted = False
        if self.vf_remain is None:
            self.vf_remain = remain
        elif remain != self.vf_remain:
            if remain < self.vf_remain:              # คลิปเดินหน้า
                if self.vf_seconds > 0:              # ช่วงแรกอาจไม่เต็มวินาที ไม่เอามาวัด
                    self.vf_fps_samples.append(self.vf_frames)
                self.vf_seconds += self.vf_remain - remain
            else:                                    # remain เพิ่มขึ้น = คลิปเล่นวนใหม่
                restarted = True
                self.vf_seconds += 1
            self.vf_remain = remain
            self.vf_frames = 0
        self.vf_frames += 1
        return self.vf_seconds + self.vf_frames / self.fps_estimate, restarted

    def _end_session(self, payload, reason):
        an = self.analyzer
        if an.attempts > 0:
            reps = list(an.reps)
            row = self.logger.log_session(an.summary())
            if self.hub is not None:
                self.hub.send_session(row, reps, exercise=self.local_ex)
            row["end_reason"] = reason
            self.last_session = row
            self.sessions_saved += 1
            self.voice.say("done", n=an.good_reps)
            try:                                     # อัปเดตรูป dashboard ทุกครั้งที่จบเซต
                import importlib, dashboard
                importlib.reload(dashboard)
                self.dashboard_path = dashboard.build_dashboard(self.logger.data_dir)
            except Exception as e:
                print("[dashboard] สร้างไม่สำเร็จ:", e)
        an.reset()
        self.logger = StatsLogger(self.local_ex)

    def process(self, payload, img):
        if self.web is not None:
            return self._process_web(payload)
        self.frame_idx += 1
        an = self.analyzer

        # ----- เวลา: คลิปวิดีโอใช้เวลาของคลิป, กล้องจริงใช้นาฬิกาจริง -----
        info = self._find_video_info(payload)
        if info is not None:
            now, restarted = self._video_time(info)
            state = info.get("state")
            ended = (self.vf_state == "playing" and state not in (None, "playing"))
            self.vf_state = state
            if restarted or ended:                   # คลิปจบ/วนใหม่ = จบเซต บันทึกสถิติ
                self._end_session(payload, "video_end")
            time_source = f"video ({self.fps_estimate:.0f} fps)"
        else:
            now = time.time()
            time_source = "clock"

        if self.yolo is not None and img is not None:
            kps = self.yolo.detect(img)
        else:
            kps = parse_keypoints(payload, img)
            if not kps:
                payload["payload_keys"] = list(payload.keys())
                payload["coach_error"] = "ไม่พบ keypoints 17 จุดใน payload"

        events = an.update(kps, now)
        target = None
        if img is not None:
            h, w = img.shape[:2]
            target = self.tracker.update(kps, w, h)

        for kind, rep in events:
            self.logger.log_rep(rep)
            if rep.valid:
                self.voice.say("count", n=an.good_reps)
            for issue in rep.issues:
                self.voice.say(issue)

        if target is not None or self.last_seen is None:
            self.last_seen = now
        elif an.attempts > 0 and now - self.last_seen > SESSION_END_AFTER:
            self._end_session(payload, "no_person")

        payload["coach"] = {
            "reps": an.good_reps,
            "bad_reps": an.attempts - an.good_reps,
            "state": an.state,
            "count_by": config.COUNT_BY,
            "depth": round(an.depth, 1) if an.depth is not None else None,
            "elbow": round(an.elbow, 1) if an.elbow is not None else None,
            "body": round(an.body, 1) if an.body is not None else None,
            "pan": round(self.tracker.pan), "tilt": round(self.tracker.tilt),
            "device": self.yolo.device_name if self.yolo is not None else "CiRA pose node",
            "time_source": time_source,
            "stats": {
                "data_folder": config.DATA_DIR,
                "sessions_saved": self.sessions_saved,
                "last_session": self.last_session,
                "dashboard": self.dashboard_path,
            },
            "hub": None if self.hub is None else {
                "url": self.hub.url, "player": self.hub.player, "status": self.hub.status,
                "last_result": self.hub.last_result,
            },
            # ประวัติ 10 ครั้งล่าสุด ดูได้ว่าแต่ละครั้งโดนตัดเพราะอะไร
            "history": [
                {"n": r.number, "ok": r.valid, "issues": r.issues, "min_depth": r.min_depth,
                 "min_elbow": r.min_elbow,
                 "min_body": r.min_body, "tut": round(r.tut, 1)}
                for r in an.reps[-10:]
            ],
        }

        if self.hub is not None:
            self.hub.send_live({
                "reps": an.good_reps, "bad": an.attempts - an.good_reps, "state": an.state,
                "depth": round(an.depth, 1) if an.depth is not None else None,
                "depth_bottom": config.DEPTH_BOTTOM,
                "last_tut": round(an.reps[-1].tut, 1) if an.reps else None,
            })

        if img is not None:
            self.draw(img, kps)
        return payload, img

    # ===================== โหมดเว็บ =====================
    def _process_web(self, payload):
        """ขอภาพจาก HUB (ที่เว็บส่งมา) ทีละไม่เกิน 2 ภาพต่อรอบ ประมวลผล แล้วส่งผลกลับ"""
        out_img = None
        try:
            for _ in range(config.MAX_WEB_JOBS_PER_TICK):
                job = self.web.fetch()
                if job is None:
                    break
                if job[0] == "cmd":
                    if job[1].get("cmd") == "stop":
                        self._web_finish(job[1])
                    continue
                _, meta, jpg = job
                frame = cv2.imdecode(np.frombuffer(jpg, np.uint8), cv2.IMREAD_COLOR)
                if frame is not None:
                    out_img = self._web_frame(meta, frame)
            web_error = None
        except Exception as e:
            web_error = f"ต่อ HUB ไม่ได้: {e}"
            time.sleep(0.2)

        payload["coach"] = {
            "mode": "web",
            "hub": config.HUB_URL,
            "error": web_error,
            "device": self.yolo.device_name if self.yolo is not None else None,
            "players": [{"player": st["player"], "reps": st["an"].good_reps,
                         "bad": st["an"].attempts - st["an"].good_reps}
                        for st in self.web_streams.values()],
        }
        # ภาพที่ส่งออกจาก node: ใช้ภาพล่าสุดของแต่ละคนเสมอ (กันจอกระพริบตอนรอบนี้ไม่มีภาพใหม่)
        now = time.time()
        recent = [st["last_img"] for st in self.web_streams.values()
                  if st.get("last_img") is not None and now - st["last_t"] < 5]
        if recent:
            out_img = self._tile(recent)
        else:
            out_img = self._waiting_image(web_error)
        return payload, out_img

    @staticmethod
    def _tile(images, height=480):
        """2 คน = วางภาพคู่กันซ้าย-ขวา, 1 คน = ภาพเดียว"""
        if len(images) == 1:
            return images[0].copy()
        resized = [cv2.resize(im, (int(im.shape[1] * height / im.shape[0]), height)) for im in images]
        gap = np.zeros((height, 8, 3), np.uint8)
        out = resized[0]
        for im in resized[1:]:
            out = np.hstack([out, gap, im])
        return out

    @staticmethod
    def _full_body(kps, w, h):
        """เห็นไหล่ สะโพก เข่า ข้อเท้า (ข้างใดข้างหนึ่ง) และจุดเหล่านั้นอยู่ในภาพจริง ๆ (กล้องใกล้ไป YOLO จะเดาจุดเลยขอบภาพ)"""
        conf = config.MIN_KEYPOINT_CONF
        def seen(n):
            x, y, c = kps.get(n, (0, 0, 0))
            return c >= conf and 0.01 * w < x < 0.99 * w and 0.01 * h < y < 0.99 * h
        missing = [j for j in ("shoulder", "hip", "knee", "ankle") if not (seen(f"left_{j}") or seen(f"right_{j}"))]
        return not missing, missing

    def _web_frame(self, meta, frame):
        sid = meta["stream"]
        st = self.web_streams.get(sid)
        if st is None:
            ex = meta.get("exercise") if meta.get("exercise") in ANALYZERS else "pushup"
            st = {"player": meta["player"], "an": ANALYZERS[ex](level=meta.get("level")), "logger": StatsLogger(ex)}
            self.web_streams[sid] = st
            print(f"[web] เริ่มเซตใหม่: {meta['player']} ({ex})")
        an = st["an"]
        h, w = frame.shape[:2]
        if hasattr(an, "frame_size"):
            an.frame_size = (w, h)
        kps = self.yolo.detect(frame) if self.yolo is not None else {}
        full, missing = self._full_body(kps, w, h) if kps else (False, [])
        events = an.update(kps, meta["t"])
        for kind, rep in events:
            st["logger"].log_rep(rep)
        if meta["local"] and self.tracker.ser is not None:
            self.tracker.update(kps, w, h)        # แขนกลตามเฉพาะกล้องที่เสียบเครื่องนี้

        self.web.result(sid, {
            "seq": meta["seq"], "w": w, "h": h,
            "kps": {n: [round(x / w, 4), round(y / h, 4), round(c, 3)] for n, (x, y, c) in kps.items()},
            "full": full, "missing": missing,
            "side": an.side, "reps": an.good_reps, "bad": an.attempts - an.good_reps,
            "state": an.state,
            "depth": round(an.depth, 1) if an.depth is not None else None,
            "depth_bottom": an.depth_bottom, "exercise": an.exercise, "ver": CODE_VERSION,
            "elbow": round(an.elbow, 1) if an.elbow is not None else None,
            "body": round(an.body, 1) if an.body is not None else None,
            "metrics": an.metrics(),
            "last_tut": round(an.reps[-1].tut, 1) if an.reps else None,
            "rep_log": [{"n": r.number, "ok": r.valid, "issues": r.issues, "tut": round(r.tut, 1),
                         "score": r.score, "depth": r.depth_score, "body": r.body_score, "tempo": r.tempo_score}
                        for r in an.reps],
            "device": self.yolo.device_name if self.yolo is not None else None,
        })
        self.draw(frame, kps, an=an, label=f"WEB: {st['player']}")
        st["last_img"], st["last_t"] = frame, time.time()
        return frame

    def _web_finish(self, cmd):
        sid = cmd["stream"]
        st = self.web_streams.pop(sid, None)
        data = {"attempts": 0}
        if st is not None and st["an"].attempts > 0:
            an = st["an"]
            row = st["logger"].log_session(an.summary())
            data = {
                "client_id": uuid.uuid4().hex, "good_reps": an.good_reps, "attempts": an.attempts,
                "bad_reps": an.attempts - an.good_reps, "avg_tut": row.get("avg_tut", 0),
                "shallow": row.get("shallow", 0), "body": row.get("body", 0), "fast": row.get("fast", 0),
                "form_score": row.get("form_score", 0),
                "reps": [{"tut": round(r.tut, 2), "valid": bool(r.valid), "issues": list(r.issues), "score": r.score,
                          "t0": round(r.t0, 2), "t1": round(r.t1, 2)}
                         for r in an.reps],
            }
            try:
                import importlib, dashboard
                importlib.reload(dashboard)
                self.dashboard_path = dashboard.build_dashboard(st["logger"].data_dir)
            except Exception as e:
                print("[dashboard] สร้างไม่สำเร็จ:", e)
        res = self.web.finish(sid, data)
        print(f"[web] จบเซต {cmd.get('player')}: {data.get('good_reps', 0)} ครั้ง -> {res and res.get('xp')} XP")

    def _waiting_image(self, error=None):
        img = np.zeros((360, 640, 3), np.uint8)
        img[:] = (40, 30, 20)
        cv2.putText(img, "AI Workout Coach - WEB MODE", (20, 50), cv2.FONT_HERSHEY_SIMPLEX, 0.9, (0, 255, 120), 2)
        if error:
            cv2.putText(img, "Cannot reach HUB - is start_hub.bat running?", (20, 110),
                        cv2.FONT_HERSHEY_SIMPLEX, 0.6, (80, 80, 255), 2)
            cv2.putText(img, str(config.HUB_URL), (20, 145), cv2.FONT_HERSHEY_SIMPLEX, 0.6, (200, 200, 200), 1)
        elif self.web_streams:
            y = 110
            for st in self.web_streams.values():
                cv2.putText(img, f"{st['player']}: {st['an'].good_reps} reps (waiting for frames)", (20, y),
                            cv2.FONT_HERSHEY_SIMPLEX, 0.65, (255, 255, 255), 2)
                y += 35
        else:
            cv2.putText(img, "Waiting for someone to press START on the web...", (20, 110),
                        cv2.FONT_HERSHEY_SIMPLEX, 0.6, (255, 255, 255), 1)
            cv2.putText(img, f"Open: {config.HUB_URL}", (20, 145), cv2.FONT_HERSHEY_SIMPLEX, 0.6,
                        (200, 200, 200), 1)
        return img

    def draw(self, img, kps, an=None, label=None):
        web = an is not None
        an = an or self.analyzer
        h, w = img.shape[:2]
        s = max(w / 640.0, 0.6)
        th = max(int(2 * s), 1)
        ok = lambda n: n in kps and kps[n][2] >= config.MIN_KEYPOINT_CONF
        side = an.side
        for a, b in SKELETON:
            if ok(a) and ok(b):
                color = (0, 255, 255) if side and a.startswith(side) and b.startswith(side) else (0, 200, 0)
                cv2.line(img, tuple(map(int, kps[a][:2])), tuple(map(int, kps[b][:2])), color, th + 1)
        for n, (x, y, c) in kps.items():
            if c >= config.MIN_KEYPOINT_CONF:
                cv2.circle(img, (int(x), int(y)), int(5 * s), (0, 0, 255), -1)

        elbow = f"{an.elbow:.0f}" if an.elbow is not None else "-"
        body = f"{an.body:.0f}" if an.body is not None else "-"
        depth = f"{an.depth:.0f}%" if an.depth is not None else "-"
        lines = [
            (f"REPS {an.good_reps}   bad {an.attempts - an.good_reps}", (0, 255, 120), 1.4),
            (f"GAME pose {an.pose}  lane {an.lane}" if getattr(an, "exercise", "") == "game" else
             f"depth {depth}  elbow {elbow}  body {body}  {an.state}" if getattr(an, "exercise", "pushup") == "pushup" else
             f"SQUAT depth {depth}  knee {an.knee or 0:.0f}  lean {an.lean or 0:.0f}  {an.state}" if getattr(an, "exercise", "") == "squat" else
             f"{an.exercise.upper()} {an.state}  " + "  ".join(f"{k} {v}" for k, v in an.metrics().items() if isinstance(v, (int, float)) and not isinstance(v, bool)),
             (255, 255, 255), 0.8),
        ]
        if an.reps:
            r = an.reps[-1]
            txt = f"last: TUT {r.tut:.1f}s " + ("OK" if r.valid else "BAD") + (" " + ",".join(r.issues) if r.issues else "")
            lines.append((txt, (0, 255, 255) if r.valid else (0, 0, 255), 0.8))
        hub = None if web else self.hub
        if label:
            lines.append((label, (0, 215, 255), 0.7))
        if hub is not None:
            res = hub.last_result
            if res and time.time() - hub.last_result_time < 10:
                rk = res.get("rank", {})
                txt = f"{hub.player}: +{res.get('xp', 0)} XP   total {res.get('total_xp', 0)}  [{rk.get('name', '')}]"
                if res.get("rank_up"):
                    txt += "  RANK UP!"
                lines.append((txt, (0, 215, 255), 0.8))
            else:
                lines.append((f"player: {hub.player}", (200, 200, 200), 0.6))
        y = int(45 * s)
        cv2.rectangle(img, (0, 0), (w, int((25 + 40 * len(lines)) * s)), (0, 0, 0), -1)
        for text, color, scale in lines:
            cv2.putText(img, text, (int(10 * s), y), cv2.FONT_HERSHEY_SIMPLEX, scale * s, color, th)
            y += int(40 * s)
