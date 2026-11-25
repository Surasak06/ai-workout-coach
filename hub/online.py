"""
เปิด HUB ให้เพื่อนที่อยู่คนละที่เข้าได้ ผ่าน Cloudflare Quick Tunnel (ฟรี ไม่ต้องสมัคร)
- ถ้ายังไม่มีโปรแกรม cloudflared จะดาวน์โหลดมาไว้ในโฟลเดอร์ hub ให้เอง (ครั้งแรกครั้งเดียว)
- ได้ลิงก์ https://xxxx.trycloudflare.com ส่งให้เพื่อนเปิดได้เลย (ลิงก์เปลี่ยนทุกครั้งที่เปิดใหม่)
ใช้ผ่าน start_online.bat
"""
import json
import os
import re
import shutil
import subprocess
import sys
import time
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
LOCAL_HUB = "http://127.0.0.1:5000"
DOWNLOAD_URL = "https://github.com/cloudflare/cloudflared/releases/latest/download/cloudflared-windows-amd64.exe"


def find_cloudflared():
    exe = "cloudflared.exe" if os.name == "nt" else "cloudflared"
    candidates = [
        shutil.which("cloudflared"),
        os.path.join(HERE, exe),
        r"C:\Program Files (x86)\cloudflared\cloudflared.exe",
        r"C:\Program Files\cloudflared\cloudflared.exe",
        os.path.expandvars(r"%LOCALAPPDATA%\Microsoft\WinGet\Links\cloudflared.exe"),
    ]
    for c in candidates:
        if c and os.path.exists(c):
            return c
    if os.name != "nt":
        return None
    target = os.path.join(HERE, exe)
    print("ยังไม่มี cloudflared -> กำลังดาวน์โหลด (ประมาณ 60 MB ครั้งแรกครั้งเดียว)...")
    try:
        urllib.request.urlretrieve(DOWNLOAD_URL, target)
        return target
    except Exception as e:
        print("ดาวน์โหลดไม่สำเร็จ:", e)
        print("ติดตั้งเองได้ด้วยคำสั่ง:  winget install Cloudflare.cloudflared")
        return None


def tell_hub(url):
    for _ in range(10):
        try:
            req = urllib.request.Request(LOCAL_HUB + "/api/public_url", data=json.dumps({"url": url}).encode(),
                                         headers={"Content-Type": "application/json"}, method="POST")
            urllib.request.urlopen(req, timeout=2)
            return True
        except Exception:
            time.sleep(1)
    return False


def banner(url):
    line = "=" * 64
    print("\n" + line)
    print("  ลิงก์สำหรับส่งให้เพื่อน (เปิดได้จากทุกที่ ทุก Wi-Fi):")
    print()
    print("     " + url)
    print()
    print("  - เพื่อนเปิดลิงก์ -> แท็บ ออกกำลังกาย -> ใส่ชื่อ -> เริ่ม (ไม่มีคำเตือน กล้องเปิดได้)")
    print("  - ลิงก์จะใช้ได้จนกว่าจะปิดหน้าต่างนี้ เปิดใหม่ = ได้ลิงก์ใหม่")
    print("  - อย่าโพสต์ลิงก์ในที่สาธารณะ (ใครมีลิงก์ก็เข้าได้)")
    print(line + "\n", flush=True)


def main():
    cf = find_cloudflared()
    if not cf:
        input("กด Enter เพื่อปิด")
        return
    print("กำลังขอลิงก์ออนไลน์จาก Cloudflare... (รอประมาณ 5-15 วินาที)", flush=True)
    proc = subprocess.Popen([cf, "tunnel", "--no-autoupdate", "--protocol", "http2", "--url", LOCAL_HUB],
                            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, encoding="utf-8",
                            errors="replace")
    url = None
    try:
        for line in proc.stdout:
            if url is None:
                m = re.search(r"https://[a-z0-9-]+\.trycloudflare\.com", line)
                if m:
                    url = m.group(0)
                    with open(os.path.join(HERE, "online_link.txt"), "w", encoding="utf-8") as f:
                        f.write(url + "\n")
                    tell_hub(url)
                    banner(url)
                    print("(ข้อความด้านล่างเป็น log ของ Cloudflare ไม่ต้องสนใจ ถ้าไม่มีคำว่า ERR ติดต่อกันนาน ๆ)")
                elif "ERR" in line or "error" in line.lower():
                    print(line.rstrip())
            elif " ERR " in line:
                print(line.rstrip())
    except KeyboardInterrupt:
        pass
    finally:
        proc.terminate()
        tell_hub(None)


if __name__ == "__main__":
    main()
