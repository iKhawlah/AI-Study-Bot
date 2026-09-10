import json
import logging
import os
import re
import string
import tempfile
import time
import uuid
from datetime import datetime, timedelta

from dateparser.search import search_dates
from dotenv import load_dotenv
from google import genai
from google.genai import types
from google.genai.errors import ServerError
from pypdf import PdfReader
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    ConversationHandler,
    MessageHandler,
    filters,
)

load_dotenv()

TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN")
GEMINI_MODEL = "gemini-3.6-flash"

MAX_PDF_CHARS = 30000
TELEGRAM_MESSAGE_LIMIT = 4096
GEMINI_MAX_RETRIES = 3
GEMINI_RETRY_DELAY_SECONDS = 2

QUIZ_SOURCE, QUIZ_AWAITING_TOPIC, QUIZ_TYPE, QUIZ_DIFFICULTY, QUIZ_COUNT = range(5)
REMINDER_TASK, REMINDER_TIME_CHOICE, REMINDER_CUSTOM_TIME = range(3)

REMINDERS_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "reminders.json")

GREETING_WORDS = {
    "الو", "هلو", "هلا", "هاي", "مرحبا", "مرحباً", "اهلا", "أهلا",
    "اهلين", "أهلين", "سلام", "السلام عليكم", "صباح الخير", "مساء الخير",
}

PM_WORDS = {"pm", "p.m.", "مساء", "مساءً", "م", "ظهرا", "ظهراً", "عصرا", "عصراً"}
AM_WORDS = {"am", "a.m.", "صباحا", "صباحاً", "ص"}
MERIDIEM_PATTERN = re.compile(
    r"(\d{1,2})(?::(\d{2}))?\s*(" + "|".join(re.escape(w) for w in PM_WORDS | AM_WORDS) + r")",
    re.IGNORECASE,
)
ARABIC_RELATIVE_TIME_MAP = {
    "دقيقتين": "2 دقيقة", "ثلاث دقايق": "3 دقيقة", "ثلاث دقائق": "3 دقيقة",
    "اربع دقايق": "4 دقيقة", "أربع دقائق": "4 دقيقة",
    "خمس دقايق": "5 دقيقة", "خمس دقائق": "5 دقيقة",
    "عشر دقايق": "10 دقيقة", "عشر دقائق": "10 دقيقة",
    "ساعتين": "2 ساعة", "ثلاث ساعات": "3 ساعة", "اربع ساعات": "4 ساعة",
    "خمس ساعات": "5 ساعة", "يومين": "2 يوم",
}
ARABIC_RELATIVE_TIME_PHRASES = sorted(ARABIC_RELATIVE_TIME_MAP, key=len, reverse=True)

logging.basicConfig(format="%(asctime)s - %(name)s - %(levelname)s - %(message)s", level=logging.INFO)
logger = logging.getLogger(__name__)

client = genai.Client()

SYSTEM_INSTRUCTION = """
أنت مساعد دراسي ذكي لطلاب الجامعات يتحدث اللغة العربية بطلاقة وبأسلوب طبيعي. 
مهمتك هي مساعدة الطالب في فهم المواد، تلخيص السلايدات، إعداد الاختبارات القصيرة، وشرح المفاهيم بطريقة مبسطة.
- استخدم لغة عربية فصحى مبسطة أو لهجة بيضاء.
- حافظ على المصطلحات العلمية بالإنجليزية بين قوسين إذا لزم.
- ممنوع منعاً باتاً استخدام علامات التنسيق مثل النجمات (**) أو (###).
- استخدم فقط الرموز التعبيرية (مثل 🔹، 💡) والأسطر الفارغة لترتيب النص.
"""

def ask_gemini(prompt: str, system_prompt: str = SYSTEM_INSTRUCTION) -> str:
    print("--> [معلومة] جاري إرسال الطلب للذكاء الاصطناعي...")
    for attempt in range(1, GEMINI_MAX_RETRIES + 1):
        try:
            response = client.models.generate_content(
                model=GEMINI_MODEL,
                contents=prompt,
                config=types.GenerateContentConfig(system_instruction=system_prompt)
            )
            if response.text:
                return response.text
        except ServerError as error:
            if attempt < GEMINI_MAX_RETRIES: time.sleep(GEMINI_RETRY_DELAY_SECONDS * attempt)
        except Exception:
            return "عذراً، واجهت مشكلة في الاتصال بالذكاء الاصطناعي. جرب بعد قليل."
    return "الخدمة عليها ضغط حالياً، يرجى المحاولة بعد قليل."

def split_message(text: str, limit: int = TELEGRAM_MESSAGE_LIMIT) -> list[str]:
    if len(text) <= limit: return [text]
    return [text[i:i + limit] for i in range(0, len(text), limit)]

async def reply_long(message, text: str) -> None:
    for chunk in split_message(text):
        await message.reply_text(chunk)

async def send_long_message(update: Update, text: str) -> None:
    await reply_long(update.message, text)

def build_main_menu_keyboard() -> InlineKeyboardMarkup:
    buttons = [
        [InlineKeyboardButton("📝 تلخيص ملف/سلايدات", callback_data="menu_summarize_request")],
        [InlineKeyboardButton("🧠 توليد كويز (اختبار)", callback_data="menu_start_quiz")],
        [InlineKeyboardButton("💡 شرح مفهوم أو سؤال", callback_data="menu_explain_request")],
        [InlineKeyboardButton("⏰ إعداد تذكير للمذاكرة", callback_data="menu_start_reminder")],
    ]
    return InlineKeyboardMarkup(buttons)

def build_pdf_action_keyboard() -> InlineKeyboardMarkup:
    buttons = [
        [InlineKeyboardButton("📝 لخص لي هذا الملف", callback_data="pdf_action_summary")],
        [InlineKeyboardButton("🧠 سوّ لي كويز منه", callback_data="pdf_action_quiz")],
        [InlineKeyboardButton("💡 اشرح لي جزئية منه", callback_data="pdf_action_explain")],
    ]
    return InlineKeyboardMarkup(buttons)

def load_reminders() -> list[dict]:
    if not os.path.exists(REMINDERS_FILE): return []
    try:
        with open(REMINDERS_FILE, "r", encoding="utf-8") as f: return json.load(f)
    except: return []

def save_reminders(reminders: list[dict]) -> None:
    try:
        with open(REMINDERS_FILE, "w", encoding="utf-8") as f: json.dump(reminders, f, ensure_ascii=False)
    except: pass

def add_reminder(chat_id: int, task: str, run_at: datetime) -> str:
    reminders = load_reminders()
    reminder_id = uuid.uuid4().hex
    reminders.append({"id": reminder_id, "chat_id": chat_id, "task": task, "run_at": run_at.isoformat()})
    save_reminders(reminders)
    return reminder_id

def remove_reminder(reminder_id: str) -> None:
    save_reminders([r for r in load_reminders() if r["id"] != reminder_id])

def normalize_arabic_relative_time(text: str) -> str:
    for phrase in ARABIC_RELATIVE_TIME_PHRASES:
        text = text.replace(phrase, ARABIC_RELATIVE_TIME_MAP[phrase])
    text = re.sub(r"(?<!\d)(?<!\d )\bدقيقة\b", "1 دقيقة", text)
    text = re.sub(r"(?<!\d)(?<!\d )\bساعة\b", "1 ساعة", text)
    return text

def parse_natural_time(text: str) -> datetime | None:
    text = normalize_arabic_relative_time(text)
    results = search_dates(text, languages=["en", "ar"], settings={"PREFER_DATES_FROM": "future", "RELATIVE_BASE": datetime.now()})
    if not results: return None
    _, run_at = results[0]
    meridiem_match = MERIDIEM_PATTERN.search(text)
    if meridiem_match:
        hour, minute, word = int(meridiem_match.group(1)), int(meridiem_match.group(2) or 0), meridiem_match.group(3).lower()
        if word in PM_WORDS and hour != 12: hour += 12
        elif word in AM_WORDS and hour == 12: hour = 0
        run_at = run_at.replace(hour=hour, minute=minute, second=0, microsecond=0)
    if run_at <= datetime.now(): run_at += timedelta(days=1)
    return run_at

def build_reminder_time_keyboard() -> InlineKeyboardMarkup:
    buttons = [
        [
            InlineKeyboardButton("⏱ بعد ساعة", callback_data="rem_time_1h"),
            InlineKeyboardButton("⏱ بعد 3 ساعات", callback_data="rem_time_3h"),
        ],
        [
            InlineKeyboardButton("🌅 بكرة 9 الصباح", callback_data="rem_time_tom9am"),
            InlineKeyboardButton("🌆 بكرة 6 المساء", callback_data="rem_time_tom6pm"),
        ],
        [InlineKeyboardButton("✍️ وقت مخصص (كتابة)", callback_data="rem_time_custom")],
    ]
    return InlineKeyboardMarkup(buttons)

async def reminder_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    message = query.message if query else update.message
    if query: await query.answer()
    await message.reply_text("وش حاب أذكرك فيه؟ ⏰\n(مثلاً: راجع سلايدات شابتر 3)")
    return REMINDER_TASK

async def reminder_task_received(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data["reminder_task"] = update.message.text.strip()
    await update.message.reply_text("متى أذكرك؟ 👇", reply_markup=build_reminder_time_keyboard())
    return REMINDER_TIME_CHOICE

async def reminder_time_choice(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    if query.data == "rem_time_custom":
        await query.edit_message_text("اكتب لي الوقت اللي يناسبك ✍️\nأمثلة: 'بعد ساعتين', 'بكرة الساعة 7 المساء', 'بعد 5 دقايق'")
        return REMINDER_CUSTOM_TIME

    now = datetime.now()
    presets = {
        "rem_time_1h": now + timedelta(hours=1), "rem_time_3h": now + timedelta(hours=3),
        "rem_time_tom9am": (now + timedelta(days=1)).replace(hour=9, minute=0, second=0, microsecond=0),
        "rem_time_tom6pm": (now + timedelta(days=1)).replace(hour=18, minute=0, second=0, microsecond=0),
    }
    run_at = presets[query.data]
    await finalize_reminder(query.message, context, run_at)
    return ConversationHandler.END

async def reminder_custom_time_received(update: Update, context: ContextTypes.DEFAULT_TYPE):
    run_at = parse_natural_time(update.message.text)
    if run_at is None:
        await update.message.reply_text("عذراً، ما فهمت الوقت زين. جرب تكتب: 'بعد ساعتين' أو 'غدا 9 صباحا'.")
        return REMINDER_CUSTOM_TIME
    await finalize_reminder(update.message, context, run_at)
    return ConversationHandler.END

async def finalize_reminder(message, context: ContextTypes.DEFAULT_TYPE, run_at: datetime) -> None:
    task = context.user_data.pop("reminder_task", "مذاكرة")
    reminder_id = add_reminder(message.chat.id, task, run_at)
    if context.job_queue:
        delay = max((run_at - datetime.now()).total_seconds(), 1)
        context.job_queue.run_once(send_reminder, delay, chat_id=message.chat.id, data={"task": task, "id": reminder_id}, name=f"rem_{reminder_id}")
    await message.reply_text(f"✅ تم الجدولة! راح أذكرك بـ:\n\"{task}\"\nالوقت: {run_at.strftime('%Y-%m-%d %H:%M')}")

async def send_reminder(context: ContextTypes.DEFAULT_TYPE) -> None:
    job = context.job
    await context.bot.send_message(chat_id=job.chat_id, text=f"⏰ تذكير للمذاكرة:\n\n{job.data['task']}")
    remove_reminder(job.data["id"])

def schedule_pending_reminders(app: Application) -> None:
    if not app.job_queue: return
    now = datetime.now()
    for rem in load_reminders():
        run_at = datetime.fromisoformat(rem["run_at"])
        delay = max((run_at - now).total_seconds(), 1)
        app.job_queue.run_once(send_reminder, delay, chat_id=rem["chat_id"], data={"task": rem["task"], "id": rem["id"]}, name=f"rem_{rem['id']}")

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = "أهلاً بك! أنا مساعدك الدراسي الذكي 🎓\nكيف أقدر أساعدك اليوم؟\n• أرسل سلايدات لترخيصها أو اختبارك فيها.\n• اسألني أي سؤال دراسي وسأشرحه.\n• استخدم الأزرار للوصول السريع 👇"
    await update.message.reply_text(text, reply_markup=build_main_menu_keyboard())

async def restart_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.callback_query.answer()
    await start(update, context)

async def handle_menu_requests(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    if query.data == "menu_summarize_request": await query.message.reply_text("ممتاز! أرسل لي ملف الـ PDF (السلايدات) 📄.")
    elif query.data == "menu_explain_request": await query.message.reply_text("اكتب لي المفهوم أو السؤال اللي مو فاهمه وراح أشرحه لك 💡.")

def extract_text_from_pdf(file_path: str) -> str:
    return "\n".join([page.extract_text() or "" for page in PdfReader(file_path).pages]).strip()

async def handle_pdf(update: Update, context: ContextTypes.DEFAULT_TYPE):
    document = update.message.document
    await update.message.reply_text("وصلني الملف 📥، جاري قراءته...")
    
    tmp_path = f"{uuid.uuid4().hex}.pdf"
    
    try:
        telegram_file = await document.get_file()
        await telegram_file.download_to_drive(custom_path=tmp_path)
        
        chapter_text = extract_text_from_pdf(tmp_path)
        
        if not chapter_text:
            await update.message.reply_text("المعذرة، ما قدرت أستخرج أي نص من الملف (تأكد أنه مو مجرد صور).")
            return
            
        context.user_data["last_pdf_text"] = chapter_text[:MAX_PDF_CHARS]
        await update.message.reply_text("قرأت الملف! وش حاب أسوي لك الحين؟ 👇", reply_markup=build_pdf_action_keyboard())
        
    except Exception as e:
        print(f"--> [خطأ في قراءة الملف]: {e}")
        await update.message.reply_text("حدث خطأ أثناء معالجة الملف، حاول مرة ثانية.")
        
    finally:
        if os.path.exists(tmp_path):
            os.remove(tmp_path)

async def handle_pdf_action(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    chapter_text = context.user_data.get("last_pdf_text", "")
    if not chapter_text: return await query.message.reply_text("عذراً، الملف مو محفوظ عندي حالياً. أرسله مرة ثانية.")
    
    if query.data == "pdf_action_summary":
        await query.edit_message_text("جاري إعداد التلخيص... ⏳")
        prompt = (
            "أنت أستاذ جامعي ممتاز. قم بتلخيص هذه السلايدات بشكل مريح جداً للعين ومنظم للطالب.\n"
            "شروط التنسيق (مهم جداً):\n"
            "1. ممنوع منعاً باتاً استخدام علامات التنسيق مثل النجمات (**) أو المربعات (###).\n"
            "2. استخدم الرموز التعبيرية (مثل 📌، 💡، 🔹) بدلاً من العلامات السابقة لتوضيح العناوين والنقاط.\n"
            "3. اترك سطر فارغ (مسافة) بين كل نقطة وأخرى لتجنب تداخل الكلام.\n"
            "4. ركز على أهم النقاط، التعاريف، وما يهم في الاختبار.\n\n"
            f"السلايدات:\n{chapter_text}"
        )
        summary = ask_gemini(prompt)
        await reply_long(query.message, summary)
    elif query.data == "pdf_action_explain":
        await query.edit_message_text("اكتب لي أي جزئية مو فاهمها من هذا الملف، وراح أشرحها لك! 💡")
    elif query.data == "pdf_action_quiz":
        context.user_data["quiz_source_text"] = chapter_text
        keyboard = InlineKeyboardMarkup([
            [InlineKeyboardButton("خيارات متعددة", callback_data="quiz_type_mcq")],
            [InlineKeyboardButton("صح وخطأ", callback_data="quiz_type_tf")],
            [InlineKeyboardButton("أسئلة قصيرة", callback_data="quiz_type_short")]
        ])
        await query.edit_message_text("حلو! اختر نوع الأسئلة اللي تبيه:", reply_markup=keyboard)

async def quiz_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    message = query.message if query else update.message
    if query: await query.answer()
    if context.user_data.get("last_pdf_text"):
        buttons = [[InlineKeyboardButton("📄 من آخر ملف", callback_data="quiz_src_pdf")], [InlineKeyboardButton("✍️ بكتب لك الموضوع", callback_data="quiz_src_topic")]]
        await message.reply_text("من وين حاب أسوي الكويز؟", reply_markup=InlineKeyboardMarkup(buttons))
        return QUIZ_SOURCE
    await message.reply_text("اكتب الموضوع اللي تبي أسوي لك كويز عليه ✍️")
    return QUIZ_AWAITING_TOPIC

async def quiz_source_choice(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    if query.data == "quiz_src_pdf":
        context.user_data["quiz_source_text"] = context.user_data["last_pdf_text"]
        keyboard = InlineKeyboardMarkup([[InlineKeyboardButton("خيارات", callback_data="quiz_type_mcq")], [InlineKeyboardButton("صح وخطأ", callback_data="quiz_type_tf")]])
        await query.edit_message_text("اختر نوع الكويز:", reply_markup=keyboard)
        return QUIZ_TYPE
    await query.edit_message_text("اكتب الموضوع:")
    return QUIZ_AWAITING_TOPIC

async def quiz_topic_received(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data["quiz_source_text"] = update.message.text
    keyboard = InlineKeyboardMarkup([[InlineKeyboardButton("خيارات", callback_data="quiz_type_mcq")], [InlineKeyboardButton("صح وخطأ", callback_data="quiz_type_tf")]])
    await update.message.reply_text("اختر نوع الكويز:", reply_markup=keyboard)
    return QUIZ_TYPE

async def quiz_type_choice(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    context.user_data["quiz_type"] = query.data.removeprefix("quiz_type_")
    keyboard = InlineKeyboardMarkup([[InlineKeyboardButton("سهل", callback_data="quiz_diff_easy"), InlineKeyboardButton("متوسط", callback_data="quiz_diff_medium"), InlineKeyboardButton("صعب", callback_data="quiz_diff_hard")]])
    await query.edit_message_text("مستوى الصعوبة؟", reply_markup=keyboard)
    return QUIZ_DIFFICULTY

async def quiz_difficulty_choice(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    context.user_data["quiz_difficulty"] = query.data.removeprefix("quiz_diff_")
    keyboard = InlineKeyboardMarkup([[InlineKeyboardButton("5 أسئلة", callback_data="quiz_count_5"), InlineKeyboardButton("10 أسئلة", callback_data="quiz_count_10")]])
    await query.edit_message_text("كم سؤال؟", reply_markup=keyboard)
    return QUIZ_COUNT

async def quiz_count_choice(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    count, text, q_type, diff = int(query.data.removeprefix("quiz_count_")), context.user_data.get("quiz_source_text", ""), context.user_data.get("quiz_type", "mcq"), context.user_data.get("quiz_difficulty", "medium")
    await query.edit_message_text(f"جاري التجهيز ({count} أسئلة)... ⏳")
    prompt = f"أنشئ اختبار من {count} أسئلة بصعوبة ({diff}) بنوع {q_type} باللغة العربية.\nاكتب الأسئلة، ثم ===الردود===، ثم الأجوبة بشرح.\nالمادة:\n{text}"
    raw = ask_gemini(prompt)
    if "===الردود===" in raw: q_text, a_text = raw.split("===الردود===", 1)
    else: q_text, a_text = raw, "لم أتمكن من فصل الأجوبة."
    context.user_data["quiz_pending_answers"] = a_text.strip()
    await reply_long(query.message, q_text.strip())
    await query.message.reply_text("تبي تشوف الأجوبة الصحيحة؟", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔓 إظهار الأجوبة", callback_data="quiz_show_answers")]]))
    return ConversationHandler.END

async def quiz_show_answers(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    ans = context.user_data.get("quiz_pending_answers")
    if ans:
        await query.edit_message_reply_markup(reply_markup=None)
        await reply_long(query.message, ans)

async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = update.message.text.strip().lower()
    if not msg or msg in GREETING_WORDS or len(msg) <= 2:
        return await update.message.reply_text("يا هلا بك! وش أقدر أساعدك فيه؟", reply_markup=build_main_menu_keyboard())
    wait_msg = await update.message.reply_text("جاري التفكير... 💭")
    prompt = f"هذا سؤال من الطالب: {msg}\nاستعن بهذا الملف: {context.user_data['last_pdf_text'][:5000]}" if context.user_data.get("last_pdf_text") else msg
    await send_long_message(update, ask_gemini(prompt))
    await wait_msg.delete()

def main():
    app = Application.builder().token(TELEGRAM_BOT_TOKEN).build()

    quiz_conv = ConversationHandler(
        entry_points=[CommandHandler("quiz", quiz_start), CallbackQueryHandler(quiz_start, pattern="^menu_start_quiz$"), CallbackQueryHandler(quiz_type_choice, pattern="^quiz_type_")],
        states={
            QUIZ_SOURCE: [CallbackQueryHandler(quiz_source_choice, pattern="^quiz_src_")],
            QUIZ_AWAITING_TOPIC: [MessageHandler(filters.TEXT & ~filters.COMMAND, quiz_topic_received)],
            QUIZ_TYPE: [CallbackQueryHandler(quiz_type_choice, pattern="^quiz_type_")],
            QUIZ_DIFFICULTY: [CallbackQueryHandler(quiz_difficulty_choice, pattern="^quiz_diff_")],
            QUIZ_COUNT: [CallbackQueryHandler(quiz_count_choice, pattern="^quiz_count_")],
        }, fallbacks=[CommandHandler("cancel", lambda u, c: ConversationHandler.END)]
    )

    remind_conv = ConversationHandler(
        entry_points=[CommandHandler("remind", reminder_start), CallbackQueryHandler(reminder_start, pattern="^menu_start_reminder$")],
        states={
            REMINDER_TASK: [MessageHandler(filters.TEXT & ~filters.COMMAND, reminder_task_received)],
            REMINDER_TIME_CHOICE: [CallbackQueryHandler(reminder_time_choice, pattern="^rem_time_")],
            REMINDER_CUSTOM_TIME: [MessageHandler(filters.TEXT & ~filters.COMMAND, reminder_custom_time_received)],
        }, fallbacks=[CommandHandler("cancel", lambda u, c: ConversationHandler.END)]
    )

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CallbackQueryHandler(restart_menu, pattern="^menu_restart$"))
    app.add_handler(CallbackQueryHandler(handle_menu_requests, pattern="^menu_.*_request$"))
    app.add_handler(CallbackQueryHandler(handle_pdf_action, pattern="^pdf_action_"))
    
    app.add_handler(quiz_conv)
    app.add_handler(remind_conv)
    app.add_handler(CallbackQueryHandler(quiz_show_answers, pattern="^quiz_show_answers$"))
    
    app.add_handler(MessageHandler(filters.Document.PDF, handle_pdf))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))

    schedule_pending_reminders(app)
    print("--> [معلومة] البوت يشتغل الآن! وميزة التذكيرات وقراءة الملفات جاهزة!")
    app.run_polling()

if __name__ == "__main__":
    main()