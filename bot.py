import os
import json
import asyncio
import sqlite3
import threading
import uuid
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
share_poll_map = {} # poll_id -> {user_id, correct_index}
shared_sessions = {} # user_id -> per-user shared quiz session
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
        conn.execute("""
            CREATE TABLE IF NOT EXISTS saved_quizzes (
                quiz_id TEXT PRIMARY KEY,
                quiz_name TEXT NOT NULL,
                payload TEXT NOT NULL,
                saved_at TEXT DEFAULT CURRENT_TIMESTAMP
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS completed_quizzes (
                quiz_id TEXT PRIMARY KEY,
                quiz_name TEXT NOT NULL,
                payload TEXT NOT NULL,
                created_at TEXT DEFAULT CURRENT_TIMESTAMP
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


def archive_current_quiz():
    """Keep a paused quiz as a separate resumable entry."""
    if not quiz_state:
        return None
    import time
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


def save_completed_quiz():
    """Save an immutable copy of a completed quiz for sharing."""
    if not quiz_state or quiz_state.get("status") != "completed":
        return None
    if quiz_state.get("share_id"):
        return quiz_state["share_id"]
    quiz_id = uuid.uuid4().hex[:12]
    name = quiz_state.get("quiz_name") or f"Quiz-{quiz_id}"
    payload_data = {
        "quiz_name": name,
        "quiz_data": quiz_state.get("quiz_data", []),
        "total": quiz_state.get("total", len(quiz_state.get("quiz_data", []))),
    }
    payload = json.dumps(payload_data, ensure_ascii=False)
    with sqlite3.connect(DB_PATH) as conn:
        conn.execute(
            "INSERT INTO completed_quizzes (quiz_id, quiz_name, payload) VALUES (?, ?, ?)",
            (quiz_id, name, payload),
        )
    quiz_state["share_id"] = quiz_id
    save_state()
    return quiz_id


def list_completed_quizzes():
    with sqlite3.connect(DB_PATH) as conn:
        return conn.execute(
            "SELECT quiz_id, quiz_name, created_at FROM completed_quizzes ORDER BY created_at DESC"
        ).fetchall()


def get_completed_quiz(quiz_id):
    with sqlite3.connect(DB_PATH) as conn:
        return conn.execute(
            "SELECT quiz_id, quiz_name, payload FROM completed_quizzes WHERE quiz_id=?",
            (quiz_id,),
        ).fetchone()


def is_admin(update: Update) -> bool:
    return bool(update.effective_user and update.effective_user.id == ADMIN_ID and ADMIN_ID != 0)


# ---------------- Commands ----------------
async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    # Deep-link format: https://t.me/BotUsername?start=share_QUIZID
    if context.args and context.args[0].startswith("share_"):
        quiz_id = context.args[0][6:]
        row = get_completed_quiz(quiz_id)
        if not row:
            await update.message.reply_text("⚠️ Yeh shared quiz link valid nahi hai ya quiz available nahi hai.")
            return
        try:
            quiz_payload = json.loads(row[2])
        except Exception:
            await update.message.reply_text("❌ Is quiz ka data read nahi ho pa raha hai.")
            return
        uid = str(update.effective_user.id)
        if uid in shared_sessions:
            await update.message.reply_text("⏳ Aapka ek shared quiz pehle se chal raha hai. Pehle use complete karein.")
            return
        task = asyncio.create_task(run_shared_quiz(context.bot, update.effective_chat.id, uid, quiz_id, quiz_payload))
        shared_sessions[uid] = {"task": task, "score": 0, "answers": {}, "name": row[1]}
        await update.message.reply_text(f"✅ {row[1]} shuru ho raha hai! Har question ka jawab select karein.")
        return

    await update.message.reply_text(
        "👋 Namaste! Quiz control commands:\n"
        "/newquiz - Nayi JSON quiz upload karein (admin)\n"
        "/pause - Chal raha quiz pause karein (admin)\n"
        "/resume [ID] - Purana quiz wahi se resume karein (admin)\n"
        "/savedquizzes - Saved quizzes ki list dekhein (admin)\n"
        "/completedquizzes - Completed Quiz list (admin)\n"
        "/sharequiz ID - Completed Quiz ka link banayein (admin)\n"
        "/status - Quiz progress dekhein\n"
        "/stop - Quiz band karein (admin)"
    )


async def completedquizzes_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update):
        await update.message.reply_text("❌ Sirf admin completed quizzes dekh sakta hai.")
        return
    # Import the latest completed quiz from older bot versions, if present.
    save_completed_quiz()
    rows = list_completed_quizzes()
    if not rows:
        await update.message.reply_text("Abhi koi Completed Quiz share ke liye save nahi hua.")
        return
    lines = ["🏁 Completed Quizzes:"]
    for quiz_id, name, created_at in rows:
        lines.append(f"• {name} | ID: {quiz_id} | {created_at}")
    lines.append("\nShare link banane ke liye: /sharequiz ID")
    await update.message.reply_text("\n".join(lines))


async def sharequiz_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update):
        await update.message.reply_text("❌ Sirf admin Quiz Share Link bana sakta hai.")
        return
    if not context.args:
        await update.message.reply_text("Pehle /completedquizzes bhejein, phir /sharequiz ID use karein.")
        return
    # Ensure a completed quiz from the previous bot version is also shareable.
    save_completed_quiz()
    quiz_id = context.args[0].strip()
    row = get_completed_quiz(quiz_id)
    if not row:
        await update.message.reply_text("⚠️ Is ID ka Completed Quiz nahi mila. /completedquizzes dekhein.")
        return
    me = await context.bot.get_me()
    link = f"https://t.me/{me.username}?start=share_{quiz_id}"
    await update.message.reply_text(
        f"📤 *Quiz Share Link Ready!*\n\n📚 Quiz: {row[1]}\n\n🔗 {link}\n\nIs link ko Group, Channel ya students ke saath share karein. Student link kholkar Start dabayega aur apna quiz attempt karega.",
        parse_mode="Markdown",
        disable_web_page_preview=True,
    )


async def ask_for_quiz_file(update: Update, context: ContextTypes.DEFAULT_TYPE):
    global quiz_state

    if not is_admin(update):
        await update.message.reply_text("❌ Sirf admin naya quiz shuru kar sakta hai.")
        return

    if quiz_state and quiz_state.get("status") == "running":
        await update.message.reply_text("⚠️ Pehle /pause karein, phir nayi JSON upload karein.")
        return
    if quiz_state and quiz_state.get("status") == "paused":
        await update.message.reply_text("⏸️ Current quiz paused hai. Ab nayi JSON upload kar sakte hain; purana quiz saved rahega.")

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

    # Preserve the paused quiz before switching to the newly uploaded quiz.
    archived_id = None
    if quiz_state and quiz_state.get("status") == "paused":
        archived_id = archive_current_quiz()
        if quiz_task is not None and not quiz_task.done():
            quiz_task.cancel()
            try:
                await quiz_task
            except asyncio.CancelledError:
                pass

    quiz_state = {
        "quiz_name": Path(doc.file_name).stem,
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

    saved_note = f"\n🗂️ Purana quiz ID: {archived_id} ( /resume {archived_id} se wapas la sakte hain)." if archived_id else ""
    await update.message.reply_text(
        f"✅ JSON receive ho gayi! Total {len(quiz_data)} questions shuru ho rahe hain.{saved_note}"
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

    requested_id = context.args[0] if context.args else None
    # No ID: resume the currently paused quiz, if one exists.
    if quiz_state and quiz_state.get("status") == "paused" and not requested_id:
        quiz_state["status"] = "running"
        save_state()
        pause_event.set()
        if quiz_task is None or quiz_task.done():
            quiz_task = asyncio.create_task(run_quiz(context.bot))
        await update.message.reply_text(
            f"▶️ Quiz resume ho gaya! Progress: {quiz_state['current_index']}/{quiz_state['total']} questions complete."
        )
        return

    # An ID (or no active paused quiz) selects an archived quiz.
    row = get_saved_quiz(requested_id)
    if not row:
        await update.message.reply_text("⚠️ Saved quiz nahi mila. /savedquizzes se IDs dekhein.")
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
    await update.message.reply_text(
        f"▶️ {quiz_state.get('quiz_name', 'Purana Quiz')} resume ho gaya! "
        f"Progress: {quiz_state.get('current_index', 0)}/{quiz_state.get('total', 0)}."
    )


async def savedquizzes_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update):
        await update.message.reply_text("❌ Sirf admin saved quizzes dekh sakta hai.")
        return
    rows = list_saved_quizzes()
    if not rows:
        await update.message.reply_text("Koi archived quiz nahi hai.")
        return
    lines = ["🗂️ Saved quizzes:"]
    for quiz_id, name, done, total in rows:
        lines.append(f"• {name} | ID: {quiz_id} | Progress: {done or 0}/{total or 0}")
    lines.append("Resume: /resume ID")
    await update.message.reply_text("\n".join(lines))


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
        completed_id = save_completed_quiz()
        print(f"Completed quiz archived for sharing. ID: {completed_id}")


async def handle_poll_answer(update: Update, context: ContextTypes.DEFAULT_TYPE):
    answer = update.poll_answer

    # Poll answers from individual shared-quiz sessions
    shared_info = share_poll_map.get(answer.poll_id)
    if shared_info:
        uid = shared_info["user_id"]
        session = shared_sessions.get(uid)
        if session is not None:
            was_correct = session["answers"].get(answer.poll_id, False)
 
