import os
import threading
from flask import Flask

web_app = Flask(__name__)

@web_app.route('/')
def home():
    return "Bot is running!"

def run_web():
    port = int(os.environ.get("PORT", 8080))
    web_app.run(host="0.0.0.0", port=port)

# Web server background thread me start karein
threading.Thread(target=run_web, daemon=True).start()

import os
import json
import asyncio
from collections import defaultdict
from telegram import Update
from telegram.ext import (
    ApplicationBuilder,
    CommandHandler,
    MessageHandler,
    PollAnswerHandler,
    filters,
    ContextTypes,
)

# Render Environment Variables se read karega
BOT_TOKEN = os.environ.get("BOT_TOKEN")
ADMIN_ID = int(os.environ.get("ADMIN_ID", "0"))  # Apna personal Telegram User ID dalein

QUESTION_TIMER = 30  # Har question ka time seconds me

# Quiz states
quiz_in_progress = False
quiz_paused = False
pause_event = asyncio.Event()
pause_event.set()  # Default state: Unpaused
user_scores = defaultdict(int)
user_names = {}
active_polls = {}

async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "👋 Namaste! Naya quiz run karne ke liye:\n"
        "1. `/newquiz` command bhejein.\n"
        "2. Bot ke mangne par nayi JSON file upload karein."
    )

async def ask_for_quiz_file(update: Update, context: ContextTypes.DEFAULT_TYPE):
    global quiz_in_progress
    if update.effective_user.id != ADMIN_ID:
        await update.message.reply_text("❌ Aapko quiz shuru karne ki permission nahi hai.")
        return

    if quiz_in_progress:
        await update.message.reply_text("⚠️ Ek quiz pehle se chal raha hai. Uske khatam hone tak wait karein.")
        return

    context.user_data["waiting_for_file"] = True
    await update.message.reply_text("📂 Kripya naye quiz ki `.json` file upload karein.")

async def handle_json_file(update: Update, context: ContextTypes.DEFAULT_TYPE):
    global quiz_in_progress, user_scores, user_names, active_polls

    if not context.user_data.get("waiting_for_file"):
        return

    doc = update.message.document
    if not doc.file_name.endswith(".json"):
        await update.message.reply_text("❌ Kripya valid `.json` file hi bhejein.")
        return

    file = await doc.get_file()
    file_bytes = await file.download_as_bytearray()

    try:
        quiz_data = json.loads(file_bytes.decode("utf-8"))
    except Exception as e:
        await update.message.reply_text(f"❌ JSON file padhne me error aayi: {e}")
        return

    context.user_data["waiting_for_file"] = False
    target_chat_id = os.environ.get("TARGET_CHAT_ID")  # Group/Channel ID

    await update.message.reply_text(f"✅ File receive ho gayi! Total {len(quiz_data)} questions group me shuru ho rahe hain...")

    # State reset
    quiz_in_progress = True
    user_scores.clear()
    user_names.clear()
    active_polls.clear()

    # Background task me quiz start karein
    asyncio.create_task(run_quiz(context.bot, target_chat_id, quiz_data))

async def run_quiz(bot, chat_id, quiz_data):
    global quiz_in_progress
    mapping = {"A": 0, "B": 1, "C": 2, "D": 3}
    total_q = len(quiz_data)

    await bot.send_message(
        chat_id=chat_id,
        text=f"🎯 *New Quiz Shuru Ho Raha Hai!*\n\n📊 Total Questions: {total_q}\n⏱️ Time per Question: {QUESTION_TIMER}s\n\nTaiyar ho jayein sabhi log!",
        parse_mode="Markdown"
    )
    await asyncio.sleep(4)

    for item in quiz_data:
        q_num = item.get("number", "")
        q_text = f"Q{q_num}. {item['question']}"
        options_dict = item.get("options", {})
        options = [v for k, v in options_dict.items()]

        if len(options) < 2:
            continue

        correct_letter = item.get("answer", "A").strip().upper()
        correct_index = mapping.get(correct_letter, 0)
        if correct_index >= len(options):
            correct_index = 0

        try:
            msg = await bot.send_poll(
                chat_id=chat_id,
                question=q_text[:300],
                options=[opt[:100] for opt in options],
                type="quiz",
                correct_option_id=correct_index,
                is_anonymous=False,
                open_period=QUESTION_TIMER
            )
            active_polls[msg.poll.id] = correct_index
            await asyncio.sleep(QUESTION_TIMER + 2)
        except Exception as e:
            print(f"Error sending poll: {e}")
            await asyncio.sleep(3)

    # Quiz khatam hone par scorecard
    await send_leaderboard(bot, chat_id, total_q)
    quiz_in_progress = False

async def handle_poll_answer(update: Update, context: ContextTypes.DEFAULT_TYPE):
    answer = update.poll_answer
    poll_id = answer.poll_id
    user = answer.user

    full_name = f"{user.first_name} {user.last_name or ''}".strip()
    user_names[user.id] = full_name

    if poll_id in active_polls:
        correct_option_id = active_polls[poll_id]
        if answer.option_ids and answer.option_ids[0] == correct_option_id:
            user_scores[user.id] += 1

async def send_leaderboard(bot, chat_id, total_q):
    if not user_names:
        await bot.send_message(chat_id=chat_id, text="🏁 Quiz samaapt ho gaya! Kisi candidate ne attempt nahi kiya.")
        return

    sorted_users = sorted(user_names.keys(), key=lambda uid: user_scores[uid], reverse=True)
    topper_id = sorted_users[0]
    topper_name = user_names[topper_id]
    topper_score = user_scores[topper_id]

    res = "🏆 *QUIZ COMPLETED - FINAL RESULT* 🏆\n"
    res += "═══════════════════════\n\n"
    res += f"🥇 *TOPPER: {topper_name}*\n"
    res += f"🎯 Score: `{topper_score}/{total_q}` Marks\n"
    res += "🎉 Congratulations to the Winner! 👏\n\n"
    res += "═══════════════════════\n"
    res += "📋 *Scorecard:*\n\n"

    emojis = {1: "🥇", 2: "🥈", 3: "🥉"}
    for rank, uid in enumerate(sorted_users, start=1):
        badge = emojis.get(rank, f"#{rank}")
        res += f"{badge} {user_names[uid]}: `{user_scores[uid]}/{total_q}`\n"

    await bot.send_message(chat_id=chat_id, text=res, parse_mode="Markdown")

def main():
    app = ApplicationBuilder().token(BOT_TOKEN).build()

    app.add_handler(CommandHandler("start", start_command))
    app.add_handler(CommandHandler("newquiz", ask_for_quiz_file))
    app.add_handler(MessageHandler(filters.Document.ALL, handle_json_file))
    app.add_handler(PollAnswerHandler(handle_poll_answer))

    print("Bot live hai aur updates sun raha hai...")
    app.run_polling()

if __name__ == "__main__":
    main()
