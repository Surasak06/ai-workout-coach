"""
AI Home Workout Coach - โปรแกรมหลัก

วิธีรัน:
    python main.py                       # เว็บแคม, ไม่ต่อแขนกล
    python main.py --serial COM3         # ต่อ ESP32 ที่ COM3
    python main.py --source test.mp4     # ทดสอบกับคลิปวิดีโอ
    python main.py --no-voice            # ปิดเสียง

ปุ่มลัดตอนรัน:  q = จบเซตและบันทึก   r = เริ่มนับใหม่   c = กล้องกลับกึ่งกลาง
"""

import argparse
import time

import cv2

import config
from pose_source import SKELETON, create_pose_source
from pushup import PushUpAnalyzer
from stats_logger import StatsLogger
from tracker import PanTiltTracker
from voice import Voice

ISSUE_TEXT = {"shallow": "Go lower!", "body": "Keep body straight!", "fast": "Too fast!"}


def draw(frame, kps, an, tracker, target, fps, msg):
    h, w = frame.shape[:2]
    ok = lambda n: n in kps and kps[n][2] >= config.MIN_KEYPOINT_CONF
    for a, b in SKELETON:
        if ok(a) and ok(b):
            pa, pb = tuple(map(int, kps[a][:2])), tuple(map(int, kps[b][:2]))
            cv2.line(frame, pa, pb, (0, 255, 120), 2)
    for n, (x, y, c) in kps.items():
        if c >= config.MIN_KEYPOINT_CONF:
            cv2.circle(frame, (int(x), int(y)), 4, (0, 200, 255), -1)

    # เป้าที่กล้องตาม + เส้นกลางภาพ
    cv2.drawMarker(frame, (w // 2, h // 2), (120, 120, 120), cv2.MARKER_CROSS, 20, 1)
    if target:
        cv2.circle(frame, tuple(map(int, target)), 8, (255, 80, 80), 2)

    # แผงข้อมูล
    cv2.rectangle(frame, (0, 0), (w, 70), (20, 20, 20), -1)
    cv2.putText(frame, f"REPS {an.good_reps}", (10, 45), cv2.FONT_HERSHEY_SIMPLEX, 1.3, (0, 255, 120), 3)
    cv2.putText(frame, f"bad {an.attempts - an.good_reps}", (230, 30), cv2.FONT_HERSHEY_SIMPLEX, 0.6, (80, 80, 255), 2)
    elbow = f"{an.elbow:.0f}" if an.elbow is not None else "-"
    body = f"{an.body:.0f}" if an.body is not None else "-"
    cv2.putText(frame, f"elbow {elbow}  body {body}  {an.state}", (230, 55),
                cv2.FONT_HERSHEY_SIMPLEX, 0.55, (230, 230, 230), 1)
    last_tut = f"TUT {an.reps[-1].tut:.1f}s" if an.reps else "TUT -"
    cv2.putText(frame, last_tut, (w - 150, 30), cv2.FONT_HERSHEY_SIMPLEX, 0.6, (255, 220, 0), 2)
    cv2.putText(frame, f"pan {tracker.pan:.0f} tilt {tracker.tilt:.0f}  {fps:.0f}fps", (w - 230, 55),
                cv2.FONT_HERSHEY_SIMPLEX, 0.45, (180, 180, 180), 1)
    if msg:
        cv2.putText(frame, msg, (10, h - 20), cv2.FONT_HERSHEY_SIMPLEX, 0.9, (0, 0, 255), 2)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--source", default=str(config.CAMERA_INDEX), help="เลขกล้อง หรือ path ไฟล์วิดีโอ")
    ap.add_argument("--serial", default=config.SERIAL_PORT, help="พอร์ต ESP32 เช่น COM3")
    ap.add_argument("--no-voice", action="store_true")
    args = ap.parse_args()

    is_file = not args.source.isdigit()
    cap = cv2.VideoCapture(args.source if is_file else int(args.source))
    if not is_file:
        cap.set(cv2.CAP_PROP_FRAME_WIDTH, config.FRAME_WIDTH)
        cap.set(cv2.CAP_PROP_FRAME_HEIGHT, config.FRAME_HEIGHT)
    if not cap.isOpened():
        raise SystemExit(f"เปิดกล้อง/ไฟล์ {args.source} ไม่ได้")

    pose = create_pose_source()
    analyzer = PushUpAnalyzer()
    tracker = PanTiltTracker(args.serial)
    voice = Voice(enabled=not args.no_voice and config.VOICE_ENABLED)
    logger = StatsLogger("pushup")

    voice.say("start")
    msg, msg_until = "", 0.0
    lost_since = None
    t_prev, fps = time.time(), 0.0

    while True:
        ok, frame = cap.read()
        if not ok:
            break
        # คลิปวิดีโอใช้เวลาของคลิป (TUT จะถูกต้องแม้ประมวลผลช้า), กล้องใช้เวลาจริง
        t = cap.get(cv2.CAP_PROP_POS_MSEC) / 1000.0 if is_file else time.time()

        kps = pose.detect(frame)
        events = analyzer.update(kps, t)
        h, w = frame.shape[:2]
        target = tracker.update(kps, w, h)

        for kind, rep in events:
            if kind != "rep":
                continue
            logger.log_rep(rep)
            if rep.valid:
                voice.say("count", n=analyzer.good_reps)
            for issue in rep.issues:
                voice.say(issue)
            if rep.issues:
                msg, msg_until = ISSUE_TEXT[rep.issues[0]], time.time() + 2

        # เตือนเมื่อมองไม่เห็นคนเกิน 3 วินาที
        if target is None:
            lost_since = lost_since or time.time()
            if time.time() - lost_since > 3:
                voice.say("lost")
                lost_since = time.time()
        else:
            lost_since = None

        now = time.time()
        fps = 0.9 * fps + 0.1 * (1.0 / max(now - t_prev, 1e-6))
        t_prev = now
        draw(frame, kps, analyzer, tracker, target, fps, msg if now < msg_until else "")
        cv2.imshow("AI Home Workout Coach", frame)

        key = cv2.waitKey(1) & 0xFF
        if key == ord("q"):
            break
        if key == ord("r"):
            analyzer.reset()
        if key == ord("c"):
            tracker.center()

    summary = analyzer.summary()
    if summary["attempts"] > 0:
        row = logger.log_session(summary)
        print("\n===== สรุปเซต =====")
        for k, v in row.items():
            print(f"{k:>12}: {v}")
        voice.say("done", n=summary["good_reps"])
        time.sleep(2.5)  # รอพูดจบ

    cap.release()
    cv2.destroyAllWindows()
    tracker.close()


if __name__ == "__main__":
    main()
