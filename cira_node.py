# =====================================================================
#  AI Home Workout Coach - โค้ดสำหรับวางใน Python+ node ของ CiRA CORE
#
#  Flow:  [Button Run] -> [VideoFile หรือ กล้อง] -> [Python+ (โค้ดนี้)] -> [Debug]
#  ช่อง "Not use image" ต้องไม่ติ๊ก
#
#  วางครั้งเดียว แก้แค่ PROJECT_DIR ให้ชี้ไปโฟลเดอร์ที่มีไฟล์ config.py
#  ต่อจากนี้ถ้าอัปเดตโปรแกรม แค่วางไฟล์ใหม่ทับในโฟลเดอร์ แล้วกด Stop -> Start ที่ Python+
#  ค่าที่ปรับบ่อย (เกณฑ์ท่า, ชื่อผู้เล่น, HUB_URL, พอร์ต ESP32) อยู่ใน config.py
# =====================================================================
import sys
import importlib

# <- โฟลเดอร์ที่มี config.py, pushup.py, cira_app.py, hub_client.py
PROJECT_DIR = r"C:\path\to\ai-workout-coach"   # <- แก้เป็นโฟลเดอร์โปรเจคในเครื่องของคุณ

if PROJECT_DIR not in sys.path:
    sys.path.insert(0, PROJECT_DIR)

# โหลดไฟล์ในโฟลเดอร์ใหม่ทุกครั้งที่กด Start (แก้ไฟล์แล้วมีผลทันที ไม่ต้องปิด CiRA)
import config, pose_source, pushup, tracker, voice, stats_logger, cira_app
for _m in (config, pose_source, pushup, tracker, voice, stats_logger, cira_app):
    importlib.reload(_m)

# สร้างตัวประมวลผลครั้งเดียวตอนกด Start
# (ถ้ามีตัวเก่าค้างอยู่ จะบันทึกเซตเก่า + ส่งคะแนนไป HUB ให้ก่อน)
app = cira_app.CoachApp(PROJECT_DIR)


# CiRA เรียกฟังก์ชันนี้ทุกเฟรม
def handle_request(req):
    payload, img = getReqData(req)
    payload, img = app.process(payload, img)   # หาท่า -> นับครั้ง -> วาดบนภาพ -> ส่งสถานะไป HUB
    return makeRespData(payload, img)
