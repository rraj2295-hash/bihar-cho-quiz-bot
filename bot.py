import os
import json
import asyncio
import sqlite3
import threading
from pathlib import Path
from flask import Flask
from telegram import Update
from telegram.ext import (
    ApplicationBuilder,
    CommandHandler,
    MessageHandler,
    PollAnswerHandler,
    filters,
    ContextTypes,
)

# ---------------- Render Web Service ----------------
web_app = Flask(__name__)

@web_app.route("/")
def home():
    return "Telegram Quiz Bot is running!"

def run_web():
    port = int(os.environ.get("PORT", 8080))
    web_app.run(host="0.0.0.0", port=port)

threading.Thread(target=run_web, daemon=True).start()

# ---------------- Configuration ----------------
BOT_TOKEN = os.environ.get("BOT_TOKEN")
ADMIN_ID = int(os.environ.get("ADMIN_ID", "0"))
QUESTION_TIMER = int(os.environ.get("QUESTION_TIMER", "30"))

# On Render, set DATA_DIR=/var/data and attach a Persistent Disk at /var/data.
# Locally, it uses a ./data folder.
DATA_DIR = Path(os.environ.get("DATA_DIR", "./data"))
DATA_DIR.mkdir(parents=True, exist_ok=True)
DB_PATH = DATA_DIR / "quiz_progress.sqlite3"

# In-memory runtime objects. Durable quiz progress is stored in SQLite.
pause_event = asyncio.Event()
pause_event.set()
active_polls = {}   # poll_id -> correct option index
quiz_task = None
quiz_state = None


# ---------------- SQLite persistence ----------------
def init_db():
    with sqlite3.connect(DB_PATH) as conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS quiz_state (
                id INTEGER PRIMARY KEY CHECK (id = 1),
                payload TEXT NOT NULL
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
        row = conn.execute(
            "SELECT payload FROM quiz_state WHERE id = 1"
        ).fetchone()
    return json.loads(row[0]) if row else None


def is_admin(update: Update) -> bool:
    return bool(update.effective_user and update.effective_user.id == ADMIN_ID and ADMIN_ID != 0)


# ---------------- Commands ----------------
async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "👋 Namaste! Quiz control commands:\n"
        "/newquiz - Nayi JSON quiz upload karein (admin)\n"
        "/pause - Chal raha quiz pause karein (admin)\n"
        "/resume - Quiz wahi se resume karein (admin)\n"
        "/status - Quiz progress dekhein\n"
        "/stop - Quiz band karein (admin)"
    )


async def ask_for_quiz_file(update: Update, context: ContextTypes.DEFAULT_TYPE):
    global quiz_state

    if not is_admin(update):
        await update.message.reply_text("❌ Sirf admin naya quiz shuru kar sakta hai.")
        return

    if quiz_state and quiz_state.get("status") in ("running", "paused"):
        await update.message.reply_text(
            "⚠️ Ek quiz pehle se active hai. Pehle /stop karein ya use complete hone dein."
        )
        return

    context.user_data["waiting_for_file"] = True
    await update.message.reply_text("📂 Kripya valid `.json` quiz file upload karein.")


async def handle_json_file(update: Update, context: ContextTypes.DEFAULT_TYPE):
    global quiz_state, quiz_task

    if not context.user_data.get("waiting_for_file"):
        return
    if not is_admin(update):
        return

    doc = update.message.document
    if not doc.file_name or not doc.file_name.lower().endswith(".json"):
        await update.message.reply_text("❌ Kripya `.json` file hi bhejein.")
        return

    tg_file = await doc.get_file()
    file_bytes = await tg_file.download_as_bytearray()

    try:
        quiz_data = json.loads(file_bytes.decode("utf-8"))
        if not isinstance(quiz_data, list) or not quiz_data:
            raise ValueError("JSON ka top-level format non-empty questions ki list hona chahiye.")
        for i, item in enumerate(quiz_data, start=1):
            if not isinstance(item, dict) or "question" not in item or "options" not in item or "answer" not in item:
                raise ValueError(f"Question {i} me question/options/answer field missing hai.")
            if not isinstance(item["options"], dict):
                raise ValueError(f"Question {i} ka options object valid nahi hai.")
    except Exception as exc:
        await update.message.reply_text(f"❌ JSON file me error: {exc}")
        return

    raw_chat_id = os.environ.get("TARGET_CHAT_ID")
    if not raw_chat_id:
        await update.message.reply_text("❌ Render Environment me TARGET_CHAT_ID set nahi hai.")
        return

    context.user_data["waiting_for_file"] = False
    quiz_state = {
        "chat_id": int(raw_chat_id),
        "quiz_data": quiz_data,
        "current_index": 0,
        "scores": {},
        "names": {},
        "status": "running",
        "total": len(quiz_data),
    }
    save_state()
    active_polls.clear()
    pause_event.set()

    await update.message.reply_text(
        f"✅ JSON receive ho gayi! Total {len(quiz_data)} questions shuru ho rahe hain."
    )
    quiz_task = asyncio.create_task(run_quiz(context.bot))


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
    await update.message.reply_text(
        "⏸️ Quiz pause request save ho gayi. Current poll ka timer khatam hone ke baad agla question ruk jayega. /resume se jaari karein."
    )


async def resume_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    global quiz_state, quiz_task
    if not is_admin(update):
        await update.message.reply_text("❌ Sirf admin quiz resume kar sakta hai.")
        return
    if not quiz_state or quiz_state.get("status") != "paused":
        await update.message.reply_text("⚠️ Koi paused quiz nahi hai.")
        return

    quiz_state["status"] = "running"
    save_state()
    pause_event.set()

    if quiz_task is None or quiz_task.done():
        quiz_task = asyncio.create_task(run_quiz(context.bot))

    await update.message.reply_text(
        f"▶️ Quiz resume ho gaya! Progress: {quiz_state['current_index']}/{quiz_state['total']} questions complete."
    )


async def status_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not quiz_state:
        await update.message.reply_text("Abhi koi quiz save nahi hai.")
        return

    done = quiz_state.get("current_index", 0)
    total = quiz_state.get("total", len(quiz_state.get("quiz_data", [])))
    status = quiz_state.get("status", "unknown")
    await update.message.reply_text(
        f"📊 Quiz status: {status.upper()}\n"
        f"✅ Questions completed: {done}/{total}\n"
        f"👥 Participants: {len(quiz_state.get('names', {}))}"
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
    await update.message.reply_text("⏹️ Quiz stop kar diya gaya hai. Is quiz ko /resume nahi kar sakte; naya quiz /newquiz se shuru karein.")


# ---------------- Quiz runner ----------------
async def run_quiz(bot):
    global quiz_state

    if not quiz_state:
        return

    chat_id = quiz_state["chat_id"]
    quiz_data = quiz_state["quiz_data"]
    total_q = len(quiz_data)
    mapping = {"A": 0, "B": 1, "C": 2, "D": 3}

    if quiz_state.get("current_index", 0) == 0:
        await bot.send_message(
            chat_id=chat_id,
            text=(
                "🎯 *Quiz shuru ho raha hai!*\n\n"
                f"📊 Total Questions: {total_q}\n"
                f"⏱️ Time per Question: {QUESTION_TIMER}s"
            ),
            parse_mode="Markdown",
        )

    while quiz_state and quiz_state.get("current_index", 0) < total_q:
        await pause_event.wait()

        if not quiz_state or quiz_state.get("status") == "stopped":
            return
        if quiz_state.get("status") == "paused":
            continue

        index = quiz_state["current_index"]
        item = quiz_data[index]
        q_num = item.get("number", index + 1)
        q_text = f"Q{q_num}. {item.get('question', '')}"
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
                open_period=QUESTION_TIMER,
            )
            active_polls[poll_msg.poll.id] = correct_index
            await asyncio.sleep(QUESTION_TIMER + 2)
        except Exception as exc:
            print(f"Poll send error: {exc}")
            await asyncio.sleep(3)

        # Current question is complete. Save next index before continuing.
        quiz_state["current_index"] = index + 1
        save_state()

    if quiz_state and quiz_state.get("status") == "running":
        await send_leaderboard(bot, chat_id, total_q)
        quiz_state["status"] = "completed"
        save_state()


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


async def send_leaderboard(bot, chat_id, total_q):
    names = quiz_state.get("names", {})
    scores = quiz_state.get("scores", {})

    if not names:
        await bot.send_message(chat_id=chat_id, text="🏁 Quiz samaapt! Kisi candidate ne attempt nahi kiya.")
        return

    sorted_users = sorted(names.keys(), key=lambda uid: scores.get(uid, 0), reverse=True)
    topper_id = sorted_users[0]

    result = "🏆 *QUIZ COMPLETED - FINAL RESULT* 🏆\n"
    result += "═══════════════════════\n\n"
    result += f"🥇 *TOPPER: {names[topper_id]}*\n"
    result += f"🎯 Score: `{scores.get(topper_id, 0)}/{total_q}` Marks\n"
    result += "🎉 Congratulations! 👏\n\n📋 *Scorecard:*\n\n"

    medals = {1: "🥇", 2: "🥈", 3: "🥉"}
    for rank, uid in enumerate(sorted_users, start=1):
        result += f"{medals.get(rank, f'#{rank}')} {names[uid]}: `{scores.get(uid, 0)}/{total_q}`\n"

    await bot.send_message(chat_id=chat_id, text=result, parse_mode="Markdown")


# ---------------- Startup / errors ----------------
async def restore_quiz(application):
    global quiz_state, quiz_task
    quiz_state = load_state()

    if quiz_state and quiz_state.get("status") == "running":
        pause_event.set()
        quiz_task = asyncio.create_task(run_quiz(application.bot))
        print("Saved quiz restored from SQLite.")
    elif quiz_state and quiz_state.get("status") == "paused":
        pause_event.clear()
        print("Saved quiz is paused. Admin can send /resume.")
    else:
        pause_event.set()


async def error_handler(update, context):
    print(f"Telegram/network error: {context.error}")


def main():
    if not BOT_TOKEN:
        raise RuntimeError("BOT_TOKEN environment variable set nahi hai.")

    init_db()

    app = (
        ApplicationBuilder()
        .token(BOT_TOKEN)
        .connect_timeout(30.0)
        .read_timeout(30.0)
        .write_timeout(30.0)
        .pool_timeout(30.0)
        .post_init(restore_quiz)
        .build()
    )

    app.add_handler(CommandHandler("start", start_command))
    app.add_handler(CommandHandler("newquiz", ask_for_quiz_file))
    app.add_handler(CommandHandler("pause", pause_command))
    app.add_handler(CommandHandler("resume", resume_command))
    app.add_handler(CommandHandler("status", status_command))
    app.add_handler(CommandHandler("stop", stop_command))
    app.add_handler(MessageHandler(filters.Document.ALL, handle_json_file))
    app.add_handler(PollAnswerHandler(handle_poll_answer))
    app.add_error_handler(error_handler)

    print("Bot live hai aur updates sun raha hai...")
    app.run_polling(drop_pending_updates=False)


if __name__ == "__main__":
    main()
