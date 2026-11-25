"""
แหล่งที่มาของ keypoints (ข้อต่อร่างกาย)

ทุก backend ต้องคืนค่าเป็น dict รูปแบบเดียวกัน:
    {"left_shoulder": (x, y, conf), "left_elbow": (x, y, conf), ...}
x, y เป็นพิกัดพิกเซลในภาพ, conf คือความมั่นใจ 0-1
ใช้ชื่อข้อต่อแบบ COCO 17 จุด ซึ่งเป็นมาตรฐานที่โมเดล pose ส่วนใหญ่ใช้

โค้ดส่วนวิเคราะห์ท่า (pushup.py) ไม่สนว่า keypoints มาจากไหน
=> ตอนนี้ทดสอบด้วย YOLO ไปก่อน แล้วค่อยสลับเป็น CiRA CORE โดยแก้แค่ไฟล์นี้
"""

import config

COCO_NAMES = [
    "nose", "left_eye", "right_eye", "left_ear", "right_ear",
    "left_shoulder", "right_shoulder", "left_elbow", "right_elbow",
    "left_wrist", "right_wrist", "left_hip", "right_hip",
    "left_knee", "right_knee", "left_ankle", "right_ankle",
]

SKELETON = [
    ("left_shoulder", "right_shoulder"), ("left_shoulder", "left_elbow"),
    ("left_elbow", "left_wrist"), ("right_shoulder", "right_elbow"),
    ("right_elbow", "right_wrist"), ("left_shoulder", "left_hip"),
    ("right_shoulder", "right_hip"), ("left_hip", "right_hip"),
    ("left_hip", "left_knee"), ("left_knee", "left_ankle"),
    ("right_hip", "right_knee"), ("right_knee", "right_ankle"),
]


class YoloPose:
    """ตัวทดสอบระหว่างยังไม่ได้เชื่อม CiRA CORE (pip install ultralytics)"""

    def __init__(self, model_path=config.YOLO_POSE_MODEL):
        from ultralytics import YOLO
        import torch
        self.model = YOLO(model_path)   # ครั้งแรกจะดาวน์โหลดไฟล์โมเดลอัตโนมัติ (~6 MB)
        # ใช้การ์ดจอ NVIDIA ถ้า PyTorch รุ่น CUDA ติดตั้งไว้ ไม่งั้นใช้ CPU
        if torch.cuda.is_available():
            self.device = 0
            self.device_name = "GPU: " + torch.cuda.get_device_name(0)
        else:
            self.device = "cpu"
            self.device_name = f"CPU (torch {torch.__version__})"
        print("[pose] ประมวลผลด้วย", self.device_name)

    def detect(self, frame):
        # conf= ให้ YOLO ตัดกรอบที่ไม่มั่นใจว่าเป็นคนทิ้งตั้งแต่แรก
        r = self.model(frame, verbose=False, device=self.device, conf=config.PERSON_MIN_CONF)[0]
        if r.keypoints is None or len(r.keypoints.xy) == 0 or r.boxes is None or len(r.boxes) == 0:
            return {}
        h, w = frame.shape[:2]
        min_area = config.PERSON_MIN_AREA * w * h
        best, best_area = None, 0
        for i in range(len(r.boxes)):
            bw, bh = r.boxes.xywh[i, 2].item(), r.boxes.xywh[i, 3].item()
            kc = r.keypoints.conf[i] if r.keypoints.conf is not None else None
            visible = int((kc >= config.MIN_KEYPOINT_CONF).sum().item()) if kc is not None else 17
            # ต้องผ่านทั้ง 3 ข้อถึงจะนับว่า "เป็นคนจริง": มั่นใจพอ (กรองแล้วข้างบน), ใหญ่พอ, เห็นข้อต่อพอ
            if bw * bh < min_area or visible < config.PERSON_MIN_KEYPOINTS:
                continue
            if bw * bh > best_area:          # ถ้ามีหลายคน เลือกคนที่กรอบใหญ่สุด (น่าจะเป็นผู้ฝึก)
                best, best_area = i, bw * bh
        if best is None:
            return {}
        idx = best
        xy = r.keypoints.xy[idx].tolist()
        conf = r.keypoints.conf[idx].tolist() if r.keypoints.conf is not None else [1.0] * 17
        return {name: (xy[i][0], xy[i][1], conf[i]) for i, name in enumerate(COCO_NAMES)}


class CiraPose:
    """
    ช่องสำหรับเชื่อม CiRA CORE  (ยังเป็นโครง ต้องเติมตามรูปแบบข้อมูลจริงของ CiRA)

    แนวทางที่เป็นไปได้ 2 แบบ (ต้องดูว่า CiRA CORE เวอร์ชันที่ใช้ทำแบบไหนได้):
      ก) เขียน logic ทั้งหมดใน Python node ของ CiRA CORE เอง
         -> ใน node นั้นแปลงผล pose ให้เป็น dict ตามรูปแบบด้านบน แล้วเรียก
            PushUpAnalyzer.update(kps, t) ได้เลย
      ข) ให้ CiRA CORE ส่ง keypoints ออกมาทาง UDP/TCP/MQTT/ไฟล์ เป็น JSON
         -> คลาสนี้รับ JSON นั้นแล้วแปลงเป็น dict

    ด้านล่างเป็นตัวอย่างแบบ (ข) รับ JSON ทาง UDP พอร์ต 5005 รูปแบบสมมติ:
        {"keypoints": [[x, y, conf], ... 17 จุดเรียงแบบ COCO]}
    """

    def __init__(self, host="127.0.0.1", port=5005):
        import socket
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.sock.bind((host, port))
        self.sock.setblocking(False)
        self.last = {}

    def detect(self, frame=None):
        import json
        try:
            while True:  # อ่านจนถึงข้อความล่าสุด ทิ้งของเก่า
                data, _ = self.sock.recvfrom(65535)
                msg = json.loads(data.decode("utf-8"))
                pts = msg["keypoints"]
                self.last = {n: tuple(pts[i][:3]) for i, n in enumerate(COCO_NAMES)}
        except BlockingIOError:
            pass
        except (ValueError, KeyError, IndexError) as e:
            print("CiRA data format error:", e)
        return self.last


def create_pose_source():
    if config.POSE_BACKEND == "cira":
        return CiraPose()
    return YoloPose()
