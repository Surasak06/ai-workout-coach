/*
  AI Home Workout Coach - ESP32 Pan-Tilt controller

  รับคำสั่งจาก PC ทาง USB Serial (115200) ทีละบรรทัด รูปแบบ JSON:
      {"pan": 92, "tilt": 85}
  แล้วขยับเซอร์โว MG996R 2 ตัวไปยังมุมนั้นแบบนุ่มนวล (ไม่กระชาก)

  ไลบรารีที่ต้องติดตั้งใน Arduino IDE:  ESP32Servo  (by Kevin Harrington)
  บอร์ด: ESP32 Dev Module

  การต่อสาย (ดู README):
    Servo Pan  สายสัญญาณ (ส้ม/เหลือง) -> GPIO 18
    Servo Tilt สายสัญญาณ (ส้ม/เหลือง) -> GPIO 19
    สายแดงของเซอร์โวทั้งสอง -> +5V จากแหล่งจ่ายแยก (อย่างน้อย 3A)
    สายน้ำตาล/ดำของเซอร์โว  -> GND ของแหล่งจ่ายแยก
    GND ของแหล่งจ่ายแยก     -> GND ของ ESP32  (ต้องต่อกราวด์ร่วม!)
*/

#include <ESP32Servo.h>

const int PAN_PIN = 18;
const int TILT_PIN = 19;

const int PAN_MIN = 20, PAN_MAX = 160;     // ต้องตรงกับ config.py
const int TILT_MIN = 50, TILT_MAX = 130;

const int STEP_DEG = 2;          // ขยับครั้งละกี่องศา
const int STEP_INTERVAL_MS = 15; // ทุกกี่มิลลิวินาที (ยิ่งน้อยยิ่งเร็ว)

Servo panServo, tiltServo;
float panNow = 90, tiltNow = 90;
int panTarget = 90, tiltTarget = 90;
unsigned long lastStep = 0, lastCmd = 0;
String buf;

int readInt(const String &s, const char *key, int fallback) {
  int i = s.indexOf(key);
  if (i < 0) return fallback;
  i = s.indexOf(':', i);
  if (i < 0) return fallback;
  return s.substring(i + 1).toInt();
}

void handleLine(const String &line) {
  if (line.indexOf("pan") < 0 && line.indexOf("tilt") < 0) return;
  panTarget = constrain(readInt(line, "\"pan\"", panTarget), PAN_MIN, PAN_MAX);
  tiltTarget = constrain(readInt(line, "\"tilt\"", tiltTarget), TILT_MIN, TILT_MAX);
  lastCmd = millis();
}

float stepToward(float now, int target) {
  if (abs(target - now) <= STEP_DEG) return target;
  return now + (target > now ? STEP_DEG : -STEP_DEG);
}

void setup() {
  Serial.begin(115200);
  ESP32PWM::allocateTimer(0);
  ESP32PWM::allocateTimer(1);
  panServo.setPeriodHertz(50);
  tiltServo.setPeriodHertz(50);
  panServo.attach(PAN_PIN, 500, 2400);   // MG996R ช่วงพัลส์ประมาณ 500-2400 us
  tiltServo.attach(TILT_PIN, 500, 2400);
  panServo.write(panNow);
  tiltServo.write(tiltNow);
  Serial.println("{\"status\":\"ready\"}");
}

void loop() {
  // อ่านข้อมูลทีละตัวอักษร จนเจอขึ้นบรรทัดใหม่
  while (Serial.available()) {
    char c = Serial.read();
    if (c == '\n') {
      handleLine(buf);
      buf = "";
    } else if (buf.length() < 64) {
      buf += c;
    }
  }

  if (millis() - lastStep >= STEP_INTERVAL_MS) {
    lastStep = millis();
    panNow = stepToward(panNow, panTarget);
    tiltNow = stepToward(tiltNow, tiltTarget);
    panServo.write((int)panNow);
    tiltServo.write((int)tiltNow);
  }
}
