import os
import random
from telegram import Update, Poll
from telegram.ext import (
    ApplicationBuilder,
    CommandHandler,
    PollAnswerHandler,
    ContextTypes,
)

# Render पर Environment Variable से BOT_TOKEN लिया जाएगा
BOT_TOKEN = os.getenv("BOT_TOKEN")

# Bihar CHO परीक्षा के लिए महत्वपूर्ण उच्च-प्राथमिकता प्रश्न बैंक
QUIZ_QUESTIONS = [
    {
        "question": "Asha worker covers a rural population of approximately:",
        "options": ["500", "1,000", "2,500", "5,000"],
        "correct_option_id": 1,
        "explanation": "Generally, one ASHA (Accredited Social Health Activist) is designated per 1,000 rural population.",
    },
    {
        "question": "Which vaccine is administered to a newborn immediately at birth?",
        "options": ["BCG, OPV, and Hepatitis B", "DPT and Measles", "Pentavalent and Rotavirus", "MMR only"],
        "correct_option_id": 0,
        "explanation": "Birth doses include BCG, OPV 0 dose, and Hepatitis B birth dose within 24 hours.",
    },
    {
        "question": "The dose of Vitamin A solution given to an infant at 9 months of age is:",
        "options": ["50,000 IU", "1,00,000 IU", "2,00,000 IU", "3,00,000 IU"],
        "correct_option_id": 1,
        "explanation": "The first dose of Vitamin A at 9 months with Measles/MR vaccine is 1 Lakh IU (1 ml). Subsequent doses are 2 Lakh IU.",
    },
    {
        "question": "Exclusive breastfeeding is recommended up to what age?",
        "options": ["3 months", "4 months", "6 months", "1 year"],
        "correct_option_id": 2,
        "explanation": "WHO and National Health Guidelines recommend exclusive breastfeeding for the first 6 months of life.",
    },
    {
        "question": "In a Community Health Center (CHC), the standard bed capacity is usually:",
        "options": ["10 beds", "30 beds", "50 beds", "100 beds"],
        "correct_option_id": 1,
        "explanation": "A standard Community Health Centre (CHC) is designed as a 30-bedded hospital serving as a referral unit.",
    },
    {
        "question": "The antidote for Magnesium Sulfate (MgSO4) toxicity in eclampsia management is:",
        "options": ["Calcium Gluconate", "Potassium Chloride", "Sodium Bicarbonate", "Naloxone"],
        "correct_option_id": 0,
        "explanation": "10% Calcium Gluconate (10 ml given IV slowly) is the specific antidote for MgSO4 toxicity.",
    },
    {
        "question": "Kangaroo Mother Care (KMC) is primarily indicated for:",
        "options": ["Post-term babies", "Low Birth Weight (LBW) babies", "Babies with birth asphyxia", "Macrosomic infants"],
        "correct_option_id": 1,
        "explanation": "KMC is an effective method for the care of Low Birth Weight (LBW) and preterm neonates.",
    },
    {
        "question": "Under the National Immunization Schedule, the Pentavalent vaccine protects against:",
        "options": [
            "Diphtheria, Pertussis, Tetanus, Polio, Measles",
            "Diphtheria, Pertussis, Tetanus, Hepatitis B, Hib",
            "TB, Polio, Tetanus, Hepatitis B, Rubella",
            "Diphtheria, Mumps, Measles, Rubella, Hib"
        ],
        "correct_option_id": 1,
        "explanation": "Pentavalent vaccine protects against Diphtheria, Pertussis, Tetanus, Hepatitis B, and Haemophilus influenzae type b (Hib).",
    },
]

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    welcome_text = (
        "🏥 **Bihar CHO Quiz Bot में आपका स्वागत है!**\n\n"
        "कमांड्स:\n"
        "👉 /quiz - एक नया प्रश्न हल करें\n"
        "👉 /help - सहायता प्राप्त करें"
    )
    await update.message.reply_text(welcome_text, parse_mode="Markdown")

async def send_quiz(update: Update, context: ContextTypes.DEFAULT_TYPE):
    item = random.choice(QUIZ_QUESTIONS)
    await update.message.reply_poll(
        question=item["question"],
        options=item["options"],
        type=Poll.QUIZ,
        correct_option_id=item["correct_option_id"],
        explanation=item["explanation"],
        is_anonymous=False,
    )

async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    help_text = "नया सवाल पाने के लिए बस /quiz टाइप करके भेजें।"
    await update.message.reply_text(help_text)

def main():
    if not BOT_TOKEN:
        raise ValueError("BOT_TOKEN environment variable सेट नहीं है!")

    app = ApplicationBuilder().token(BOT_TOKEN).build()

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("quiz", send_quiz))
    app.add_handler(CommandHandler("help", help_command))

    print("Bihar CHO Quiz Bot चालू हो गया है...")
    app.run_polling()

if __name__ == "__main__":
    main()
