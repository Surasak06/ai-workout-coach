"""
Dashboard สถิติการฝึก: อ่าน data/sessions.csv และ data/reps.csv แล้ววาดเป็นรูปเดียว data/dashboard.png

ใช้ได้ 2 แบบ
  1. อัตโนมัติ: ใน CiRA ทุกครั้งที่จบเซต ระบบจะสร้าง dashboard.png ใหม่เอง
  2. เปิดดูเอง: ดับเบิลคลิก dashboard.py หรือพิมพ์  python dashboard.py
"""

import csv
import os
import sys

import matplotlib
matplotlib.use("Agg")                 # วาดลงไฟล์ ไม่เปิดหน้าต่าง (ใช้ใน CiRA ได้)
import matplotlib.pyplot as plt

GREEN, RED, BLUE, GRAY, ORANGE = "#2e9e5b", "#d9534f", "#1f6feb", "#8b949e", "#e8912d"
MAX_SESSIONS = 15                     # แสดงกี่เซตล่าสุด


def _read(path):
    if not os.path.exists(path):
        return []
    with open(path, encoding="utf-8-sig") as f:
        return list(csv.DictReader(f))


def _num(row, key, cast=float, default=0):
    try:
        return cast(row.get(key) or default)
    except ValueError:
        return default


def build_dashboard(data_dir, out_path=None):
    """สร้างรูป dashboard คืนค่า path ของรูป หรือ None ถ้ายังไม่มีข้อมูล"""
    sessions = _read(os.path.join(data_dir, "sessions.csv"))
    reps = _read(os.path.join(data_dir, "reps.csv"))
    if not sessions:
        return None
    out_path = out_path or os.path.join(data_dir, "dashboard.png")
    recent = sessions[-MAX_SESSIONS:]

    good = [_num(r, "good_reps", int) for r in recent]
    bad = [_num(r, "bad_reps", int) for r in recent]
    tut = [_num(r, "avg_tut") for r in recent]
    labels = [f"{r.get('date', '')[5:]}\n{r.get('time', '')}" for r in recent]
    x = list(range(len(recent)))

    total_good = sum(_num(r, "good_reps", int) for r in sessions)
    total_attempts = sum(_num(r, "attempts", int) for r in sessions)
    form_pct = 100.0 * total_good / total_attempts if total_attempts else 0.0
    tut_vals = [_num(r, "avg_tut") for r in sessions if _num(r, "good_reps", int) > 0]
    avg_tut = sum(tut_vals) / len(tut_vals) if tut_vals else 0.0
    last = sessions[-1]
    last_reps = [r for r in reps if r.get("session_id") == last.get("session_id")]

    plt.rcParams.update({"font.size": 10, "axes.spines.top": False, "axes.spines.right": False})
    fig = plt.figure(figsize=(13, 8.5))
    fig.suptitle("AI Home Workout Coach  -  Push-up Dashboard", fontsize=16, fontweight="bold", x=0.02, ha="left")
    grid = fig.add_gridspec(3, 3, height_ratios=[0.55, 1.4, 1.4], hspace=0.55, wspace=0.3)

    # ---------- แถวบน: ตัวเลขสรุป ----------
    kpis = [
        ("Sessions", f"{len(sessions)}", None),
        ("Total good reps", f"{total_good}", None),
        ("Good form", f"{form_pct:.0f}%", f"{total_good} of {total_attempts} attempts"),
        ("Avg time under tension", f"{avg_tut:.1f} s", "per good rep"),
        ("Last session", f"{_num(last, 'good_reps', int)} reps",
         f"{last.get('date', '')} {last.get('time', '')}"),
    ]
    kpi_ax = fig.add_subplot(grid[0, :])
    kpi_ax.axis("off")
    for i, (title, value, sub) in enumerate(kpis):
        cx = (i + 0.5) / len(kpis)
        kpi_ax.text(cx, 0.95, title, ha="center", va="top", color=GRAY, fontsize=10, transform=kpi_ax.transAxes)
        kpi_ax.text(cx, 0.55, value, ha="center", va="center", fontsize=20, fontweight="bold",
                    transform=kpi_ax.transAxes)
        if sub:
            kpi_ax.text(cx, 0.08, sub, ha="center", va="bottom", color=GRAY, fontsize=8.5,
                        transform=kpi_ax.transAxes)

    # ---------- จำนวนครั้งต่อเซต ----------
    ax1 = fig.add_subplot(grid[1, :2])
    ax1.bar(x, good, color=GREEN, label="Good reps")
    ax1.bar(x, bad, bottom=good, color=RED, label="Bad reps (cheated)")
    for xi, g, b in zip(x, good, bad):
        ax1.text(xi, g + b + 0.2, str(g), ha="center", va="bottom", fontsize=9)
    ax1.set_title("Reps per session", loc="left", fontweight="bold")
    ax1.set_xticks(x)
    ax1.set_xticklabels(labels, fontsize=8)
    ax1.legend(frameon=False, loc="upper left", fontsize=8.5)
    ax1.set_ylim(0, max([g + b for g, b in zip(good, bad)] + [1]) * 1.25)

    # ---------- ปัญหาท่าที่พบ (ทุกเซต) ----------
    ax2 = fig.add_subplot(grid[1, 2])
    issues = {
        "Not deep enough": sum(_num(r, "shallow", int) for r in sessions),
        "Body not straight": sum(_num(r, "body", int) for r in sessions),
        "Too fast": sum(_num(r, "fast", int) for r in sessions),
    }
    names = list(issues)
    vals = [issues[n] for n in names]
    ax2.barh(names, vals, color=[RED, ORANGE, BLUE])
    for i, v in enumerate(vals):
        ax2.text(v + 0.1, i, str(v), va="center", fontsize=9)
    ax2.invert_yaxis()
    ax2.set_title("Form issues (all sessions)", loc="left", fontweight="bold")
    ax2.set_xlim(0, max(vals + [1]) * 1.3)

    # ---------- TUT เฉลี่ยต่อเซต ----------
    ax3 = fig.add_subplot(grid[2, :2])
    ax3.plot(x, tut, marker="o", color=BLUE)
    ax3.axhspan(2, 4, color=GREEN, alpha=0.08)
    ax3.text(-0.4, 3.9, "target 2-4 s", color=GREEN, fontsize=8, va="top")
    ax3.set_title("Avg time under tension per rep (s)", loc="left", fontweight="bold")
    ax3.set_xticks(x)
    ax3.set_xticklabels(labels, fontsize=8)
    ax3.set_ylim(0, max(tut + [5]) * 1.15)

    # ---------- รายครั้งของเซตล่าสุด ----------
    ax4 = fig.add_subplot(grid[2, 2])
    if last_reps:
        rx = [_num(r, "rep", int) for r in last_reps]
        ry = [_num(r, "tut") for r in last_reps]
        colors = [GREEN if r.get("valid") in ("1", "True") else RED for r in last_reps]
        ax4.bar(rx, ry, color=colors)
        ax4.set_xticks(rx)
    ax4.set_title("Last session: TUT per rep (s)", loc="left", fontweight="bold")
    ax4.set_xlabel("rep  (green = good, red = bad)", fontsize=8.5, color=GRAY)

    fig.savefig(out_path, dpi=110, bbox_inches="tight")
    plt.close(fig)
    return out_path


if __name__ == "__main__":
    here = os.path.dirname(os.path.abspath(__file__))
    data_dir = sys.argv[1] if len(sys.argv) > 1 else os.path.join(here, "data")
    path = build_dashboard(data_dir)
    if path is None:
        print("ยังไม่มีข้อมูลใน", data_dir, "- ฝึกให้จบอย่างน้อย 1 เซตก่อน")
    else:
        print("สร้าง dashboard แล้ว:", path)
        if hasattr(os, "startfile"):
            os.startfile(path)        # Windows: เปิดรูปให้ดูเลย
