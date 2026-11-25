"""
AI Workout Coach - HUB (ศูนย์กลางคะแนน)

รันบนคอมเครื่องเดียว (เช่นโน้ตบุ๊กของคุณ) แล้ว:
  - CiRA ของทุกคน (คนละเครื่องได้) ส่งผลแต่ละเซตมาที่นี่
  - มือถือ/คอมทุกเครื่องใน Wi-Fi เดียวกัน เปิดหน้าเว็บดูอันดับ โปรไฟล์ และคนที่กำลังเล่นอยู่สด ๆ

วิธีรัน:
    pip install flask
    python server.py
แล้วเปิดลิงก์ที่โปรแกรมพิมพ์ออกมา (เช่น http://192.168.1.23:5000) จากมือถือ
"""

import hashlib
from collections import deque
import json
import os
import secrets
import random
import socket
import sqlite3
import threading
import time
from datetime import date, datetime, timedelta, timezone

from flask import Response, Flask, jsonify, request, send_from_directory

HERE = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.path.join(HERE, "coach.db")
PORT = 5000

# ===================== กติกาคะแนน (ปรับได้) =====================
XP_PER_GOOD_REP = 10        # ทำถูก 1 ครั้ง
XP_CONTROL_BONUS = 5        # ทำถูก + ช้าคุมจังหวะ (TUT อยู่ในช่วง) ได้เพิ่ม
CONTROL_TUT = (2.0, 4.0)    # ช่วง TUT ที่ถือว่าคุมจังหวะดี (วินาที)
XP_PERFECT_SET = 20         # ทั้งเซตทำถูก >= 90% และอย่างน้อย 5 ครั้ง
DAILY_GOAL = 30             # เป้าหมายรายวัน (ครั้งที่ทำถูก)
XP_DAILY_GOAL = 50          # โบนัสเมื่อถึงเป้าวันนั้นครั้งแรก

# ---- 2 ระบบแยกกัน ----
# 1) เลเวล (Lv.) มาจาก EXP ที่ได้ตอนฝึก/เล่นเกม : Lv.n ต้องใช้ EXP เพิ่ม 100 x n  (Lv2=100, Lv3=300, Lv4=600, Lv5=1000 ...)
# 2) แรงค์ มาจาก "แต้มแรงค์" (RP) ที่ได้/เสียจากการแข่งกับผู้เล่นอื่น: ดวล (ชนะ/เสมอ/แพ้) + คะแนนเกม
RANKS = [                   # (ชื่อแรงค์, RP ขั้นต่ำ, สี)
    ("Bronze", 0, "#c98a4b"),
    ("Silver", 100, "#a9b4c2"),
    ("Gold", 250, "#f2c230"),
    ("Platinum", 450, "#4fd1c5"),
    ("Diamond", 700, "#8b7cf6"),
]
RP_WIN, RP_DRAW, RP_LOSS = 25, 5, -15      # ดวล
RP_GAME_MAX = 20                           # เกม: ได้สูงสุดต่อรอบ

EXERCISE_NAMES = {"pushup": "วิดพื้น", "squat": "สควอท", "plank": "แพลงก์", "game": "เกมเลนท่าทาง"}
# ท่าไหนฝึกกล้ามเนื้ออะไร (น้ำหนัก 0-1) ใช้ทำแผนที่กล้ามเนื้อรายสัปดาห์
# ค่า MET จาก 2024 Adult Compendium of Physical Activities
#   02020 calisthenics (pushups ...) vigorous = 7.5 ; 02052 resistance training, squats = 5.0
MET = {"pushup": 7.5, "squat": 5.0, "plank": 2.8}
# ระดับสัตว์: คะแนน = จำนวนครั้งที่ทำถูกต่อนาที (สูงสุด 30) x (คะแนนท่า / 100)^2  ทำเร็วแต่ท่าเสียก็ไม่ได้สัตว์ระดับสูง
ANIMALS = [  # (key, ชื่อ, คำอธิบาย, emoji, คะแนนขั้นต่ำ)
    ("turtle", "เต่า", "ช้าแต่มั่นคง ค่อย ๆ เพิ่มจังหวะได้", "🐢", 0),
    ("panda", "แพนด้า", "กำลังอุ่นเครื่อง ยังเพิ่มได้อีกเยอะ", "🐼", 6),
    ("cat", "แมว", "คล่องแคล่ว ท่าเริ่มนิ่ง", "🐈", 10),
    ("wolf", "หมาป่า", "อึดและแม่น ทำได้ต่อเนื่อง", "🐺", 14),
    ("tiger", "เสือ", "ทรงพลัง เร็วและท่าดี", "🐅", 18),
    ("cheetah", "ชีตาห์", "เร็วและเป๊ะ ระดับสูงสุด", "🐆", 22),
]


def animal_of(s):
    """คืนระดับสัตว์ของเซตนี้ (ต้องมีอย่างน้อย 3 ครั้ง)"""
    reps = s.get("reps") or []
    timed = [r for r in reps if r.get("t1") and r.get("t0") is not None]
    if s["attempts"] < 3 or len(timed) < 3:
        return None
    minutes = max(1e-6, (timed[-1]["t1"] - timed[0]["t0"]) / 60)
    pace = s["good"] / minutes
    # เร็วเกิน 30 ครั้ง/นาที ไม่ได้คะแนนเพิ่ม (ไม่ใช่การควบคุมท่า) และท่าไม่ดีถูกลดหนักขึ้น (ยกกำลังสอง)
    score = round(min(pace, 30) * (s["form_score"] / 100) ** 2, 1)
    idx = max(i for i, a in enumerate(ANIMALS) if score >= a[4])
    k, name, desc, emo, _ = ANIMALS[idx]
    out = {"key": k, "name": name, "desc": desc, "emoji": emo, "score": score, "pace": round(pace, 1), "level": idx + 1}
    if idx + 1 < len(ANIMALS):
        nk, nname, _, nemo, nmin = ANIMALS[idx + 1]
        out["next"] = {"key": nk, "name": nname, "emoji": nemo, "need": round(nmin - score, 1)}
    return out


LEVEL_NAMES = {"beginner": "มือใหม่", "intermediate": "ปานกลาง", "advanced": "คล่องแล้ว"}
EXERCISE_MUSCLES = {
    "pushup": {"chest": 1.0, "triceps": 0.7, "shoulders": 0.6, "abs": 0.3},
    "squat": {"quads": 1.0, "glutes": 0.8, "hams": 0.4},
    "plank": {"abs": 1.0, "shoulders": 0.3},
}
LIVE_TIMEOUT = 8            # ไม่ได้ข่าวจากผู้เล่นเกินกี่วินาที ถือว่าเลิกเล่นแล้ว

app = Flask(__name__, static_folder=os.path.join(HERE, "static"))
db_lock = threading.Lock()
live = {}                   # player -> {"ts":..., ...สถานะล่าสุด}


# ===================== ฐานข้อมูล =====================
def db():
    con = sqlite3.connect(DB_PATH)
    con.row_factory = sqlite3.Row
    return con


def init_db():
    with db() as con:
        con.executescript("""
        CREATE TABLE IF NOT EXISTS sessions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            player TEXT NOT NULL,
            exercise TEXT NOT NULL DEFAULT 'pushup',
            day TEXT NOT NULL,
            ts TEXT NOT NULL,
            client_id TEXT UNIQUE,
            good INTEGER, attempts INTEGER, bad INTEGER,
            avg_tut REAL, shallow INTEGER, body INTEGER, fast INTEGER,
            xp INTEGER, xp_detail TEXT
        );
        CREATE INDEX IF NOT EXISTS idx_sessions_player ON sessions(player);
        CREATE INDEX IF NOT EXISTS idx_sessions_day ON sessions(day);
        CREATE TABLE IF NOT EXISTS users (
            name TEXT PRIMARY KEY, pin_hash TEXT NOT NULL, salt TEXT NOT NULL, created TEXT,
            weight REAL, height REAL, age INTEGER, sex TEXT, level TEXT
        );
        CREATE TABLE IF NOT EXISTS tokens (token TEXT PRIMARY KEY, name TEXT NOT NULL, created TEXT);
        CREATE TABLE IF NOT EXISTS games (
            id INTEGER PRIMARY KEY AUTOINCREMENT, ts TEXT NOT NULL, day TEXT NOT NULL, player TEXT NOT NULL,
            game TEXT, score INTEGER, hits INTEGER, miss INTEGER, best_combo INTEGER, xp INTEGER, rp INTEGER
        );
        CREATE TABLE IF NOT EXISTS duels (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            ts TEXT NOT NULL, day TEXT NOT NULL,
            p1 TEXT, p2 TEXT, s1 INTEGER, s2 INTEGER, f1 INTEGER, f2 INTEGER,
            winner TEXT
        );
        """)
        cols = [r[1] for r in con.execute("PRAGMA table_info(sessions)")]
        if "form" not in cols:      # ฐานข้อมูลเก่า: เพิ่มคอลัมน์คะแนนท่า
            con.execute("ALTER TABLE sessions ADD COLUMN form INTEGER DEFAULT 0")
        if "kcal" not in cols:      # ฐานข้อมูลเก่า: เพิ่มคอลัมน์แคลอรี่
            con.execute("ALTER TABLE sessions ADD COLUMN kcal REAL")
        for c in ("hr_avg", "hr_max"):
            if c not in cols:
                con.execute(f"ALTER TABLE sessions ADD COLUMN {c} INTEGER")
        if "animal" not in cols:
            con.execute("ALTER TABLE sessions ADD COLUMN animal TEXT")
        ucols = [r[1] for r in con.execute("PRAGMA table_info(users)")]
        if "avatar" not in ucols:
            con.execute("ALTER TABLE users ADD COLUMN avatar TEXT")
        dcols = [r[1] for r in con.execute("PRAGMA table_info(duels)")]
        if "exercise" not in dcols:
            con.execute("ALTER TABLE duels ADD COLUMN exercise TEXT DEFAULT 'pushup'")


# ===================== คำนวณ =====================
def rank_of(xp):
    cur = RANKS[0]
    nxt = None
    for i, r in enumerate(RANKS):
        if xp >= r[1]:
            cur = r
            nxt = RANKS[i + 1] if i + 1 < len(RANKS) else None
    progress = 1.0 if nxt is None else (xp - cur[1]) / (nxt[1] - cur[1])
    return {
        "name": cur[0], "color": cur[2], "min": cur[1],
        "next": nxt[0] if nxt else None, "next_xp": nxt[1] if nxt else None,
        "progress": round(progress, 3),
    }


def level_of(xp):
    lv, need, base = 1, 100, 0
    while xp >= base + need:
        base += need
        lv += 1
        need = 100 * lv
    return {"level": lv, "xp": xp, "min": base, "next_xp": base + need, "progress": round((xp - base) / need, 3)}


def rp_table(with_last=False):
    """แต้มแรงค์ของทุกคน: ไล่ตามเวลา ดวล (ชนะ/เสมอ/แพ้) + เกม ; ไม่ติดลบ"""
    with db() as con:
        ev = [(r["ts"], r["p1"], r["p2"], r["winner"], None) for r in con.execute("SELECT ts, p1, p2, winner FROM duels")]
        ev += [(r["ts"], r["player"], None, None, r["rp"]) for r in con.execute("SELECT ts, player, rp FROM games")]
    rp, last = {}, {}
    for ts, a, b, win, grp in sorted(ev, key=lambda e: e[0]):
        if grp is not None:
            rp[a] = rp.get(a, 0) + grp
            last[a] = grp
            continue
        for me, other in ((a, b), (b, a)):
            d = RP_DRAW if win == "" else RP_WIN if win == me else RP_LOSS
            new = max(0, rp.get(me, 0) + d)
            last[me] = new - rp.get(me, 0)
            rp[me] = new
    return (rp, last) if with_last else rp


def compute_xp(s, good_today_before):
    """คืนค่า (xp, รายการที่มาของคะแนน)"""
    good, attempts = s["good"], s["attempts"]
    detail = []
    base = XP_PER_GOOD_REP * good
    if base:
        detail.append([f"ทำถูก {good} ครั้ง", base])

    reps = s.get("reps") or []
    if reps:
        controlled = sum(1 for r in reps if r.get("valid") and CONTROL_TUT[0] <= r.get("tut", 0) <= CONTROL_TUT[1])
    else:
        controlled = good if CONTROL_TUT[0] <= s.get("avg_tut", 0) <= CONTROL_TUT[1] else 0
    if controlled:
        detail.append([f"คุมจังหวะดี {controlled} ครั้ง", XP_CONTROL_BONUS * controlled])

    if good >= 5 and attempts and good / attempts >= 0.9:
        detail.append(["ท่าเป๊ะทั้งเซต", XP_PERFECT_SET])

    if good_today_before < DAILY_GOAL <= good_today_before + good:
        detail.append([f"ถึงเป้าวันนี้ {DAILY_GOAL} ครั้ง", XP_DAILY_GOAL])

    for name, v in s.get("bonus") or []:          # เช่น ชนะดวล
        detail.append([name, v])

    return sum(v for _, v in detail), detail


def streak_of(days):
    """จำนวนวันติดกันที่มีการฝึก นับถึงวันนี้ (หรือถึงเมื่อวาน ถ้าวันนี้ยังไม่ได้ฝึก)"""
    days = set(days)
    d = date.today()
    if d.isoformat() not in days:
        d -= timedelta(days=1)
    n = 0
    while d.isoformat() in days:
        n += 1
        d -= timedelta(days=1)
    return n


def badges_of(rows, streak):
    total_good = sum(r["good"] for r in rows)
    best_set = max((r["good"] for r in rows), default=0)
    perfect = any(r["good"] >= 5 and r["attempts"] and r["good"] / r["attempts"] >= 0.9 for r in rows)
    by_day = {}
    for r in rows:
        by_day[r["day"]] = by_day.get(r["day"], 0) + r["good"]
    goal_days = sum(1 for v in by_day.values() if v >= DAILY_GOAL)
    return [
        # key = ชื่อไฟล์รูปเหรียญ  hub/static/img/badge/<key>.png
        {"key": "first_set", "icon": "🎯", "name": "เซตแรก", "got": len(rows) > 0},
        {"key": "perfect", "icon": "✨", "name": "ท่าเป๊ะ", "got": perfect},
        {"key": "set20", "icon": "💪", "name": "20 ครั้งในเซตเดียว", "got": best_set >= 20},
        {"key": "total100", "icon": "💯", "name": "รวม 100 ครั้ง", "got": total_good >= 100},
        {"key": "daily_goal", "icon": "🏆", "name": "ถึงเป้ารายวัน", "got": goal_days > 0},
        {"key": "streak3", "icon": "🔥", "name": "ฝึกติดกัน 3 วัน", "got": streak >= 3},
        {"key": "streak7", "icon": "⚡", "name": "ฝึกติดกัน 7 วัน", "got": streak >= 7},
        {"key": "total1000", "icon": "👑", "name": "รวม 1,000 ครั้ง", "got": total_good >= 1000},
    ]


def all_rows():
    with db() as con:
        return [dict(r) for r in con.execute("SELECT * FROM sessions ORDER BY ts")]


def leaderboard(rows, since_day=None):
    agg = {}
    for r in rows:
        if since_day and r["day"] < since_day:
            continue
        a = agg.setdefault(r["player"], {"player": r["player"], "xp": 0, "good": 0, "attempts": 0, "sets": 0,
                                         "_fs": 0, "_fn": 0})
        a["xp"] += r["xp"]
        if r.get("form"):
            a["_fs"] += r["form"]
            a["_fn"] += 1
        a["good"] += r["good"]
        a["attempts"] += r["attempts"]
        a["sets"] += 1
    out = sorted(agg.values(), key=lambda a: (-a["xp"], -a["good"]))
    for a in out:
        a["form"] = round(100 * a["good"] / a["attempts"]) if a["attempts"] else 0
        a["form_score"] = round(a.pop("_fs") / a["_fn"]) if a["_fn"] else None
        a.pop("_fn")
    return out


def elo_table():
    """(เลิกใช้แล้ว แทนด้วย rp_table) คะแนน ELO จากผลดวลทั้งหมด"""
    with db() as con:
        rows = con.execute("SELECT p1, p2, winner FROM duels ORDER BY id").fetchall()
    elo = {}
    for r in rows:
        a, b = elo.get(r["p1"], 1000.0), elo.get(r["p2"], 1000.0)
        ea = 1 / (1 + 10 ** ((b - a) / 400))
        sa = 1.0 if r["winner"] == r["p1"] else 0.5 if r["winner"] == "" else 0.0
        elo[r["p1"]], elo[r["p2"]] = a + 32 * (sa - ea), b + 32 * ((1 - sa) - (1 - ea))
    return {k: round(v) for k, v in elo.items()}


def duel_record(player):
    with db() as con:
        rows = con.execute("SELECT * FROM duels WHERE p1=? OR p2=? ORDER BY id DESC", (player, player)).fetchall()
    win = sum(1 for r in rows if r["winner"] == player)
    draw = sum(1 for r in rows if r["winner"] == "")
    recent = [{"ts": r["ts"], "opponent": r["p2"] if r["p1"] == player else r["p1"],
               "me": r["s1"] if r["p1"] == player else r["s2"], "them": r["s2"] if r["p1"] == player else r["s1"],
               "result": "win" if r["winner"] == player else "draw" if r["winner"] == "" else "loss"} for r in rows[:5]]
    return {"played": len(rows), "win": win, "draw": draw, "loss": len(rows) - win - draw, "recent": recent}


def profile(rows, player):
    mine = [r for r in rows if r["player"] == player]
    total_xp = sum(r["xp"] for r in mine)
    today = date.today().isoformat()
    streak = streak_of(r["day"] for r in mine)
    good = sum(r["good"] for r in mine)
    attempts = sum(r["attempts"] for r in mine)
    tuts = [r["avg_tut"] for r in mine if r["good"] > 0 and r["avg_tut"]]
    days = []
    for i in range(13, -1, -1):
        d = (date.today() - timedelta(days=i)).isoformat()
        dr = [r for r in mine if r["day"] == d]
        days.append({"day": d, "good": sum(r["good"] for r in dr), "xp": sum(r["xp"] for r in dr)})
    all_xp = {}
    for r in rows:
        all_xp[r["player"]] = all_xp.get(r["player"], 0) + r["xp"]
    position = sorted(all_xp.values(), reverse=True).index(total_xp) + 1 if mine else None
    rps = rp_table()
    my_rp = rps.get(player, 0)
    everyone = set(all_xp) | set(rps)
    rank_position = 1 + sum(1 for p_ in everyone if rps.get(p_, 0) > my_rp)
    recent = []
    for r in reversed(mine[-10:]):
        recent.append({
            "ts": r["ts"], "exercise": EXERCISE_NAMES.get(r["exercise"], r["exercise"]),
            "good": r["good"], "bad": r["bad"], "avg_tut": r["avg_tut"], "xp": r["xp"], "form_score": r.get("form"), "kcal": r.get("kcal"),
            "detail": json.loads(r["xp_detail"] or "[]"),
        })
    week_ago = (date.today() - timedelta(days=6)).isoformat()
    two_weeks = (date.today() - timedelta(days=13)).isoformat()
    fs_now = [r["form"] for r in mine if r["day"] >= week_ago and r.get("form")]
    fs_prev = [r["form"] for r in mine if two_weeks <= r["day"] < week_ago and r.get("form")]
    muscles = {}
    for r in mine:
        if r["day"] >= week_ago:
            for m, w in EXERCISE_MUSCLES.get(r["exercise"], {}).items():
                muscles[m] = muscles.get(m, 0) + r["good"] * w
    return {
        "player": player,
        "form_score_7d": round(sum(fs_now) / len(fs_now)) if fs_now else None,
        "form_score_prev": round(sum(fs_prev) / len(fs_prev)) if fs_prev else None,
        "muscles_week": {m: round(v) for m, v in muscles.items()},
        "xp": total_xp,
        "level": level_of(total_xp),
        "rp": my_rp,
        "rank": rank_of(my_rp),
        "position": position,
        "rank_position": rank_position,
        "players": len(everyone),
        "streak": streak,
        "today_good": sum(r["good"] for r in mine if r["day"] == today),
        "daily_goal": DAILY_GOAL,
        "total_good": good,
        "form": round(100 * good / attempts) if attempts else 0,
        "avg_tut": round(sum(tuts) / len(tuts), 1) if tuts else 0,
        "sets": len(mine),
        "best_set": max((r["good"] for r in mine), default=0),
        "days": days,
        "recent": recent,
        "badges": badges_of(mine, streak),
        "duels": duel_record(player),
        "kcal_7d": round(sum((r.get("kcal") or 0) for r in mine if r["day"] >= week_ago), 1),
        "kcal_total": round(sum((r.get("kcal") or 0) for r in mine), 1),
        "body": public_user(get_user(player)),
    }


# ===================== API =====================
def save_session(s):
    """บันทึก 1 เซต คิด XP แล้วคืน dict ผลลัพธ์ (ใช้ทั้งจาก CiRA โหมดเก่า และจากเว็บ)"""
    player = str(s.get("player", "")).strip()[:30]
    if not player:
        raise ValueError("ต้องมีชื่อผู้เล่น (player)")
    s["good"] = int(s.get("good_reps", s.get("good", 0)))
    s["attempts"] = int(s.get("attempts", s["good"]))
    s["bad"] = int(s.get("bad_reps", s["attempts"] - s["good"]))
    s["avg_tut"] = float(s.get("avg_tut", 0) or 0)
    if not s.get("form_score") and s.get("reps"):
        sc = [r.get("score", 0) for r in s["reps"]]
        s["form_score"] = round(sum(sc) / len(sc)) if sc else 0
    s["form_score"] = int(s.get("form_score", 0) or 0)
    # แคลอรี่โดยประมาณ = MET x น้ำหนัก(กก.) x เวลาออกแรง(ชม.)  เวลาออกแรง = TUT ทุกครั้ง + ช่วงเปลี่ยนท่า 1 วิ/ครั้ง
    u = get_user(player)
    animal = animal_of(s)
    hr_avg = int(s["hr_avg"]) if str(s.get("hr_avg", "")).isdigit() else None
    hr_max = int(s["hr_max"]) if str(s.get("hr_max", "")).isdigit() else None
    kcal = None
    if u and u["weight"]:
        active = sum(float(r.get("tut", 0) or 0) for r in s.get("reps") or []) or s["avg_tut"] * s["attempts"]
        active += s["attempts"] * 1.0
        kcal = round(MET.get(s.get("exercise", "pushup"), 5.0) * u["weight"] * active / 3600, 1)

    now = datetime.now()
    day = now.date().isoformat()
    with db_lock, db() as con:
        cid = s.get("client_id")
        if cid:
            old = con.execute("SELECT xp, xp_detail FROM sessions WHERE client_id=?", (cid,)).fetchone()
            if old:   # ส่งซ้ำ (เช่น ส่งค้างจากตอนเน็ตหลุด) ไม่นับซ้ำ
                return {"ok": True, "duplicate": True, "xp": old["xp"],
                        "detail": json.loads(old["xp_detail"]), **_after(all_rows(), player)}
        before = con.execute("SELECT COALESCE(SUM(good),0) FROM sessions WHERE player=? AND day=?",
                             (player, day)).fetchone()[0]
        xp, detail = compute_xp(s, before)
        old_level = level_of(con.execute("SELECT COALESCE(SUM(xp),0) FROM sessions WHERE player=?",
                                         (player,)).fetchone()[0])["level"]
        con.execute(
            "INSERT INTO sessions (player, exercise, day, ts, client_id, good, attempts, bad, avg_tut,"
            " shallow, body, fast, xp, xp_detail, form, kcal, hr_avg, hr_max, animal) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (player, s.get("exercise", "pushup"), day, now.isoformat(timespec="seconds"), cid,
             s["good"], s["attempts"], s["bad"], s["avg_tut"],
             int(s.get("shallow", 0) or 0), int(s.get("body", 0) or 0), int(s.get("fast", 0) or 0),
             xp, json.dumps(detail, ensure_ascii=False), s["form_score"], kcal, hr_avg, hr_max,
             animal["key"] if animal else None))
    after = _after(all_rows(), player)
    print(f"[hub] {player}: +{xp} XP ({s['good']} ครั้ง) -> Lv.{after['level']['level']}")
    return {"ok": True, "xp": xp, "detail": detail, "good": s["good"], "bad": s["bad"],
            "avg_tut": s["avg_tut"], "form_score": s["form_score"], "kcal": kcal, "has_weight": bool(u and u["weight"]),
            "animal": animal, "hr_avg": hr_avg,
            "animal_note": None if animal or s["attempts"] < 3 else
                           "old_cira" if not any(r.get("t1") for r in s.get("reps") or []) else "few", "hr_max": hr_max, "age": u.get("age") if u else None,
            "exercise": s.get("exercise", "pushup"), "level_up": after["level"]["level"] > old_level, **after}


@app.post("/api/session")
def api_session():
    s = request.get_json(force=True, silent=True) or {}
    try:
        return jsonify(save_session(s))
    except (TypeError, ValueError) as e:
        return jsonify({"ok": False, "error": str(e)}), 400


def _after(rows, player):
    p = profile(rows, player)
    return {"total_xp": p["xp"], "level": p["level"], "rank": p["rank"], "rp": p["rp"], "position": p["position"], "streak": p["streak"]}


@app.post("/api/live")
def api_live():
    s = request.get_json(force=True, silent=True) or {}
    player = str(s.get("player", "")).strip()[:30]
    if player:
        s["ts"] = time.time()
        live[player] = s
    return jsonify({"ok": True})


@app.get("/api/summary")
def api_summary():
    rows = all_rows()
    today = date.today()
    now = time.time()
    players = sorted({r["player"] for r in rows} | set(live))
    player = request.args.get("player") or (players[0] if players else None)
    total = {}
    for r in rows:
        total[r["player"]] = total.get(r["player"], 0) + r["xp"]
    rps = rp_table()
    users = {u["name"]: u for u in all_users()}

    def slim(board):          # อันดับการฝึก: เรียงตาม XP แต่ไม่ส่งตัวเลข XP ของคนอื่นออกไป
        return [{"player": a["player"], "me": a["player"] == player, "level": level_of(total.get(a["player"], 0))["level"],
                 "form_score": a["form_score"], "xp": a["xp"] if a["player"] == player else None} for a in board]
    everyone = sorted(set(total) | set(rps), key=lambda p_: (-rps.get(p_, 0), -total.get(p_, 0)))
    return jsonify({
        "now": datetime.now().isoformat(timespec="seconds"),
        "players": players,
        "boards": {
            "day": slim(leaderboard(rows, today.isoformat())),
            "week": slim(leaderboard(rows, (today - timedelta(days=6)).isoformat())),
            "all": slim(leaderboard(rows)),
        },
        # อันดับแรงค์: เรียงตามแต้มแรงค์ แสดงแค่ชื่อแรงค์ (ตัวเลข RP ส่งเฉพาะของตัวเอง)
        "rank_board": [{"player": p_, "me": p_ == player, "rank": {k: v for k, v in rank_of(rps.get(p_, 0)).items() if k in ("name", "color")},
                        "level": level_of(total.get(p_, 0))["level"], "rp": rps.get(p_, 0) if p_ == player else None} for p_ in everyone],
        "ranks": {p_: {"name": rank_of(rps.get(p_, 0))["name"], "color": rank_of(rps.get(p_, 0))["color"]} for p_ in everyone},
        "avatars": {n: avatar_url(u) for n, u in users.items() if u.get("avatar")},
        "live": [dict(v, player=k) for k, v in live.items() if now - v["ts"] < LIVE_TIMEOUT],
        "feed": [{"id": r["id"], "player": r["player"], "ts": r["ts"], "good": r["good"],
                  "exercise": EXERCISE_NAMES.get(r["exercise"], r["exercise"])} for r in reversed(rows[-15:])],
        "profile": profile(rows, player) if player else None,
        "rules": {"daily_goal": DAILY_GOAL, "xp_per_rep": XP_PER_GOOD_REP, "control_bonus": XP_CONTROL_BONUS,
                  "control_tut": CONTROL_TUT, "perfect": XP_PERFECT_SET, "goal_bonus": XP_DAILY_GOAL,
                  "ranks": [{"name": n, "min": m, "color": c} for n, m, c in RANKS],
                  "rp": {"win": RP_WIN, "draw": RP_DRAW, "loss": RP_LOSS, "game_max": RP_GAME_MAX}},
    })


# ===================== เข้าสู่ระบบ (แบบง่าย: ชื่อ + PIN) =====================
# PIN เก็บแบบ hash (PBKDF2 + salt) ไม่เก็บตัวเลขจริง ; เข้าสู่ระบบแล้วได้ token เก็บไว้ในเบราว์เซอร์
# ชื่อเก่าที่เคยเล่นก่อนมีระบบนี้ สมัครด้วยชื่อเดิมได้ คะแนนเดิมยังอยู่
login_fails = {}            # name -> (จำนวนครั้งที่ผิด, เวลาล็อกถึง)


def _hash_pin(pin, salt):
    return hashlib.pbkdf2_hmac("sha256", pin.encode(), bytes.fromhex(salt), 120_000).hex()


def get_user(name):
    if not name:
        return None
    with db() as con:
        r = con.execute("SELECT * FROM users WHERE name=?", (name,)).fetchone()
    return dict(r) if r else None


def all_users():
    with db() as con:
        return [dict(r) for r in con.execute("SELECT name, avatar FROM users")]


def avatar_url(u):
    return f"/static/{u['avatar']}" if u and u.get("avatar") else None


def public_user(u):
    if not u:
        return None
    raw = u["weight"] / (u["height"] / 100) ** 2 if u.get("weight") and u.get("height") else None
    bmi = round(raw, 1) if raw is not None else None
    # เกณฑ์ BMI สำหรับคนเอเชีย (WHO Asia-Pacific) ตัดสินจากค่าจริงก่อนปัดเศษ
    cat = None if raw is None else "น้ำหนักน้อย" if raw < 18.5 else "ปกติ" if raw < 23 else "น้ำหนักเกิน" if raw < 25 else "อ้วน"
    return {"name": u["name"], "weight": u.get("weight"), "height": u.get("height"), "age": u.get("age"),
            "sex": u.get("sex"), "level": u.get("level") or "beginner", "level_name": LEVEL_NAMES.get(u.get("level") or "beginner"),
            "bmi": bmi, "bmi_cat": cat, "complete": bool(u.get("weight") and u.get("height")), "avatar": avatar_url(u)}


def token_player():
    tok = request.headers.get("X-Token", "") or str((request.get_json(force=True, silent=True) or {}).get("token", ""))
    if not tok:
        return None
    with db() as con:
        r = con.execute("SELECT name FROM tokens WHERE token=?", (tok,)).fetchone()
    return r["name"] if r else None


def _new_token(name):
    tok = secrets.token_hex(16)
    with db_lock, db() as con:
        con.execute("INSERT INTO tokens (token, name, created) VALUES (?,?,?)",
                    (tok, name, datetime.now().isoformat(timespec="seconds")))
    return tok


def _check_pin(pin):
    return isinstance(pin, str) and pin.isdigit() and 4 <= len(pin) <= 6


@app.post("/api/auth/register")
def auth_register():
    s = request.get_json(force=True, silent=True) or {}
    name, pin = str(s.get("name", "")).strip()[:30], str(s.get("pin", ""))
    if len(name) < 2:
        return jsonify({"ok": False, "error": "ชื่อต้องยาวอย่างน้อย 2 ตัวอักษร"}), 400
    if not _check_pin(pin):
        return jsonify({"ok": False, "error": "PIN ต้องเป็นตัวเลข 4-6 หลัก"}), 400
    if get_user(name):
        return jsonify({"ok": False, "error": "ชื่อนี้มีคนใช้แล้ว ลองเข้าสู่ระบบ หรือใช้ชื่ออื่น"}), 409
    salt = secrets.token_hex(16)
    with db_lock, db() as con:
        con.execute("INSERT INTO users (name, pin_hash, salt, created, level) VALUES (?,?,?,?,?)",
                    (name, _hash_pin(pin, salt), salt, datetime.now().isoformat(timespec="seconds"), "beginner"))
    print(f"[hub] สมัครสมาชิกใหม่: {name}")
    return jsonify({"ok": True, "token": _new_token(name), "user": public_user(get_user(name))})


@app.post("/api/auth/login")
def auth_login():
    s = request.get_json(force=True, silent=True) or {}
    name, pin = str(s.get("name", "")).strip()[:30], str(s.get("pin", ""))
    n, until = login_fails.get(name, (0, 0))
    if time.time() < until:
        return jsonify({"ok": False, "error": f"ใส่ PIN ผิดหลายครั้ง รออีก {int(until - time.time()) + 1} วินาที"}), 429
    u = get_user(name)
    if not u or not _check_pin(pin) or _hash_pin(pin, u["salt"]) != u["pin_hash"]:
        n += 1
        login_fails[name] = (n, time.time() + 30 if n >= 5 else 0)
        return jsonify({"ok": False, "error": "ชื่อหรือ PIN ไม่ถูกต้อง" if u else "ยังไม่มีชื่อนี้ กด สมัครใหม่"}), 401
    login_fails.pop(name, None)
    return jsonify({"ok": True, "token": _new_token(name), "user": public_user(u)})


@app.post("/api/auth/logout")
def auth_logout():
    with db_lock, db() as con:
        con.execute("DELETE FROM tokens WHERE token=?", (request.headers.get("X-Token", ""),))
    return jsonify({"ok": True})


@app.route("/api/me", methods=["GET", "POST"])
def me_api():
    name = token_player()
    if not name:
        return jsonify({"ok": False, "error": "กรุณาเข้าสู่ระบบ", "auth": True}), 401
    if request.method == "POST":
        s = request.get_json(force=True, silent=True) or {}

        def num(k, lo, hi, cast=float):
            try:
                v = cast(s.get(k))
                return v if lo <= v <= hi else None
            except (TypeError, ValueError):
                return None
        level = s.get("level") if s.get("level") in LEVEL_NAMES else "beginner"
        sex = s.get("sex") if s.get("sex") in ("male", "female", "other") else None
        with db_lock, db() as con:
            con.execute("UPDATE users SET weight=?, height=?, age=?, sex=?, level=? WHERE name=?",
                        (num("weight", 20, 300), num("height", 100, 250), num("age", 7, 100, int), sex, level, name))
    return jsonify({"ok": True, "user": public_user(get_user(name))})


@app.post("/api/me/avatar")
def me_avatar():
    """รูปโปรไฟล์: เว็บครอปเป็นสี่เหลี่ยม 256x256 JPEG มาแล้ว เก็บไว้ที่ hub/static/uploads/avatars/"""
    name = token_player()
    if not name:
        return jsonify(AUTH_ERROR[0]), AUTH_ERROR[1]
    data = request.get_data()
    if not data.startswith(b"\xff\xd8") or len(data) > 400_000:
        return jsonify({"ok": False, "error": "ไฟล์รูปไม่ถูกต้องหรือใหญ่เกินไป"}), 400
    folder = os.path.join(HERE, "static", "uploads", "avatars")
    os.makedirs(folder, exist_ok=True)
    fn = hashlib.sha1(name.encode()).hexdigest()[:16] + ".jpg"
    with open(os.path.join(folder, fn), "wb") as f:
        f.write(data)
    rel = f"uploads/avatars/{fn}?v={int(time.time())}"
    with db_lock, db() as con:
        con.execute("UPDATE users SET avatar=? WHERE name=?", (rel, name))
    return jsonify({"ok": True, "user": public_user(get_user(name))})


def require_player():
    """คืนชื่อผู้เล่นจาก token หรือ None (ให้ endpoint ตอบ 401)"""
    return token_player()


AUTH_ERROR = ({"ok": False, "error": "กรุณาเข้าสู่ระบบก่อน", "auth": True}, 401)


# ===================== ออกกำลังกายผ่านเว็บ (เว็บส่งภาพ -> CiRA ประมวลผล) =====================
MAX_STREAMS = 2             # เล่นพร้อมกันได้กี่คน
STREAM_IDLE_STOP = 20       # เว็บเงียบเกินกี่วินาที (ปิดหน้าเว็บ/เน็ตหลุด) -> จบเซตให้อัตโนมัติ (ตอนกดพัก เว็บยังส่งสัญญาณมาเรื่อย ๆ)
streams = {}                # stream_id -> dict
streams_lock = threading.Lock()
worker = {"last_poll": 0.0}  # CiRA มาขอภาพครั้งล่าสุดเมื่อไหร่


public = {"url": None}     # ลิงก์ออนไลน์ (Cloudflare tunnel) สำหรับส่งให้เพื่อน


def _is_local_request():
    """มาจากเครื่องนี้จริงไหม (ผ่าน tunnel จะมาจาก 127.0.0.1 เหมือนกัน แต่มี header ของ Cloudflare ติดมา)"""
    via_proxy = any(h in request.headers for h in ("Cf-Connecting-Ip", "X-Forwarded-For", "Cf-Ray"))
    return request.remote_addr in ("127.0.0.1", "::1") and not via_proxy


def _cleanup_streams():
    now = time.time()
    for sid, st in list(streams.items()):
        if st["status"] == "active" and now - st["last_frame_at"] > STREAM_IDLE_STOP:
            st["status"] = "stopping"                     # ให้ CiRA ปิดเซตและบันทึกให้
        if st["status"] == "done" and now - st["done_at"] > 120:
            del streams[sid]
        elif st["status"] == "stopping" and now - st["stop_at"] > 30:   # CiRA ไม่ตอบ
            st.update(status="done", done_at=now, final={"ok": False, "error": "CiRA ไม่ตอบ"})


def _public(st):
    return {"id": st["id"], "player": st["player"], "status": st["status"], "result": st["result"],
            "final": st["final"], "cira_online": time.time() - worker["last_poll"] < 3,
            "frames_sent": st["seq"], "frames_done": st["done_seq"]}


@app.post("/api/stream/start")
def stream_start():
    s = request.get_json(force=True, silent=True) or {}
    player = require_player()
    if not player:
        return jsonify(AUTH_ERROR[0]), AUTH_ERROR[1]
    with streams_lock:
        sid, err = _create_stream(player, s.get("exercise", "pushup"))
        if err:
            return jsonify({"ok": False, "error": err}), 409
        room = rooms.get(str(s.get("duel") or ""))
        if room is not None and room["state"] != "done":          # ดวลจบแล้ว: ไม่ผูกกล้องใหม่กับห้อง (กันผลรอบที่แล้วหาย)
            p = _room_player(room, player)
            if p is not None:
                if room["state"] == "live" and p["sid"]:          # กล้องหลุดแล้วต่อใหม่กลางเกม: เก็บคะแนนที่ทำไปแล้ว
                    p["carry"] = p["score"]
                    p["carry_att"] = p["attempts"]
                    p["base"] = 0
                p.update(sid=sid, ready=False)
                streams[sid]["duel"] = room["code"]
                streams[sid]["exercise"] = room["exercise"] or streams[sid]["exercise"]
    return jsonify({"ok": True, "id": sid, "cira_online": time.time() - worker["last_poll"] < 3})


def _create_stream(player, exercise):
    """สร้างเซตใหม่ (ต้องถือ streams_lock อยู่) คืน (sid, error)"""
    u = get_user(player) or {}
    _cleanup_streams()
    now = time.time()
    # ชื่อเดียวกันเริ่มใหม่ (เปิดหน้าใหม่/เน็ตหลุดแล้วต่อใหม่) -> ปิดอันเก่าให้เลย ไม่ต้องรอ
    for old in streams.values():
        if old["player"] == player and old["status"] == "active":
            old["status"], old["stop_at"] = "stopping", now
    # นับเฉพาะคนที่ยังส่งภาพอยู่จริง (อันที่เงียบเกิน 10 วิ ไม่กินที่)
    busy = [st for st in streams.values() if st["status"] == "active"
            and now - max(st["last_frame_at"], st["created"]) < 10]
    if len(busy) >= MAX_STREAMS:
        return None, f"เล่นพร้อมกันได้ {MAX_STREAMS} คน ตอนนี้เต็ม รอสักครู่นะ"
    sid = os.urandom(6).hex()
    now = time.time()
    streams[sid] = {
        "id": sid, "player": player, "exercise": exercise,
        "status": "active", "created": now, "last_frame_at": now, "stop_at": 0, "done_at": 0,
        "frame": None, "frame_t": 0, "seq": 0, "done_seq": 0, "taken_seq": 0, "last_served": 0.0,
        "local": _is_local_request(), "level": u.get("level") or "beginner",
        "result": None, "final": None, "duel": None, "hist": deque(maxlen=40), "game": None,
    }
    return sid, None


@app.post("/api/stream/<sid>/frame")
def stream_frame(sid):
    with streams_lock:
        st = streams.get(sid)
        if st is None:
            return jsonify({"ok": False, "error": "ไม่พบเซตนี้"}), 404
        if st["status"] == "active":
            st["last_frame_at"] = time.time()
            if request.headers.get("X-Keepalive") != "1":      # ตอนพัก: แค่บอกว่ายังอยู่ ไม่ส่งภาพ
                st["frame"] = request.get_data()
                st["frame_t"] = float(request.headers.get("X-Capture-Ms", time.time() * 1000))
                st["seq"] += 1
        return jsonify(_public(st))


@app.post("/api/stream/<sid>/hr")
def stream_hr(sid):
    s = request.get_json(force=True, silent=True) or {}
    with streams_lock:
        st = streams.get(sid)
        if st is not None:
            try:
                st["hr"] = {"hr_avg": str(int(s["avg"])), "hr_max": str(int(s["max"]))}
            except (KeyError, TypeError, ValueError):
                pass
    return jsonify({"ok": True})


@app.post("/api/stream/<sid>/stop")
def stream_stop(sid):
    with streams_lock:
        st = streams.get(sid)
        if st is None:
            return jsonify({"ok": False, "error": "ไม่พบเซตนี้"}), 404
        if st["status"] == "active":
            st["status"], st["stop_at"] = "stopping", time.time()
        return jsonify(_public(st))


@app.get("/api/stream/<sid>")
def stream_get(sid):
    with streams_lock:
        st = streams.get(sid)
        return (jsonify(_public(st)) if st else (jsonify({"ok": False, "error": "ไม่พบเซตนี้"}), 404))


# ----- ฝั่ง CiRA -----
@app.get("/api/work")
def work_get():
    """CiRA มาของาน: คำสั่งจบเซต หรือภาพล่าสุดของคนที่รอนานที่สุด"""
    worker["last_poll"] = time.time()
    with streams_lock:
        _cleanup_streams()
        _tick_rooms()
        for st in streams.values():
            if st["status"] == "stopping" and not st.get("stop_sent"):
                st["stop_sent"] = True
                return jsonify({"cmd": "stop", "stream": st["id"], "player": st["player"],
                                "exercise": st["exercise"]})
        ready = [st for st in streams.values()
                 if st["status"] == "active" and st["frame"] is not None and st["seq"] > st["taken_seq"]]
        if not ready:
            return ("", 204)
        st = min(ready, key=lambda x: x["last_served"])   # สลับกันระหว่างผู้เล่น
        st["taken_seq"], st["last_served"] = st["seq"], time.time()
        resp = app.response_class(st["frame"], mimetype="image/jpeg")
        resp.headers.update({
            "X-Stream": st["id"], "X-Player": st["player"].encode("utf-8").hex(),
            "X-Exercise": st["exercise"], "X-Seq": str(st["seq"]),
            "X-Capture-Ms": str(st["frame_t"]), "X-Local": "1" if st["local"] else "0", "X-Level": st.get("level", "beginner"),
        })
        return resp


@app.post("/api/work/<sid>/result")
def work_result(sid):
    r = request.get_json(force=True, silent=True) or {}
    with streams_lock:
        st = streams.get(sid)
        if st is None:
            return jsonify({"ok": False}), 404
        st["result"] = r
        st["result_at"] = time.time()
        m = r.get("metrics") or {}
        if "pose" in m:                                  # เกม: เก็บประวัติท่า/เลนล่าสุดไว้ตัดสิน
            st["hist"].append((st["result_at"], m.get("lane"), m.get("pose")))
        st["done_seq"] = max(st["done_seq"], int(r.get("seq", 0)))
        live[st["player"]] = {"ts": time.time(), "reps": r.get("reps"), "bad": r.get("bad"),
                              "state": r.get("state"), "depth": r.get("depth"),
                              "depth_bottom": r.get("depth_bottom"), "last_tut": r.get("last_tut")}
    return jsonify({"ok": True})


@app.post("/api/work/<sid>/finish")
def work_finish(sid):
    """CiRA ปิดเซตแล้ว ส่งสรุปมา -> บันทึก + คิด XP"""
    s = request.get_json(force=True, silent=True) or {}
    with streams_lock:
        st = streams.get(sid)
    final = {"ok": True, "empty": True, "xp": 0, "good": 0, "bad": 0}
    if st is not None:
        s["player"] = st["player"]
        s["exercise"] = st["exercise"]
        s.update(st.get("hr") or {})
        if st.get("duel"):
            with streams_lock:
                _duel_session(st, s)
    if int(s.get("attempts", 0) or 0) > 0 and s.get("player"):
        try:
            final = save_session(s)
        except (TypeError, ValueError) as e:
            final = {"ok": False, "error": str(e)}
    if st is not None:
        with streams_lock:
            st.update(status="done", done_at=time.time(), final=final)
            live.pop(st["player"], None)
            room = rooms.get(st.get("duel") or "")
            p = _room_player(room, st["player"]) if room else None
            if p is not None and p.get("sid") == sid and room["state"] == "done":
                p["final"] = final
    return jsonify(final)


# ===================== ดวล 1:1 =====================
# ห้องมีรหัส 4 หลัก คนสร้างกับเพื่อนเปิดกล้องในห้อง -> CiRA เห็นตัวทั้งคู่ + กดพร้อม -> นับถอยหลังพร้อมกัน -> แข่ง 60 วินาที
# นับเฉพาะครั้งที่ทำถูก "ระหว่างเวลาแข่ง" (ครั้งที่ทำตอนวอร์มในห้องรอไม่นับ) เสมอให้ดูคะแนนท่าเฉลี่ย
DUEL_SECONDS = 60
DUEL_EXERCISES = ("pushup", "squat")
DUEL_COUNTDOWN = 4          # 3-2-1-เริ่ม
XP_DUEL_WIN = 30
XP_DUEL_DRAW = 15
rooms = {}                  # code -> room


def _room_player(room, name):
    return next((p for p in room["players"] if p["name"] == name), None) if room else None


def _new_player(name):
    rows = [r for r in all_rows() if r["player"] == name]
    rp = rp_table().get(name, 0)
    return {"name": name, "sid": None, "ready": False, "seen": time.time(),
            "rp": rp, "rank": rank_of(rp), "level": level_of(sum(r["xp"] for r in rows))["level"],
            "avatar": avatar_url(get_user(name)),
            "base": 0, "end": None, "score": 0, "form": 0, "attempts": 0, "final": None}


def _stream_of(p):
    st = streams.get(p["sid"]) if p["sid"] else None
    return st if st is not None and st["status"] == "active" else None


def _cam_ok(p):
    st = _stream_of(p)
    r = st["result"] if st else None
    # ต้องเห็นเต็มตัว (หัวถึงเท้า) ถึงจะกดพร้อมได้ ; CiRA รุ่นเก่าที่ยังไม่ส่ง full ใช้แค่ว่ามีคน
    return bool(r and r.get("kps") and r.get("full", True) and time.time() - st.get("result_at", 0) < 2.5)


def _rep_log(p):
    st = streams.get(p["sid"]) if p["sid"] else None
    return (st["result"] or {}).get("rep_log", []) if st else []


def _tick_rooms():
    now = time.time()
    for code, room in list(rooms.items()):
        if now - room["touched"] > 1800:                     # ห้องร้างเกิน 30 นาที
            del rooms[code]
            continue
        st = room["state"]
        if st == "lobby":
            for p in list(room["players"]):
                if p["sid"] and _stream_of(p) is None:       # กล้องหลุด/ปิดหน้า
                    p.update(sid=None, ready=False)
                st_ = streams.get(p["sid"]) if p["sid"] else None
                alive = max(p["seen"], st_["last_frame_at"] if st_ else 0)
                if now - alive > 45:                           # ไม่มีทั้งการเช็กห้องและภาพเกิน 45 วิ = ออกไปแล้ว
                    room["players"].remove(p)
            ps = room["players"]
            if len(ps) == 2 and room["exercise"] and all(p["ready"] and _cam_ok(p) for p in ps):
                room["state"] = "countdown"
                room["start_at"] = now + DUEL_COUNTDOWN
                room["end_at"] = room["start_at"] + room["seconds"]
        elif st == "countdown" and now >= room["start_at"]:
            room["state"] = "live"
            for p in room["players"]:
                p["base"] = len(_rep_log(p))                  # ครั้งก่อนหน้านี้ไม่นับ
        if room["state"] == "live":
            for p in room["players"]:
                if p["sid"] and streams.get(p["sid"]) and streams[p["sid"]]["status"] == "active":
                    log = _rep_log(p)
                    p["end"] = len(log)
                    mine = log[p["base"]:p["end"]]
                    p["score"] = p.get("carry", 0) + sum(1 for r in mine if r.get("ok"))
                    p["attempts"] = p.get("carry_att", 0) + len(mine)
                    sc = [r.get("score") or 0 for r in mine]
                    p["form"] = round(sum(sc) / len(sc)) if sc else 0
            if now >= room["end_at"]:
                _finish_room(room)


def _finish_room(room):
    room["state"] = "done"
    a, b = (room["players"] + [None, None])[:2]
    if a and b:
        if (a["score"], a["form"]) == (b["score"], b["form"]):
            room["winner"] = ""
        else:
            room["winner"] = max((a, b), key=lambda p: (p["score"], p["form"]))["name"]
        room["by_form"] = a["score"] == b["score"] and room["winner"] != ""
        with db() as con:
            con.execute("INSERT INTO duels (ts, day, p1, p2, s1, s2, f1, f2, winner, exercise) VALUES (?,?,?,?,?,?,?,?,?,?)",
                        (datetime.now().isoformat(timespec="seconds"), date.today().isoformat(),
                         a["name"], b["name"], a["score"], b["score"], a["form"], b["form"], room["winner"], room["exercise"]))
        rps = rp_table()
        for p in (a, b):
            new = rps.get(p["name"], 0)
            p["rp_delta"], p["rp"], p["rank"] = new - p["rp"], new, rank_of(new)
        print(f"[duel {room['code']}] {a['name']} {a['score']} - {b['score']} {b['name']} -> {room['winner'] or 'เสมอ'}")
    else:
        room["winner"] = None
    now = time.time()
    for p in room["players"]:                                 # สั่ง CiRA ปิดเซตของทั้งคู่ -> บันทึก XP
        st = streams.get(p["sid"]) if p["sid"] else None
        if st is not None and st["status"] == "active":
            st["status"], st["stop_at"] = "stopping", now


def _duel_session(st, s):
    """ก่อนบันทึก: ตัดเหลือเฉพาะครั้งที่ทำในเวลาแข่ง + ใส่โบนัสชนะ/เสมอ"""
    room = rooms.get(st["duel"])
    p = _room_player(room, st["player"])
    if room is None or p is None or p["sid"] != st["id"] or room["state"] not in ("live", "done"):
        s["attempts"] = 0                                    # ออกก่อนเริ่มแข่ง: ไม่บันทึก
        return
    reps = (s.get("reps") or [])[p["base"]:p["end"] if p["end"] is not None else None]
    good = [r for r in reps if r.get("valid")]
    s.update(reps=reps, attempts=len(reps), good_reps=len(good), bad_reps=len(reps) - len(good),
             avg_tut=round(sum(r.get("tut", 0) for r in good) / len(good), 2) if good else 0,
             form_score=round(sum(r.get("score", 0) for r in reps) / len(reps)) if reps else 0,
             shallow=sum("shallow" in r.get("issues", []) for r in reps),
             body=sum("body" in r.get("issues", []) for r in reps),
             fast=sum("fast" in r.get("issues", []) for r in reps))
    s.pop("form", None)
    if room["state"] == "done" and room.get("winner") is not None:
        if room["winner"] == st["player"]:
            s["bonus"] = [["ชนะดวล", XP_DUEL_WIN]]
        elif room["winner"] == "":
            s["bonus"] = [["เสมอในการดวล", XP_DUEL_DRAW]]
    if not reps and s.get("bonus"):                           # ไม่ได้สักครั้งแต่ชนะ (อีกฝ่ายออก) ก็ยังได้โบนัส
        s.update(attempts=1, good_reps=0, bad_reps=0)


def _room_view(room, me):
    now = time.time()
    out = {"ok": True, "code": room["code"], "state": room["state"], "seconds": room["seconds"], "exercise": room["exercise"], "host": room["players"][0]["name"] if room["players"] else None,
           "round": room["round"], "winner": room.get("winner"), "by_form": room.get("by_form", False),
           "starts_in": round(room["start_at"] - now, 2) if room["state"] == "countdown" else None,
           "ends_in": round(max(0.0, room["end_at"] - now), 2) if room["state"] == "live" else None,
           "cira_online": now - worker["last_poll"] < 3, "players": []}
    for p in room["players"]:
        st = streams.get(p["sid"]) if p["sid"] else None
        r = (st or {}).get("result") or {}
        out["players"].append({
            "name": p["name"], "me": p["name"] == me, "ready": p["ready"], "camera": p["sid"] is not None,
            "cam_ok": _cam_ok(p), "score": p["score"], "form": p["form"], "attempts": p["attempts"],
            "base": p["base"], "final": p["final"], "rp_delta": p.get("rp_delta"), "level": p["level"], "avatar": p["avatar"],
            "rank": {"name": p["rank"]["name"], "color": p["rank"]["color"]}, "rp": p["rp"] if p["name"] == me else None,
            "kps": r.get("kps") if p["name"] != me and room["state"] in ("lobby", "countdown", "live") else None,
            "w": r.get("w"), "h": r.get("h"), "side": r.get("side")})
    return out


@app.get("/api/duel/open")
def duel_open():
    """ห้องที่มีคนรออยู่คนเดียว (ให้เพื่อนกดเข้าได้เลย ไม่ต้องพิมพ์รหัส)"""
    me_ = token_player() or ""
    with streams_lock:
        _tick_rooms()
        out = [{"code": r["code"], "host": r["players"][0]["name"], "exercise": r["exercise"],
                "rank": r["players"][0]["rank"]["name"], "level": r["players"][0]["level"], "avatar": r["players"][0]["avatar"]}
               for r in rooms.values() if r["state"] == "lobby" and len(r["players"]) == 1 and r["players"][0]["name"] != me_]
    return jsonify({"ok": True, "rooms": out})


@app.post("/api/duel/create")
def duel_create():
    s = request.get_json(force=True, silent=True) or {}
    player = require_player()
    if not player:
        return jsonify(AUTH_ERROR[0]), AUTH_ERROR[1]
    exercise = s.get("exercise") if s.get("exercise") in DUEL_EXERCISES else None     # สร้างห้องก่อน ค่อยเลือกท่าในห้องก็ได้
    with streams_lock:
        _tick_rooms()
        code = next(c for c in (str(random.randint(1000, 9999)) for _ in range(10000)) if c not in rooms)
        rooms[code] = {"code": code, "state": "lobby", "seconds": DUEL_SECONDS, "round": 1, "touched": time.time(),
                       "exercise": exercise,
                       "start_at": 0, "end_at": 0, "winner": None, "players": [_new_player(player)]}
        return jsonify(_room_view(rooms[code], player))


@app.post("/api/duel/<code>/join")
def duel_join(code):
    player = require_player()
    if not player:
        return jsonify(AUTH_ERROR[0]), AUTH_ERROR[1]
    with streams_lock:
        _tick_rooms()
        room = rooms.get(code)
        if room is None:
            return jsonify({"ok": False, "error": f"ไม่พบห้อง {code} ลองเช็กรหัสอีกครั้ง"}), 404
        p = _room_player(room, player)
        if p is None:
            if len(room["players"]) >= 2:
                return jsonify({"ok": False, "error": "ห้องนี้มีผู้เล่นครบ 2 คนแล้ว"}), 409
            if room["state"] != "lobby":
                return jsonify({"ok": False, "error": "ห้องนี้กำลังแข่งอยู่"}), 409
            room["players"].append(_new_player(player))
        else:
            p["seen"] = time.time()
        room["touched"] = time.time()
        return jsonify(_room_view(room, player))


@app.get("/api/duel/<code>")
def duel_get(code):
    player = token_player() or request.args.get("player", "")
    with streams_lock:
        _tick_rooms()
        room = rooms.get(code)
        if room is None:
            return jsonify({"ok": False, "error": "ห้องนี้ปิดไปแล้ว"}), 404
        p = _room_player(room, player)
        if p is not None:
            p["seen"] = room["touched"] = time.time()
        return jsonify(_room_view(room, player))


@app.post("/api/duel/<code>/ready")
def duel_ready(code):
    s = request.get_json(force=True, silent=True) or {}
    player = require_player() or ""
    with streams_lock:
        room = rooms.get(code)
        p = _room_player(room, player)
        if p is None:
            return jsonify({"ok": False, "error": "ไม่ได้อยู่ในห้องนี้"}), 404
        if room["state"] == "lobby":
            if not room["exercise"]:
                return jsonify({"ok": False, "error": "ยังไม่ได้เลือกท่าที่จะดวล"}), 400
            p["ready"] = bool(s.get("ready", True))
        _tick_rooms()
        return jsonify(_room_view(room, p["name"]))


@app.post("/api/duel/<code>/leave")
def duel_leave(code):
    s = request.get_json(force=True, silent=True) or {}
    player = require_player() or ""
    with streams_lock:
        room = rooms.get(code)
        p = _room_player(room, player)
        if p is not None:
            if room["state"] in ("lobby", "done"):
                room["players"].remove(p)
            elif room["state"] == "countdown":                  # ออกตอนนับถอยหลัง -> ยกเลิกรอบนี้
                room["players"].remove(p)
                room["state"] = "lobby"
                for q in room["players"]:
                    q["ready"] = False
            if not room["players"]:
                rooms.pop(code, None)
        return jsonify({"ok": True})


@app.post("/api/duel/<code>/exercise")
def duel_exercise(code):
    """เจ้าของห้องเลือก/เปลี่ยนท่าที่จะดวล (ในห้องรอ) -> ทุกคนต้องกดพร้อมใหม่"""
    s = request.get_json(force=True, silent=True) or {}
    player = require_player() or ""
    ex = s.get("exercise")
    with streams_lock:
        room = rooms.get(code)
        if room is None or _room_player(room, player) is None:
            return jsonify({"ok": False, "error": "ไม่ได้อยู่ในห้องนี้"}), 404
        if room["players"][0]["name"] != player:
            return jsonify({"ok": False, "error": "เจ้าของห้องเป็นคนเลือกท่า"}), 403
        if ex not in DUEL_EXERCISES or room["state"] != "lobby":
            return jsonify({"ok": False, "error": "เลือกท่านี้ไม่ได้"}), 400
        room["exercise"] = ex
        for p in room["players"]:
            p["ready"] = False
        return jsonify(_room_view(room, player))


@app.post("/api/duel/<code>/rematch")
def duel_rematch(code):
    player = require_player()
    if not player:
        return jsonify(AUTH_ERROR[0]), AUTH_ERROR[1]
    with streams_lock:
        room = rooms.get(code)
        if room is None:
            return jsonify({"ok": False, "error": "ห้องนี้ปิดไปแล้ว"}), 404
        if room["state"] == "done":                             # คนแรกที่กด เปิดรอบใหม่
            room.update(state="lobby", round=room["round"] + 1, winner=None, by_form=False, start_at=0, end_at=0)
            room["players"] = [_new_player(p["name"]) for p in room["players"]]
        if _room_player(room, player) is None:
            if len(room["players"]) >= 2:
                return jsonify({"ok": False, "error": "ห้องนี้มีผู้เล่นครบ 2 คนแล้ว"}), 409
            room["players"].append(_new_player(player))
        room["touched"] = time.time()
        return jsonify(_room_view(room, player))


# ===================== เกม 3 เลน (ท่าตกลงมา) =====================
# CiRA (game.py) บอกทุกเฟรมว่า ผู้เล่นยืนเลนไหน + ทำท่าอะไร  -> HUB เป็นคนตัดสินว่าโดนหรือพลาด (เว็บแค่แสดงผล)
GAME_TRY_WINDOW = 0.6       # ตอนการ์ดยังไม่ถึงเส้น: ดูแค่ท่าล่าสุด (กันนับท่าที่ทำค้างไว้ก่อนการ์ดโผล่)
GAME_WINDOW = 1.2           # ท่าต้องตรงภายใน 1.2 วินาทีล่าสุดตอนการ์ดถึงเส้น (รวมเวลาที่ภาพเดินทาง)
GAME_POSES = ("up", "t", "hips", "squat")


def _game_stream(sid):
    st = streams.get(sid)
    player = token_player()
    if st is None or (player and player != st["player"]) or not player:
        return None
    if st["game"] is None:
        st["game"] = {"score": 0, "hits": 0, "miss": 0, "combo": 0, "best": 0, "judged": set(), "saved": None}
    return st


@app.post("/api/game/<sid>/judge")
def game_judge(sid):
    s = request.get_json(force=True, silent=True) or {}
    with streams_lock:
        st = _game_stream(sid)
        if st is None:
            return jsonify({"ok": False, "error": "ไม่พบเกมนี้"}), 404
        g = st["game"]
        cid = str(s.get("id"))
        if cid in g["judged"]:
            return jsonify({"ok": True, "dup": True, **{k: g[k] for k in ("score", "hits", "miss", "combo")}})
        now = time.time()
        # final=False: เว็บเห็นว่าน่าจะทำถูกแล้ว ขอให้ตรวจ (ไม่ถูกก็ยังไม่นับพลาด) ; final=True: การ์ดถึงเส้นแล้ว ตัดสินจริง
        final = bool(s.get("final", True))
        win = GAME_WINDOW if final else GAME_TRY_WINDOW
        recent = [(lane, pose) for t, lane, pose in st["hist"] if now - t <= win]
        hit = (s.get("lane"), s.get("pose")) in recent
        lane_ok = any(lane == s.get("lane") for lane, _ in recent)
        if not hit and not final:
            last = recent[-1] if recent else (None, None)
            return jsonify({"ok": True, "hit": False, "pending": True, "now_lane": last[0], "now_pose": last[1],
                            **{k: g[k] for k in ("score", "hits", "miss", "combo")}})
        g["judged"].add(cid)
        if hit:
            g["combo"] += 1
            g["hits"] += 1
            g["best"] = max(g["best"], g["combo"])
            g["score"] += 100 + 10 * min(g["combo"] - 1, 10)
        else:
            g["combo"] = 0
            g["miss"] += 1
        last = recent[-1] if recent else (None, None)
        return jsonify({"ok": True, "hit": hit, "lane_ok": lane_ok, "now_lane": last[0], "now_pose": last[1],
                        **{k: g[k] for k in ("score", "hits", "miss", "combo")}})


@app.get("/api/game/best")
def game_best():
    player = require_player()
    if not player:
        return jsonify(AUTH_ERROR[0]), AUTH_ERROR[1]
    with db() as con:
        r = con.execute("SELECT score, hits, miss, best_combo, ts FROM games WHERE player=? ORDER BY score DESC LIMIT 1", (player,)).fetchone()
        n = con.execute("SELECT COUNT(*) FROM games WHERE player=?", (player,)).fetchone()[0]
    return jsonify({"ok": True, "best": dict(r) if r else None, "plays": n})


@app.post("/api/game/<sid>/finish")
def game_finish(sid):
    with streams_lock:
        st = _game_stream(sid)
        if st is None:
            return jsonify({"ok": False, "error": "ไม่พบเกมนี้"}), 404
        g = st["game"]
        if g["saved"]:
            return jsonify(g["saved"])
        if st["status"] == "active":                     # ปิดกล้องฝั่ง CiRA ด้วย
            st["status"], st["stop_at"] = "stopping", time.time()
        player = st["player"]
    total = g["hits"] + g["miss"]
    acc = g["hits"] / total if total else 0
    xp = g["hits"] * 5 + (20 if total >= 10 and acc >= 0.8 else 0)
    rp = min(RP_GAME_MAX, g["hits"] // 2 + (5 if total >= 10 and acc >= 0.8 else 0))
    detail = [[f"ทำท่าถูก {g['hits']} ครั้ง", g["hits"] * 5]] + ([["แม่นยำ 80% ขึ้นไป", 20]] if xp > g["hits"] * 5 else [])
    now = datetime.now()
    rp_before = rp_table().get(player, 0)
    rows = [r for r in all_rows() if r["player"] == player]
    old_level = level_of(sum(r["xp"] for r in rows))["level"]
    with db_lock, db() as con:
        con.execute("INSERT INTO games (ts, day, player, game, score, hits, miss, best_combo, xp, rp) VALUES (?,?,?,?,?,?,?,?,?,?)",
                    (now.isoformat(timespec="seconds"), now.date().isoformat(), player, "lanes", g["score"], g["hits"],
                     g["miss"], g["best"], xp, rp))
        if total:   # นับ EXP เข้าเลเวลด้วย (บันทึกเป็นเซตชนิด "game")
            con.execute("INSERT INTO sessions (player, exercise, day, ts, good, attempts, bad, avg_tut, xp, xp_detail, form)"
                        " VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                        (player, "game", now.date().isoformat(), now.isoformat(timespec="seconds"), g["hits"], total,
                         g["miss"], 0, xp, json.dumps(detail, ensure_ascii=False), 0))   # form 0 = ไม่นับในคะแนนท่าเฉลี่ย
    with db() as con:
        best = con.execute("SELECT MAX(score) FROM games WHERE player=? AND game='lanes'", (player,)).fetchone()[0]
    after = _after(all_rows(), player)
    out = {"ok": True, "score": g["score"], "hits": g["hits"], "miss": g["miss"], "best_combo": g["best"],
           "accuracy": round(100 * acc), "xp": xp, "detail": detail, "rp_gain": after["rp"] - rp_before,
           "best_score": best, "new_record": best == g["score"] and g["score"] > 0,
           "level_up": after["level"]["level"] > old_level, **after}
    g["saved"] = out
    print(f"[game] {player}: {g['score']} คะแนน (โดน {g['hits']}/{total}) +{xp} XP +{out['rp_gain']} RP")
    return jsonify(out)


# ===================== จอใหญ่: มือถือเป็นกล้อง คอมแสดงผล =====================
pairs = {}                  # code -> {"player", "sid", "created", "touched"}


@app.post("/api/pair/create")
def pair_create():
    player = require_player()
    if not player:
        return jsonify(AUTH_ERROR[0]), AUTH_ERROR[1]
    with streams_lock:
        for c in [c for c, p in pairs.items() if time.time() - p["touched"] > 1800 or p["player"] == player]:
            pairs.pop(c, None)
        code = next(c for c in (str(random.randint(1000, 9999)) for _ in range(10000)) if c not in pairs and c not in rooms)
        pairs[code] = {"player": player, "sid": None, "created": time.time(), "touched": time.time(), "phone_seen": 0, "want": True}
    return jsonify({"ok": True, "code": code})


@app.get("/api/pair/<code>")
def pair_get(code):
    with streams_lock:
        p = pairs.get(code)
        if p is None:
            return jsonify({"ok": False, "error": "ไม่พบรหัสนี้ ให้ดูรหัสบนจอคอมอีกครั้ง"}), 404
        p["touched"] = time.time()
        st = streams.get(p["sid"]) if p["sid"] else None
        return jsonify({"ok": True, "code": code, "player": p["player"], "sid": p["sid"] if st and st["status"] == "active" else None,
                        "phone": time.time() - p["phone_seen"] < 5, "cira_online": time.time() - worker["last_poll"] < 3})


@app.post("/api/pair/<code>/start")
def pair_start(code):
    """มือถือสแกน QR แล้วกดเริ่ม: สร้างเซตในชื่อเจ้าของจอ (มือถือไม่ต้องล็อกอิน แค่รู้รหัส)"""
    s = request.get_json(force=True, silent=True) or {}
    with streams_lock:
        p = pairs.get(code)
        if p is None:
            return jsonify({"ok": False, "error": "ไม่พบรหัสนี้ ให้ดูรหัสบนจอคอมอีกครั้ง"}), 404
        sid, err = _create_stream(p["player"], s.get("exercise", "game"))
        if err:
            return jsonify({"ok": False, "error": err}), 409
        p.update(sid=sid, phone_seen=time.time(), touched=time.time(), want=False)
    return jsonify({"ok": True, "id": sid, "player": p["player"]})


@app.post("/api/pair/<code>/again")
def pair_again(code):
    """จอคอมกด เล่นอีกครั้ง -> บอกมือถือให้เริ่มส่งภาพรอบใหม่"""
    player = require_player()
    with streams_lock:
        p = pairs.get(code)
        if p is None or p["player"] != player:
            return jsonify({"ok": False, "error": "ไม่พบรหัสนี้"}), 404
        p.update(want=True, sid=None, touched=time.time())
    return jsonify({"ok": True})


@app.post("/api/pair/<code>/ping")
def pair_ping(code):
    with streams_lock:
        p = pairs.get(code)
        if p:
            p["phone_seen"] = p["touched"] = time.time()
            st = streams.get(p["sid"]) if p["sid"] else None
            return jsonify({"ok": True, "want": p["want"], "active": bool(st and st["status"] == "active"), "player": p["player"]})
    return jsonify({"ok": False, "error": "รหัสนี้หมดอายุแล้ว ให้สแกน QR ใหม่"})


@app.get("/api/stream/<sid>/frame.jpg")
def stream_frame_jpg(sid):
    """ภาพล่าสุดจากกล้องมือถือ ให้จอคอมแสดง (ต้องเป็นเจ้าของเซต)"""
    tok = request.args.get("t", "")
    with db() as con:
        r = con.execute("SELECT name FROM tokens WHERE token=?", (tok,)).fetchone()
    with streams_lock:
        st = streams.get(sid)
        if st is None or not r or r["name"] != st["player"] or not st["frame"]:
            return ("", 404)
        data = st["frame"]
    return Response(data, mimetype="image/jpeg", headers={"Cache-Control": "no-store"})


@app.post("/api/public_url")
def set_public_url():
    if not _is_local_request():
        return jsonify({"ok": False}), 403
    public["url"] = (request.get_json(force=True, silent=True) or {}).get("url")
    return jsonify({"ok": True})


@app.get("/api/streams")
def streams_list():
    with streams_lock:
        _cleanup_streams()
        return jsonify({"cira_online": time.time() - worker["last_poll"] < 3, "max": MAX_STREAMS,
                        "public_url": public["url"],
                        "streams": [{"player": st["player"], "status": st["status"]}
                                    for st in streams.values() if st["status"] != "done"]})


@app.get("/")
def index():
    return send_from_directory(app.static_folder, "index.html")


def lan_ips():
    ips = set()
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("8.8.8.8", 80))       # ไม่ได้ส่งข้อมูลจริง แค่ถามว่าออกเน็ตทางการ์ดไหน
        ips.add(s.getsockname()[0])
        s.close()
    except OSError:
        pass
    try:
        for info in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET):
            ips.add(info[4][0])
    except OSError:
        pass
    return sorted(ip for ip in ips if not ip.startswith("127."))


HTTPS_PORT = 5443
CERT = os.path.join(HERE, "cert.pem")
KEY = os.path.join(HERE, "key.pem")


def ensure_cert(ips):
    """สร้างใบรับรอง HTTPS แบบทำเอง (self-signed) ครั้งเดียว ให้ browser เครื่องอื่นยอมเปิดกล้อง"""
    if os.path.exists(CERT) and os.path.exists(KEY):
        return True
    try:
        import ipaddress
        from cryptography import x509
        from cryptography.hazmat.primitives import hashes, serialization
        from cryptography.hazmat.primitives.asymmetric import rsa
        from cryptography.x509.oid import NameOID
    except ImportError:
        print("  (ไม่มีไลบรารี cryptography -> เปิด HTTPS ไม่ได้: pip install cryptography)")
        return False
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "AI Workout Coach HUB")])
    alt = [x509.DNSName("localhost")] + [x509.IPAddress(ipaddress.ip_address(i)) for i in ["127.0.0.1"] + ips]
    cert = (x509.CertificateBuilder().subject_name(name).issuer_name(name).public_key(key.public_key())
            .serial_number(x509.random_serial_number())
            .not_valid_before(datetime.now(timezone.utc) - timedelta(days=1))
            .not_valid_after(datetime.now(timezone.utc) + timedelta(days=3650))
            .add_extension(x509.SubjectAlternativeName(alt), critical=False)
            .sign(key, hashes.SHA256()))
    with open(KEY, "wb") as f:
        f.write(key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.TraditionalOpenSSL,
                                  serialization.NoEncryption()))
    with open(CERT, "wb") as f:
        f.write(cert.public_bytes(serialization.Encoding.PEM))
    return True


if __name__ == "__main__":
    import logging
    from werkzeug.serving import make_server
    logging.getLogger("werkzeug").setLevel(logging.ERROR)   # ไม่ต้องพิมพ์ทุก request (ภาพวิ่งเยอะ)
    init_db()
    ips = lan_ips()
    https_ok = ensure_cert(ips)
    print("=" * 64)
    print(" AI Workout Coach HUB พร้อมแล้ว   (ปิดโปรแกรม: Ctrl+C)")
    print(f"   เครื่องนี้ (เปิดเว็บ + เปิดกล้องได้) :  http://127.0.0.1:{PORT}")
    if https_ok:
        for ip in ips:
            print(f"   เครื่องเพื่อน (Wi-Fi เดียวกัน)      :  https://{ip}:{HTTPS_PORT}")
        print("     -> ครั้งแรก browser จะเตือนว่าไม่ปลอดภัย ให้กด Advanced / ขั้นสูง -> ดำเนินการต่อ")
    print(f"   CiRA (config.py)  HUB_URL = \"http://127.0.0.1:{PORT}\"")
    print("=" * 64)
    if https_ok:
        https = make_server("0.0.0.0", HTTPS_PORT, app, threaded=True, ssl_context=(CERT, KEY))
        threading.Thread(target=https.serve_forever, daemon=True).start()
    make_server("0.0.0.0", PORT, app, threaded=True).serve_forever()
