import os
import json
import time
import urllib.parse
import asyncio
import sqlite3
import threading
from pathlib import Path
from flask import Flask
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    ApplicationBuilder,
    CommandHandler,
    MessageHandler,
    PollAnswerHandler,
    filters,
    ContextTypes,
)

# ---------------- Render Web Service (Port Binding) ----------------
web_app = Flask(__name__)

@web_app.route("/")
def home():
    return "Telegram Quiz Bot is running successfully!"

def run_web():
    port = int(os.environ.get("PORT", 8080))
    web_app.run(host="0.0.0.0", port=port, debug=False, use_reloader=False)

flask_thread = threading.Thread(target=run_web, daemon=True)
flask_thread.start()

# ---------------- Configuration & Persistence Setup ----------------
BOT_TOKEN = os.environ.get("BOT_TOKEN")
ADMIN_ID = int(os.environ.get("ADMIN_ID", "0"))
DEFAULT_TIMER = int(os.environ.get("QUESTION_TIMER", "30"))

DATA_DIR = Path(os.environ.get("DATA_DIR", "./data"))
DATA_DIR.mkdir(parents=True, exist_ok=True)
DB_PATH = DATA_DIR / "quiz_progress.sqlite3"

pause_event = asyncio.Event()
pause_event.set()
active_polls = {}   # poll_id -> correct option index
quiz_task = None
quiz_state = None


# ---------------- SQLite Persistence ----------------
def init_db():
    with sqlite3.connect(DB_PATH) as conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS quiz_state (
                id INTEGER PRIMARY KEY CHECK (id = 1),
                payload TEXT NOT NULL
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS saved_quizzes (
                quiz_id TEXT PRIMARY KEY,
                quiz_name TEXT NOT NULL,
                payload TEXT NOT NULL,
                saved_at TEXT DEFAULT CURRENT_TIMESTAMP
            )
        """)

def save_state():
    if quiz_state is None:
        return
    payload = json.dumps(quiz_state, ensure_ascii=False)
    with sqlite3.connect(DB_PATH) as conn:
        conn.execute(
            "INSERT INTO quiz_state (id, payload) VALUES (1, ?) "
            "ON CONFLICT(id) DO UPDATE SET payload=excluded.payload",
            (payload,),
        )

def load_state():
    with sqlite3.connect(DB_PATH) as conn:
        row = conn.execute("SELECT payload FROM quiz_state WHERE id = 1").fetchone()
    return json.loads(row[0]) if row else None

def archive_current_quiz():
    if not quiz_state:
        return None
    quiz_id = str(int(time.time()))
    name = quiz_state.get("quiz_name") or f"Quiz-{quiz_id}"
    payload = json.dumps(quiz_state, ensure_ascii=False)
    with sqlite3.connect(DB_PATH) as conn:
        conn.execute(
            "INSERT OR REPLACE INTO saved_quizzes (quiz_id, quiz_name, payload) VALUES (?, ?, ?)",
            (quiz_id, name, payload),
        )
    return quiz_id

def list_saved_quizzes():
    with sqlite3.connect(DB_PATH) as conn:
        return conn.execute(
            "SELECT quiz_id, quiz_name, json_extract(payload, '$.current_index'), json_extract(payload, '$.total') "
            "FROM saved_quizzes ORDER BY saved_at DESC"
        ).fetchall()

def get_saved_quiz(quiz_id=None):
    with sqlite3.connect(DB_PATH) as conn:
        if quiz_id:
            row = conn.execute("SELECT quiz_id, payload FROM saved_quizzes WHERE quiz_id=?", (quiz_id,)).fetchone()
        else:
            row = conn.execute("SELECT quiz_id, payload FROM saved_quizzes ORDER BY saved_at DESC LIMIT 1").fetchone()
    return row

def is_admin(update: Update) -> bool:
    return bool(update.effective_user and update.effective_user.id == ADMIN_ID and ADMIN_ID != 0)


# ---------------- Commands ----------------
async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "👋 *Namaste! Quiz Bot Commands:*\n\n"
        "🔹 `/newquiz` - Nayi JSON quiz file upload karein (admin)\n"
        "🔹 `/restart` - Current ya Completed quiz ko shuru se restart karein (admin)\n"
        "🔹 `/share` - Quiz invite aur details direct chat me share karein\n"
        "🔹 `/pause` - Chal raha quiz pause karein (admin)\n"
        "🔹 `/resume [ID]` - Paused quiz jaari karein (admin)\n"
        "🔹 `/savedquizzes` - Archived quizzes ki list dekhein (admin)\n"
        "🔹 `/status` - Current quiz progress check karein\n"
        "🔹 `/stop` - Quiz ko band karein (admin)",
        parse_mode="Markdown"
    )

async def ask_for_quiz_file(update: Update, context: ContextTypes.DEFAULT_TYPE):
    global quiz_state
    if not is_admin(update):
        await update.message.reply_text("❌ Sirf admin naya quiz shuru kar sakta hai.")
        return

    if quiz_state and quiz_state.get("status") == "running":
        await update.message.reply_text("⚠️ Pehle /pause karein, phir nayi JSON upload karein.")
        return

    context.user_data["waiting_for_file"] = True
    context.user_data["waiting_for_timer"] = False
    await update.message.reply_text("📂 Kripya valid `.json` quiz file upload karein.")

async def handle_json_file(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.user_data.get("waiting_for_file") or not is_admin(update):
        return

    doc = update.message.document
    if not doc or not doc.file_name or not doc.file_name.lower().endswith(".json"):
        await update.message.reply_text("❌ Kripya sirf `.json` file hi bhejein.")
        return

    tg_file = await doc.get_file()
    file_bytes = await tg_file.download_as_bytearray()

    try:
        quiz_data = json.loads(file_bytes.decode("utf-8"))
        if not isinstance(quiz_data, list) or not quiz_data:
            raise ValueError("JSON ka format non-empty questions ki list hona chahiye.")
        for i, item in enumerate(quiz_data, start=1):
            if not isinstance(item, dict) or "question" not in item or "options" not in item or "answer" not in item:
                raise ValueError(f"Question {i} me question/options/answer missing hai.")
    except Exception as exc:
        await update.message.reply_text(f"❌ JSON file error: {exc}")
        return

    context.user_data["temp_quiz_data"] = quiz_data
    context.user_data["temp_quiz_name"] = Path(doc.file_name).stem
    context.user_data["waiting_for_file"] = False
    context.user_data["waiting_for_timer"] = True

    await update.message.reply_text(
        f"✅ *JSON File Valid:* Kul {len(quiz_data)} questions load hue hain.\n\n"
        f"⏱️ Har question ke liye timer kitna rakhna hai? (seconds me likhein):\n"
        f"Example: `15`, `30`, `45`, ya `60` reply karein.",
        parse_mode="Markdown"
    )

async def handle_text_messages(update: Update, context: ContextTypes.DEFAULT_TYPE):
    global quiz_state, quiz_task

    if not context.user_data.get("waiting_for_timer") or not is_admin(update):
        return

    text = update.message.text.strip()
    if not text.isdigit() or int(text) < 10 or int(text) > 300:
        await update.message.reply_text("⚠️ Kripya 10 se 300 seconds ke beech ka koi number bhejein (jaise: 30).")
        return

    timer_val = int(text)
    context.user_data["waiting_for_timer"] = False

    # Agar TARGET_CHAT_ID env me nahi hai to usi chat me send karega
    raw_chat_id = os.environ.get("TARGET_CHAT_ID")
    target_chat = int(raw_chat_id) if raw_chat_id else update.effective_chat.id

    quiz_data = context.user_data.get("temp_quiz_data")
    quiz_name = context.user_data.get("temp_quiz_name")

    archived_id = None
    if quiz_state and quiz_state.get("status") in ("running", "paused"):
        archived_id = archive_current_quiz()
        if quiz_task is not None and not quiz_task.done():
            quiz_task.cancel()
            try:
                await quiz_task
            except asyncio.CancelledError:
                pass

    quiz_state = {
        "quiz_name": quiz_name,
        "chat_id": target_chat,
        "admin_chat_id": update.effective_chat.id,
        "quiz_data": quiz_data,
        "current_index": 0,
        "timer": timer_val,
        "scores": {},
        "names": {},
        "status": "running",
        "total": len(quiz_data),
    }
    save_state()
    active_polls.clear()
    pause_event.set()

    saved_note = f"\n🗂️ Purana quiz save ho gaya (ID: `{archived_id}`)." if archived_id else ""
    await update.message.reply_text(
        f"🚀 *Quiz Start Ho Raha Hai!*\n\n"
        f"📌 *Topic:* {quiz_name}\n"
        f"📊 *Questions:* {len(quiz_data)}\n"
        f"⏱️ *Timer:* {timer_val}s per question\n"
        f"🎯 *Target Chat ID:* `{target_chat}`{saved_note}",
        parse_mode="Markdown"
    )
    quiz_task = asyncio.create_task(run_quiz(context.bot))

async def restart_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    global quiz_state, quiz_task
    if not is_admin(update):
        await update.message.reply_text("❌ Sirf admin quiz restart kar sakta hai.")
        return

    if not quiz_state or not quiz_state.get("quiz_data"):
        await update.message.reply_text("⚠️ Restart karne ke liye koi quiz nahi mila. Kripya /newquiz se upload karein.")
        return

    if quiz_task is not None and not quiz_task.done():
        quiz_task.cancel()
        try:
            await quiz_task
        except asyncio.CancelledError:
            pass

    quiz_state["current_index"] = 0
    quiz_state["scores"] = {}
    quiz_state["names"] = {}
    quiz_state["status"] = "running"
    save_state()
    active_polls.clear()
    pause_event.set()

    await update.message.reply_text(
        f"🔄 *Quiz Restart Kiya Gaya!*\n\n"
        f"📖 *Topic:* {quiz_state.get('quiz_name', 'Mock Test')}\n"
        f"Question 1 se fir se sabhi poll shuru ho rahe hain...",
        parse_mode="Markdown"
    )
    quiz_task = asyncio.create_task(run_quiz(context.bot))

async def share_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not quiz_state or not quiz_state.get("quiz_data"):
        await update.message.reply_text("Abhi koi active ya completed quiz nahi hai jise share kiya ja sake.")
        return

    name = quiz_state.get("quiz_name", "Live Quiz")
    total = quiz_state.get("total", len(quiz_state.get("quiz_data", [])))
    timer = quiz_state.get("timer", DEFAULT_TIMER)

    bot_info = await context.bot.get_me()
    bot_username = bot_info.username

    share_msg = (
        f"🎯 *Live Quiz me hissa lein!*\n\n"
        f"📖 *Topic:* {name}\n"
        f"📝 *Total Questions:* {total}\n"
        f"⏱️ *Time Limit:* {timer} seconds\n\n"
        f"👉 Join karke test attempt karein: @{bot_username}"
    )

    encoded_text = urllib.parse.quote(share_msg)
    share_url = f"https://t.me/share/url?text={encoded_text}"

    keyboard = InlineKeyboardMarkup([
        [InlineKeyboardButton("📲 Dosto ko Share Karein", url=share_url)]
    ])

    await update.message.reply_text(
        f"📢 *Quiz Invite Tayyar Hai!*\nNeeche diye button se kisi bhi friend ya group ko direct share karein:",
        reply_markup=keyboard,
        parse_mode="Markdown"
    )

async def pause_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    global quiz_state
    if not is_admin(update):
        await update.message.reply_text("❌ Sirf admin quiz pause kar sakta hai.")
        return
    if not quiz_state or quiz_state.get("status") != "running":
        await update.message.reply_text("⚠️ Koi running quiz nahi hai.")
        return

    quiz_state["status"] = "paused"
    save_state()
    pause_event.clear()
    await update.message.reply_text("⏸️ Quiz pause ho gaya hai. Agla question ruk gaya hai. /resume se jaari karein.")

async def resume_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    global quiz_state, quiz_task
    if not is_admin(update):
        await update.message.reply_text("❌ Sirf admin quiz resume kar sakta hai.")
        return

    requested_id = context.args[0] if context.args else None

    if quiz_state and quiz_state.get("status") == "paused" and not requested_id:
        quiz_state["status"] = "running"
        save_state()
        pause_event.set()
        if quiz_task is None or quiz_task.done():
            quiz_task = asyncio.create_task(run_quiz(context.bot))
        await update.message.reply_text(
            f"▶️ Quiz resume ho gaya! Question {quiz_state['current_index'] + 1}/{quiz_state['total']} se aage continue ho raha hai."
        )
        return

    row = get_saved_quiz(requested_id)
    if not row:
        await update.message.reply_text("⚠️ Saved quiz nahi mila. /savedquizzes dekhein.")
        return

    if quiz_state and quiz_state.get("status") == "running":
        await update.message.reply_text("⚠️ Pehle current quiz ko /pause karein.")
        return
    if quiz_state and quiz_state.get("status") == "paused":
        archive_current_quiz()

    selected_id, payload = row
    quiz_state = json.loads(payload)
    quiz_state["status"] = "running"
    save_state()
    with sqlite3.connect(DB_PATH) as conn:
        conn.execute("DELETE FROM saved_quizzes WHERE quiz_id=?", (selected_id,))
    active_polls.clear()
    pause_event.set()
    if quiz_task is not None and not quiz_task.done():
        quiz_task.cancel()
        try:
            await quiz_task
        except asyncio.CancelledError:
            pass
    quiz_task = asyncio.create_task(run_quiz(context.bot))
    await update.message.reply_text(f"▶️ Saved Quiz `{selected_id}` resume ho gaya!")

async def savedquizzes_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update):
        await update.message.reply_text("❌ Sirf admin saved quizzes dekh sakta hai.")
        return
    rows = list_saved_quizzes()
    if not rows:
        await update.message.reply_text("Koi saved quiz nahi hai.")
        return
    lines = ["🗂️ *Saved Quizzes Library:*"]
    for q_id, name, done, total in rows:
        lines.append(f"• `{q_id}` | *{name}* | Progress: {done or 0}/{total or 0}")
    lines.append("\nResume karne ke liye: `/resume ID`")
    await update.message.reply_text("\n".join(lines), parse_mode="Markdown")

async def status_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not quiz_state:
        await update.message.reply_text("Abhi koi active quiz nahi hai.")
        return

    done = quiz_state.get("current_index", 0)
    total = quiz_state.get("total", len(quiz_state.get("quiz_data", [])))
    status = quiz_state.get("status", "unknown")
    await update.message.reply_text(
        f"📊 *Quiz Status:* `{status.upper()}`\n"
        f"📖 *Topic:* {quiz_state.get('quiz_name', 'N/A')}\n"
        f"✅ *Progress:* {done}/{total} Questions\n"
        f"👥 *Active Participants:* {len(quiz_state.get('names', {}))}",
        parse_mode="Markdown"
    )

async def stop_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    global quiz_state
    if not is_admin(update):
        await update.message.reply_text("❌ Sirf admin quiz stop kar sakta hai.")
        return
    if not quiz_state or quiz_state.get("status") not in ("running", "paused"):
        await update.message.reply_text("⚠️ Koi active quiz nahi hai.")
        return

    quiz_state["status"] = "stopped"
    save_state()
    pause_event.set()
    await update.message.reply_text("⏹️ Quiz band kar diya gaya hai.")


# ---------------- Quiz Runner ----------------
async def run_quiz(bot):
    global quiz_state
    if not quiz_state:
        return

    chat_id = quiz_state["chat_id"]
    admin_chat = quiz_state.get("admin_chat_id", chat_id)
    quiz_data = quiz_state["quiz_data"]
    total_q = len(quiz_data)
    timer = quiz_state.get("timer", DEFAULT_TIMER)
    mapping = {"A": 0, "B": 1, "C": 2, "D": 3}

    if quiz_state.get("current_index", 0) == 0:
        try:
            await bot.send_message(
                chat_id=chat_id,
                text=(
                    "🔥 *QUIZ SHURU HO RAHA HAI!* 🔥\n"
                    "━━━━━━━━━━━━━━━━━━\n"
                    f"📖 *Topic:* {quiz_state.get('quiz_name', 'Mock Test')}\n"
                    f"📊 *Total Questions:* {total_q}\n"
                    f"⏱️ *Timer:* {timer} seconds\n"
                    "━━━━━━━━━━━━━━━━━━\n"
                    "👉 Sabhi candidates taiyar ho jayein!"
                ),
                parse_mode="Markdown",
            )
        except Exception as exc:
            err_msg = f"⚠️ Quiz Start Message Error (Chat ID `{chat_id}`): {exc}"
            print(err_msg)
            if admin_chat != chat_id:
                try:
                    await bot.send_message(chat_id=admin_chat, text=err_msg, parse_mode="Markdown")
                except Exception:
                    pass

    while quiz_state and quiz_state.get("current_index", 0) < total_q:
        await pause_event.wait()

        if not quiz_state or quiz_state.get("status") == "stopped":
            return
        if quiz_state.get("status") == "paused":
            await asyncio.sleep(1)
            continue

        index = quiz_state["current_index"]
        item = quiz_data[index]
        q_num = item.get("number", index + 1)
        q_text = f"[{index + 1}/{total_q}] Q{q_num}. {item.get('question', '')}"
        options_dict = item.get("options", {})
        options = [options_dict[k] for k in ("A", "B", "C", "D") if k in options_dict]

        if len(options) < 2:
            quiz_state["current_index"] = index + 1
            save_state()
            continue

        correct_letter = str(item.get("answer", "A")).strip().upper()
        correct_index = mapping.get(correct_letter, 0)
        if correct_index >= len(options):
            correct_index = 0

        try:
            poll_msg = await bot.send_poll(
                chat_id=chat_id,
                question=q_text[:300],
                options=[str(opt)[:100] for opt in options],
                type="quiz",
                correct_option_id=correct_index,
                is_anonymous=False,
                open_period=timer,
            )
            active_polls[poll_msg.poll.id] = correct_index
            await asyncio.sleep(timer + 2)
        except Exception as exc:
            err_msg = f"⚠️ Poll Error: {exc}"
            print(err_msg)
            if admin_chat != chat_id:
                try:
                    await bot.send_message(chat_id=admin_chat, text=err_msg)
                except Exception:
                    pass
            await asyncio.sleep(3)

        quiz_state["current_index"] = index + 1
        save_state()

    if quiz_state and quiz_state.get("status") == "running":
        quiz_state["status"] = "completed"
        save_state()
        await send_leaderboard(bot, chat_id, total_q)


async def handle_poll_answer(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not quiz_state:
        return

    answer = update.poll_answer
    user = answer.user
    full_name = f"{user.first_name} {user.last_name or ''}".strip()

    if answer.poll_id not in active_polls:
        return

    correct_option_id = active_polls[answer.poll_id]
    uid = str(user.id)
    quiz_state.setdefault("names", {})[uid] = full_name
    quiz_state.setdefault("scores", {}).setdefault(uid, 0)

    if answer.option_ids and answer.option_ids[0] == correct_option_id:
        quiz_state["scores"][uid] += 1

    save_state()


# ---------------- Leaderboard ----------------
async def send_leaderboard(bot, chat_id, total_q):
    names = quiz_state.get("names", {})
    scores = quiz_state.get("scores", {})
    quiz_name = quiz_state.get("quiz_name", "Test")

    if not names:
        await bot.send_message(
            chat_id=chat_id, 
            text=(
                "🏁 *Quiz Complete ho gaya!*\n\n"
                "Kisi bhi candidate ne attempt nahi kiya.\n"
                "Usi quiz ko fir se chalane ke liye admin `/restart` bhej sakte hain."
            ),
            parse_mode="Markdown"
        )
        return

    sorted_users = sorted(names.keys(), key=lambda uid: scores.get(uid, 0), reverse=True)
    res = [
        "╔════════════════════════╗",
        "       🏆 *FINAL LEADERBOARD* 🏆",
        "╚════════════════════════╝\n",
        f"📖 *Topic:* {quiz_name}",
        f"📊 *Questions:* {total_q}\n",
    ]

    medals = ["🥇", "🥈", "🥉"]
    for i, uid in enumerate(sorted_users[:10]):
        rank_icon = medals[i] if i < 3 else f"{i+1}."
        sc = scores.get(uid, 0)
        pct = round((sc / total_q) * 100, 1) if total_q > 0 else 0
        res.append(f"{rank_icon} *{names[uid]}* — {sc}/{total_q} ({pct}%)")

    await bot.send_message(chat_id=chat_id, text="\n".join(res), parse_mode="Markdown")


# ---------------- Application Entrypoint ----------------
def main():
    init_db()
    global quiz_state
    quiz_state = load_state()

    application = ApplicationBuilder().token(BOT_TOKEN).build()

    application.add_handler(CommandHandler("start", start_command))
    application.add_handler(CommandHandler("newquiz", ask_for_quiz_file))
    application.add_handler(CommandHandler("restart", restart_command))
    application.add_handler(CommandHandler("share", share_command))
    application.add_handler(CommandHandler("pause", pause_command))
    application.add_handler(CommandHandler("resume", resume_command))
    application.add_handler(CommandHandler("savedquizzes", savedquizzes_command))
    application.add_handler(CommandHandler("status", status_command))
    application.add_handler(CommandHandler("stop", stop_command))

    application.add_handler(MessageHandler(filters.Document.ALL, handle_json_file))
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text_messages))
    application.add_handler(PollAnswerHandler(handle_poll_answer))

    application.run_polling()

if __name__ == "__main__":
    main()
