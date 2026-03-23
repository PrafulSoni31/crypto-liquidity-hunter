#!/usr/bin/env python3
"""
Reminder System — Devanshi's Daily Routine

Level 1 (15 min overdue) → child-friendly nudge to Devanshi
Level 2 (30 min overdue) → parent alert to Charlie

Runs every 15 mins via cron (Mon–Fri only).
Each reminder fires ONCE per task per day (UNIQUE constraint prevents spam).
"""

import sqlite3
import requests
import sys
from datetime import datetime, timezone, timedelta
from pathlib import Path

# ─── Config ────────────────────────────────────────────────────────────────────
BOT_TOKEN        = "8663125030:AAHO1AIHTTObsj4exqoEc82935zhrYxO7Ys"
PARENT_TG_ID     = "686482312"    # Charlie's Telegram
CHILD_TG_ID      = "686482312"    # Devanshi's Telegram (set separately if she has one; using parent's for now)
CHILD_NAME       = "Devanshi"
IST              = timezone(timedelta(hours=5, minutes=30))
DB_PATH          = Path(__file__).parent / "data" / "routine.db"

REMINDER_1_DELAY = 15   # minutes after scheduled time → nudge child
REMINDER_2_DELAY = 30   # minutes after scheduled time → alert parent
MAX_LATE_MINS    = 180  # don't bother reminding if task is >3h overdue (day is done)
# ──────────────────────────────────────────────────────────────────────────────

ROUTINE = [
    {"id": "wake_up",       "label": "Wake Up",            "emoji": "☀️",  "time": "06:30"},
    {"id": "brush_teeth",   "label": "Brush Teeth",         "emoji": "🪥",  "time": "06:35"},
    {"id": "toilet",        "label": "Toilet & Freshen Up", "emoji": "🚿",  "time": "06:40"},
    {"id": "bath",          "label": "Bath",                "emoji": "🛁",  "time": "07:00"},
    {"id": "get_dressed",   "label": "Get Dressed",         "emoji": "👗",  "time": "07:20"},
    {"id": "breakfast",     "label": "Breakfast",           "emoji": "🥣",  "time": "07:30"},
    {"id": "school",        "label": "School / Classes",    "emoji": "🏫",  "time": "08:00"},
    {"id": "lunch",         "label": "Lunch",               "emoji": "🍱",  "time": "13:00"},
    {"id": "rest",          "label": "Rest / Nap Time",     "emoji": "😴",  "time": "13:30"},
    {"id": "homework",      "label": "Homework / Study",    "emoji": "📚",  "time": "15:00"},
    {"id": "playtime",      "label": "Play Time",           "emoji": "🎮",  "time": "17:00"},
    {"id": "snack",         "label": "Evening Snack",       "emoji": "🍎",  "time": "17:30"},
    {"id": "reading",       "label": "Reading / Stories",   "emoji": "📖",  "time": "18:00"},
    {"id": "dinner",        "label": "Dinner",              "emoji": "🍽️",  "time": "19:30"},
    {"id": "night_brush",   "label": "Brush Teeth (Night)", "emoji": "🌙",  "time": "20:30"},
    {"id": "bedtime",       "label": "Bedtime",             "emoji": "🛏️",  "time": "21:00"},
]


# ─── DB helpers ────────────────────────────────────────────────────────────────
def init_reminders_table():
    with sqlite3.connect(DB_PATH) as conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS reminders (
                id       INTEGER PRIMARY KEY AUTOINCREMENT,
                date     TEXT NOT NULL,
                task_id  TEXT NOT NULL,
                level    INTEGER NOT NULL,
                sent_at  TEXT NOT NULL,
                UNIQUE(date, task_id, level)
            )
        """)
        conn.commit()


def get_ticked_today(today: str) -> set:
    with sqlite3.connect(DB_PATH) as conn:
        rows = conn.execute(
            "SELECT task_id FROM ticks WHERE date=?", (today,)
        ).fetchall()
    return {r[0] for r in rows}


def get_sent_reminders_today(today: str) -> set:
    with sqlite3.connect(DB_PATH) as conn:
        rows = conn.execute(
            "SELECT task_id, level FROM reminders WHERE date=?", (today,)
        ).fetchall()
    return {(r[0], r[1]) for r in rows}


def mark_sent(today: str, task_id: str, level: int, sent_at: str):
    with sqlite3.connect(DB_PATH) as conn:
        conn.execute(
            "INSERT OR IGNORE INTO reminders (date, task_id, level, sent_at) VALUES (?,?,?,?)",
            (today, task_id, level, sent_at)
        )
        conn.commit()


# ─── Telegram ──────────────────────────────────────────────────────────────────
def send_telegram(chat_id: str, message: str, dry_run: bool = False) -> bool:
    if dry_run:
        print(f"\n[DRY-RUN → {chat_id}]\n{message}\n")
        return True
    try:
        r = requests.post(
            f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage",
            json={"chat_id": chat_id, "text": message, "parse_mode": "Markdown"},
            timeout=12
        )
        if r.status_code == 200:
            return True
        # Fallback: plain text (in case of Markdown parse error)
        r2 = requests.post(
            f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage",
            json={"chat_id": chat_id, "text": message.replace("*","").replace("_","").replace("`","")},
            timeout=12
        )
        return r2.status_code == 200
    except Exception as e:
        print(f"Telegram error: {e}")
        return False


# ─── Message builders ──────────────────────────────────────────────────────────
def build_child_message(tasks: list) -> str:
    """Fun, child-friendly nudge for Devanshi."""
    if len(tasks) == 1:
        t = tasks[0]
        return (
            f"Hey {CHILD_NAME}! {t['emoji']}\n\n"
            f"⏰ Time for *{t['label']}*!\n"
            f"It was scheduled at {t['time']} — don't forget!\n\n"
            f"You can do it! Every task brings a ⭐ star! 🌈"
        )
    task_list = "\n".join(f"  {t['emoji']} {t['label']} (was {t['time']})" for t in tasks)
    return (
        f"Hey {CHILD_NAME}! 🌟\n\n"
        f"You have {len(tasks)} tasks waiting for you:\n"
        f"{task_list}\n\n"
        f"Let's tick them off and keep that streak going! 🔥"
    )


def build_parent_message(tasks: list) -> str:
    """Clear parent alert with task details."""
    now_str = datetime.now(IST).strftime("%I:%M %p IST")
    if len(tasks) == 1:
        t = tasks[0]
        late = int(t['minutes_late'])
        return (
            f"⚠️ *Parent Alert — {CHILD_NAME}*\n\n"
            f"{t['emoji']} *{t['label']}* still not done\n"
            f"📅 Scheduled: {t['time']} | Now: {now_str}\n"
            f"⏱ {late} minutes overdue\n\n"
            f"Please check on her! 👨‍👩‍👧"
        )
    task_list = "\n".join(
        f"  {t['emoji']} *{t['label']}* — {int(t['minutes_late'])} min late"
        for t in tasks
    )
    return (
        f"⚠️ *Parent Alert — {CHILD_NAME}*\n\n"
        f"The following tasks are overdue ({now_str}):\n"
        f"{task_list}\n\n"
        f"Please encourage her to complete them! 👨‍👩‍👧"
    )


# ─── Main check ────────────────────────────────────────────────────────────────
def check_and_remind(dry_run: bool = False):
    now   = datetime.now(IST)
    today = now.date().strftime("%Y-%m-%d")

    # Weekdays only
    if now.weekday() >= 5:
        print(f"Weekend ({now.strftime('%A')}) — skipping reminders.")
        return

    print(f"\n{'='*50}")
    print(f"Reminder check — {now.strftime('%Y-%m-%d %H:%M IST')}")
    print(f"{'='*50}")

    ticked = get_ticked_today(today)
    sent   = get_sent_reminders_today(today)
    now_ts = now.strftime("%Y-%m-%d %H:%M:%S IST")

    # Collect tasks needing each reminder level
    need_r1 = []  # send to child
    need_r2 = []  # send to parent

    for task in ROUTINE:
        if task["id"] in ticked:
            continue  # ✅ already done

        h, m = map(int, task["time"].split(":"))
        task_time    = now.replace(hour=h, minute=m, second=0, microsecond=0)
        minutes_late = (now - task_time).total_seconds() / 60

        if minutes_late < 0 or minutes_late > MAX_LATE_MINS:
            continue  # not due yet, or too far past

        t = {**task, "minutes_late": minutes_late}

        # Level 1: child nudge (15+ mins late, not yet sent)
        if minutes_late >= REMINDER_1_DELAY and (task["id"], 1) not in sent:
            need_r1.append(t)
            print(f"  [R1 needed] {task['id']} — {int(minutes_late)}m late")

        # Level 2: parent alert (30+ mins late, not yet sent)
        if minutes_late >= REMINDER_2_DELAY and (task["id"], 2) not in sent:
            need_r2.append(t)
            print(f"  [R2 needed] {task['id']} — {int(minutes_late)}m late")

    # ── Send batched Reminder 1 (child) ─────────────────────────────────────
    if need_r1:
        msg  = build_child_message(need_r1)
        sent_ok = send_telegram(CHILD_TG_ID, msg, dry_run)
        if sent_ok:
            for t in need_r1:
                mark_sent(today, t["id"], 1, now_ts)
            print(f"✅ Reminder 1 sent ({len(need_r1)} tasks) to child [{CHILD_TG_ID}]")
        else:
            print(f"❌ Failed to send Reminder 1")
    else:
        print("  No Reminder 1 needed right now.")

    # ── Send batched Reminder 2 (parent) ────────────────────────────────────
    if need_r2:
        msg  = build_parent_message(need_r2)
        sent_ok = send_telegram(PARENT_TG_ID, msg, dry_run)
        if sent_ok:
            for t in need_r2:
                mark_sent(today, t["id"], 2, now_ts)
            print(f"✅ Reminder 2 sent ({len(need_r2)} tasks) to parent [{PARENT_TG_ID}]")
        else:
            print(f"❌ Failed to send Reminder 2")
    else:
        print("  No Reminder 2 needed right now.")

    print(f"Done. Ticked today: {len(ticked)}/{len(ROUTINE)}")


if __name__ == "__main__":
    dry_run = "--dry-run" in sys.argv
    init_reminders_table()
    check_and_remind(dry_run=dry_run)
