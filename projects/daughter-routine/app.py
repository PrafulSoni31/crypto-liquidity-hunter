"""
Daily Routine Tracker for Kids
- Weekday routine with tap-to-tick checkboxes
- Timestamps recorded for every tick
- Streak tracking, regularity KPIs, parent dashboard
"""

from flask import Flask, render_template, request, jsonify, redirect, url_for
import sqlite3
import json
from datetime import datetime, date, timedelta, timezone
from pathlib import Path
import os

app = Flask(__name__)

# ─── Config ────────────────────────────────────────────────────────────────────
CHILD_NAME   = "Devanshi"       # Devanshi Soni
IST          = timezone(timedelta(hours=5, minutes=30))
DB_PATH      = Path(__file__).parent / "data" / "routine.db"
PARENT_PIN   = "1234"           # Change this!

# ─── Daily Routine Items ───────────────────────────────────────────────────────
ROUTINE = [
    {"id": "wake_up",       "label": "Wake Up",           "emoji": "☀️",  "time": "06:30", "category": "morning"},
    {"id": "brush_teeth",   "label": "Brush Teeth",        "emoji": "🪥",  "time": "06:35", "category": "morning"},
    {"id": "toilet",        "label": "Toilet & Freshen Up","emoji": "🚿",  "time": "06:40", "category": "morning"},
    {"id": "bath",          "label": "Bath",               "emoji": "🛁",  "time": "07:00", "category": "morning"},
    {"id": "get_dressed",   "label": "Get Dressed",        "emoji": "👗",  "time": "07:20", "category": "morning"},
    {"id": "breakfast",     "label": "Breakfast",          "emoji": "🥣",  "time": "07:30", "category": "morning"},
    {"id": "school",        "label": "School / Classes",   "emoji": "🏫",  "time": "08:00", "category": "school"},
    {"id": "lunch",         "label": "Lunch",              "emoji": "🍱",  "time": "13:00", "category": "afternoon"},
    {"id": "rest",          "label": "Rest / Nap Time",    "emoji": "😴",  "time": "13:30", "category": "afternoon"},
    {"id": "homework",      "label": "Homework / Study",   "emoji": "📚",  "time": "15:00", "category": "study"},
    {"id": "playtime",      "label": "Play Time",          "emoji": "🎮",  "time": "17:00", "category": "play"},
    {"id": "snack",         "label": "Evening Snack",      "emoji": "🍎",  "time": "17:30", "category": "play"},
    {"id": "reading",       "label": "Reading / Stories",  "emoji": "📖",  "time": "18:00", "category": "study"},
    {"id": "dinner",        "label": "Dinner",             "emoji": "🍽️",  "time": "19:30", "category": "evening"},
    {"id": "night_brush",   "label": "Brush Teeth (Night)","emoji": "🌙",  "time": "20:30", "category": "evening"},
    {"id": "bedtime",       "label": "Bedtime",            "emoji": "🛏️",  "time": "21:00", "category": "evening"},
]

CATEGORIES = {
    "morning":   {"label": "Morning",   "color": "#FFA500"},
    "school":    {"label": "School",    "color": "#4CAF50"},
    "afternoon": {"label": "Afternoon", "color": "#2196F3"},
    "study":     {"label": "Study",     "color": "#9C27B0"},
    "play":      {"label": "Play",      "color": "#FF5722"},
    "evening":   {"label": "Evening",   "color": "#607D8B"},
}

# ─── DB Init ───────────────────────────────────────────────────────────────────
def init_db():
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(DB_PATH) as conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS ticks (
                id         INTEGER PRIMARY KEY AUTOINCREMENT,
                date       TEXT NOT NULL,          -- YYYY-MM-DD in IST
                task_id    TEXT NOT NULL,
                ticked_at  TEXT NOT NULL,          -- ISO datetime in IST
                UNIQUE(date, task_id)
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS stars (
                id    INTEGER PRIMARY KEY AUTOINCREMENT,
                date  TEXT NOT NULL UNIQUE,
                note  TEXT
            )
        """)
        conn.commit()

init_db()

# ─── Helpers ───────────────────────────────────────────────────────────────────
def now_ist():
    return datetime.now(IST)

def today_ist():
    return now_ist().date()

def is_weekday(d=None):
    if d is None:
        d = today_ist()
    return d.weekday() < 5   # Mon=0 … Fri=4

def get_ticks_for_date(date_str):
    with sqlite3.connect(DB_PATH) as conn:
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            "SELECT task_id, ticked_at FROM ticks WHERE date = ?", (date_str,)
        ).fetchall()
    return {r["task_id"]: r["ticked_at"] for r in rows}

def get_streak():
    """Count consecutive weekdays (going back) where all tasks were ticked."""
    with sqlite3.connect(DB_PATH) as conn:
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            "SELECT date, COUNT(*) as cnt FROM ticks GROUP BY date ORDER BY date DESC"
        ).fetchall()
    total_tasks = len(ROUTINE)
    streak = 0
    d = today_ist()
    tick_map = {r["date"]: r["cnt"] for r in rows}
    for _ in range(365):
        if d.weekday() >= 5:       # skip weekends
            d -= timedelta(days=1)
            continue
        ds = d.strftime("%Y-%m-%d")
        if tick_map.get(ds, 0) >= total_tasks:
            streak += 1
        else:
            break
        d -= timedelta(days=1)
    return streak

def get_kpis(days=30):
    """Return regularity KPIs for the past N weekdays."""
    end   = today_ist()
    start = end - timedelta(days=days)
    weekdays = [start + timedelta(days=i)
                for i in range((end - start).days + 1)
                if (start + timedelta(days=i)).weekday() < 5]

    with sqlite3.connect(DB_PATH) as conn:
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            "SELECT date, COUNT(*) as cnt FROM ticks WHERE date >= ? GROUP BY date",
            (start.strftime("%Y-%m-%d"),)
        ).fetchall()
    tick_map = {r["date"]: r["cnt"] for r in rows}

    total_tasks  = len(ROUTINE)
    perfect_days = 0
    partial_days = 0
    missed_days  = 0
    total_completion = 0

    per_task = {t["id"]: 0 for t in ROUTINE}
    for t_id in per_task:
        with sqlite3.connect(DB_PATH) as conn:
            cnt = conn.execute(
                "SELECT COUNT(*) FROM ticks WHERE task_id=? AND date >= ?",
                (t_id, start.strftime("%Y-%m-%d"))
            ).fetchone()[0]
        per_task[t_id] = cnt

    for d in weekdays:
        ds   = d.strftime("%Y-%m-%d")
        cnt  = tick_map.get(ds, 0)
        pct  = cnt / total_tasks * 100
        total_completion += pct
        if cnt == total_tasks:
            perfect_days += 1
        elif cnt > 0:
            partial_days += 1
        else:
            missed_days += 1

    n = len(weekdays) or 1
    return {
        "total_weekdays": n,
        "perfect_days":   perfect_days,
        "partial_days":   partial_days,
        "missed_days":    missed_days,
        "avg_completion": round(total_completion / n, 1),
        "streak":         get_streak(),
        "per_task":       per_task,
        "most_missed":    min(per_task, key=per_task.get),
        "most_done":      max(per_task, key=per_task.get),
    }

def get_weekly_data():
    """Return last 7 weekdays with completion % for chart."""
    today = today_ist()
    days  = []
    d     = today
    while len(days) < 7:
        if d.weekday() < 5:
            days.append(d)
        d -= timedelta(days=1)
    days.reverse()
    with sqlite3.connect(DB_PATH) as conn:
        conn.row_factory = sqlite3.Row
        rows = conn.execute("SELECT date, COUNT(*) as cnt FROM ticks GROUP BY date").fetchall()
    tick_map = {r["date"]: r["cnt"] for r in rows}
    total    = len(ROUTINE)
    result   = []
    for d in days:
        ds  = d.strftime("%Y-%m-%d")
        cnt = tick_map.get(ds, 0)
        result.append({
            "date":  ds,
            "label": d.strftime("%a %d"),
            "pct":   round(cnt / total * 100),
            "count": cnt,
        })
    return result

# ─── Routes ────────────────────────────────────────────────────────────────────
@app.route("/")
def index():
    today     = today_ist()
    date_str  = today.strftime("%Y-%m-%d")
    weekday   = is_weekday(today)
    ticks     = get_ticks_for_date(date_str)
    done      = len(ticks)
    total     = len(ROUTINE)
    pct       = round(done / total * 100) if total else 0
    streak    = get_streak()
    today_fmt = today.strftime("%A, %d %B %Y")

    # Annotate routine with tick status
    routine_with_status = []
    for task in ROUTINE:
        t = dict(task)
        t["done"]      = task["id"] in ticks
        t["ticked_at"] = ticks.get(task["id"], "")
        t["cat_color"] = CATEGORIES[task["category"]]["color"]
        routine_with_status.append(t)

    return render_template("index.html",
        child_name=CHILD_NAME,
        today=date_str,
        today_fmt=today_fmt,
        weekday=weekday,
        routine=routine_with_status,
        categories=CATEGORIES,
        done=done,
        total=total,
        pct=pct,
        streak=streak,
    )

@app.route("/api/tick", methods=["POST"])
def tick():
    data     = request.json
    task_id  = data.get("task_id")
    date_str = data.get("date")
    action   = data.get("action", "tick")   # "tick" or "untick"

    valid_ids = {t["id"] for t in ROUTINE}
    if task_id not in valid_ids:
        return jsonify({"error": "invalid task"}), 400

    now = now_ist().strftime("%Y-%m-%d %H:%M:%S IST")

    with sqlite3.connect(DB_PATH) as conn:
        if action == "tick":
            conn.execute(
                "INSERT OR IGNORE INTO ticks (date, task_id, ticked_at) VALUES (?, ?, ?)",
                (date_str, task_id, now)
            )
        else:
            conn.execute(
                "DELETE FROM ticks WHERE date=? AND task_id=?",
                (date_str, task_id)
            )
        conn.commit()
        done = conn.execute(
            "SELECT COUNT(*) FROM ticks WHERE date=?", (date_str,)
        ).fetchone()[0]

    return jsonify({
        "ok":     True,
        "done":   done,
        "total":  len(ROUTINE),
        "pct":    round(done / len(ROUTINE) * 100),
        "streak": get_streak(),
    })

@app.route("/parent")
def parent():
    pin = request.args.get("pin", "")
    if pin != PARENT_PIN:
        return render_template("pin.html", child_name=CHILD_NAME, error=(pin != ""))

    kpis    = get_kpis(30)
    weekly  = get_weekly_data()
    routine = ROUTINE

    # Enrich per_task with labels
    per_task_detail = []
    for t in ROUTINE:
        per_task_detail.append({
            "id":    t["id"],
            "label": t["label"],
            "emoji": t["emoji"],
            "count": kpis["per_task"][t["id"]],
            "pct":   round(kpis["per_task"][t["id"]] / max(kpis["total_weekdays"], 1) * 100),
        })
    per_task_detail.sort(key=lambda x: x["pct"])

    return render_template("parent.html",
        child_name=CHILD_NAME,
        kpis=kpis,
        weekly=weekly,
        per_task=per_task_detail,
        categories=CATEGORIES,
        today=today_ist().strftime("%d %B %Y"),
    )

@app.route("/history")
def history():
    pin = request.args.get("pin", "")
    if pin != PARENT_PIN:
        return redirect(url_for("parent"))

    with sqlite3.connect(DB_PATH) as conn:
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            """SELECT t.date, t.task_id, t.ticked_at
               FROM ticks t ORDER BY t.date DESC, t.ticked_at ASC LIMIT 500"""
        ).fetchall()

    # Group by date
    by_date = {}
    for r in rows:
        d = r["date"]
        if d not in by_date:
            by_date[d] = []
        by_date[d].append({"task_id": r["task_id"], "ticked_at": r["ticked_at"]})

    task_label = {t["id"]: f'{t["emoji"]} {t["label"]}' for t in ROUTINE}
    total      = len(ROUTINE)

    history_list = []
    for d in sorted(by_date.keys(), reverse=True):
        ticks = by_date[d]
        dt    = datetime.strptime(d, "%Y-%m-%d")
        history_list.append({
            "date":      d,
            "label":     dt.strftime("%A, %d %b %Y"),
            "ticks":     ticks,
            "count":     len(ticks),
            "total":     total,
            "pct":       round(len(ticks) / total * 100),
        })

    return render_template("history.html",
        child_name=CHILD_NAME,
        history=history_list,
        task_label=task_label,
        pin=pin,
    )

@app.route("/api/reminders")
def api_reminders():
    """Return today's sent reminders."""
    date_str = request.args.get("date", today_ist().strftime("%Y-%m-%d"))
    with sqlite3.connect(DB_PATH) as conn:
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            "SELECT task_id, level, sent_at FROM reminders WHERE date=? ORDER BY sent_at",
            (date_str,)
        ).fetchall()
    task_label = {t["id"]: f'{t["emoji"]} {t["label"]}' for t in ROUTINE}
    result = []
    for r in rows:
        result.append({
            "task_id":   r["task_id"],
            "task_name": task_label.get(r["task_id"], r["task_id"]),
            "level":     r["level"],
            "label":     "Child nudge" if r["level"] == 1 else "Parent alert",
            "sent_at":   r["sent_at"],
        })
    return jsonify(result)

@app.route("/api/status")
def api_status():
    date_str = request.args.get("date", today_ist().strftime("%Y-%m-%d"))
    ticks    = get_ticks_for_date(date_str)
    return jsonify({
        "date":   date_str,
        "ticks":  ticks,
        "done":   len(ticks),
        "total":  len(ROUTINE),
        "pct":    round(len(ticks) / len(ROUTINE) * 100),
        "streak": get_streak(),
    })

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5001, debug=False)
