from __future__ import annotations

import logging
import os
from dotenv import load_dotenv
from telegram import ReplyKeyboardMarkup, ReplyKeyboardRemove, Update
from telegram.ext import (
    ApplicationBuilder,
    CommandHandler,
    ContextTypes,
    ConversationHandler,
    MessageHandler,
    filters,
)

from database import Database

load_dotenv()

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)

ASK_NAME, ASK_AGE, ASK_BIO = range(3)

db = Database()

MAIN_KEYBOARD = ReplyKeyboardMarkup(
    [
        ["Смотреть анкеты"],
        ["Моя анкета", "Изменить описание"],
        ["Заполнить анкету заново"],
    ],
    resize_keyboard=True,
)

BROWSE_KEYBOARD = ReplyKeyboardMarkup(
    [
        ["❤️ Нравится", "➡️ Пропустить"],
        ["⬅️ В меню"],
    ],
    resize_keyboard=True,
)


def _ensure_profile(update: Update) -> bool:
    user_row = db.get_user(update.effective_user.id)
    return bool(user_row and user_row["profile_completed"])


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    user = update.effective_user
    db.upsert_user(user.id, user.username)

    if not _ensure_profile(update):
        await update.message.reply_text(
            "Привет! 👋 Я помогу найти знакомства на форуме.\n\n"
            "Расскажи немного о себе, чтобы другие участники увидели твою анкету.\n"
            "Сейчас мы заполним её в три шага:\n"
            "1. Напишем отображаемое имя.\n"
            "2. Укажем возраст.\n"
            "3. Добавим короткое описание о себе.\n\n"
            "Для начала напиши, как тебя будут видеть другие участники.",
            reply_markup=ReplyKeyboardRemove(),
        )
        return ASK_NAME

    await update.message.reply_text(
        "Рад видеть тебя снова! 👋\n\n"
        "Меню ниже подскажет дальнейшие шаги:\n"
        "- Смотреть анкеты — найди новые профили участников.\n"
        "- Моя анкета — проверь, как тебя видят другие.\n"
        "- Изменить описание — быстро поправь текст.\n"
        "- Заполнить анкету заново — начни всё с нуля.",
        reply_markup=MAIN_KEYBOARD,
    )
    return ConversationHandler.END


async def ask_name(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    name = update.message.text.strip()
    if len(name) < 2:
        await update.message.reply_text("Имя должно содержать хотя бы 2 символа. Попробуй ещё раз.")
        return ASK_NAME

    context.user_data["profile_name"] = name
    await update.message.reply_text("Супер! Теперь напиши свой возраст (числом).")
    return ASK_AGE


async def ask_age(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    text = update.message.text.strip()
    if not text.isdigit():
        await update.message.reply_text("Возраст нужно указать числом. Попробуй снова.")
        return ASK_AGE

    age = int(text)
    if age < 16 or age > 100:
        await update.message.reply_text("Возраст должен быть от 16 до 100 лет. Попробуй снова.")
        return ASK_AGE

    context.user_data["profile_age"] = age
    await update.message.reply_text("И последнее — напиши пару предложений о себе.")
    return ASK_BIO


async def finish_profile(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    bio = update.message.text.strip()
    if len(bio) < 5:
        await update.message.reply_text("Опиши себя чуть подробнее, минимум 5 символов.")
        return ASK_BIO

    user = update.effective_user
    name = context.user_data.get("profile_name")
    age = context.user_data.get("profile_age")

    db.set_profile(user.id, name, age, bio, user.username)
    context.user_data.pop("profile_name", None)
    context.user_data.pop("profile_age", None)

    await update.message.reply_text(
        "Готово! Твоя анкета сохранена. Можешь смотреть анкеты других участников.",
        reply_markup=MAIN_KEYBOARD,
    )
    return ConversationHandler.END


async def show_profile(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user_id = update.effective_user.id
    profile = db.get_user(user_id)
    if not profile or not profile["profile_completed"]:
        await update.message.reply_text(
            "Сначала заполни анкету, затем сможешь её посмотреть.",
            reply_markup=ReplyKeyboardRemove(),
        )
        return

    text = (
        f"Твоя анкета:\n"
        f"{profile['display_name']}, {profile['age']}\n"
        f"Описание: {profile['bio']}"
    )
    await update.message.reply_text(text, reply_markup=MAIN_KEYBOARD)


async def restart_profile(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    db.reset_profile(update.effective_user.id)
    await update.message.reply_text(
        "Заполним всё заново! Как тебя будут видеть другие участники?",
        reply_markup=ReplyKeyboardRemove(),
    )
    return ASK_NAME


async def change_bio(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    if not _ensure_profile(update):
        await update.message.reply_text(
            "Сначала заполни анкету с помощью /start.",
            reply_markup=ReplyKeyboardRemove(),
        )
        return ConversationHandler.END

    profile = db.get_user(update.effective_user.id)
    context.user_data["profile_name"] = profile["display_name"]
    context.user_data["profile_age"] = profile["age"]
    await update.message.reply_text(
        "Обновим текст анкеты. Напиши новый текст описания.",
        reply_markup=ReplyKeyboardRemove(),
    )
    return ASK_BIO


async def browse_profiles(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not _ensure_profile(update):
        await update.message.reply_text(
            "Сначала заполни анкету, и мы продолжим.",
            reply_markup=ReplyKeyboardRemove(),
        )
        return

    await send_next_profile(update, context)


async def send_next_profile(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    candidate = db.get_next_profile(update.effective_user.id)
    if not candidate:
        await update.message.reply_text(
            "Пока нет новых анкет. Загляни позже!",
            reply_markup=MAIN_KEYBOARD,
        )
        context.user_data.pop("current_candidate", None)
        return

    context.user_data["current_candidate"] = candidate["telegram_id"]
    text = (
        f"{candidate['display_name']}, {candidate['age']}\n"
        f"{candidate['bio']}"
    )
    await update.message.reply_text(text, reply_markup=BROWSE_KEYBOARD)


async def handle_like(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    candidate_id = context.user_data.get("current_candidate")
    if not candidate_id:
        await update.message.reply_text(
            "Сначала выбери анкету в разделе 'Смотреть анкеты'.",
            reply_markup=MAIN_KEYBOARD,
        )
        return

    user_id = update.effective_user.id
    db.record_reaction(user_id, candidate_id, "like")

    if db.has_mutual_like(user_id, candidate_id):
        await notify_match(user_id, candidate_id, context)

    await send_next_profile(update, context)


async def handle_skip(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    candidate_id = context.user_data.get("current_candidate")
    if not candidate_id:
        await update.message.reply_text(
            "Сначала выбери анкету в разделе 'Смотреть анкеты'.",
            reply_markup=MAIN_KEYBOARD,
        )
        return

    user_id = update.effective_user.id
    db.record_reaction(user_id, candidate_id, "skip")
    await send_next_profile(update, context)


async def notify_match(user_a: int, user_b: int, context: ContextTypes.DEFAULT_TYPE) -> None:
    profile_a = db.get_profile_text(user_a)
    profile_b = db.get_profile_text(user_b)
    if not profile_a or not profile_b:
        return

    message_for_a = (
        "У вас взаимная симпатия!\n\n"
        f"Анкета собеседника:\n{profile_b}"
    )
    message_for_b = (
        "У вас взаимная симпатия!\n\n"
        f"Анкета собеседника:\n{profile_a}"
    )

    await context.bot.send_message(chat_id=user_a, text=message_for_a)
    await context.bot.send_message(chat_id=user_b, text=message_for_b)


async def back_to_menu(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    context.user_data.pop("current_candidate", None)
    await update.message.reply_text("Возвращаемся в меню.", reply_markup=MAIN_KEYBOARD)


async def unknown(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text(
        "Не понял запрос. Используй кнопки меню или команды бота.",
        reply_markup=MAIN_KEYBOARD,
    )


def main() -> None:
    token = os.getenv("BOT_TOKEN")
    if not token:
        raise RuntimeError("BOT_TOKEN не задан. Установите переменную окружения или добавьте её в .env")

    application = ApplicationBuilder().token(token).build()

    profile_conversation = ConversationHandler(
        entry_points=[CommandHandler("start", start)],
        states={
            ASK_NAME: [MessageHandler(filters.TEXT & ~filters.COMMAND, ask_name)],
            ASK_AGE: [MessageHandler(filters.TEXT & ~filters.COMMAND, ask_age)],
            ASK_BIO: [MessageHandler(filters.TEXT & ~filters.COMMAND, finish_profile)],
        },
        fallbacks=[CommandHandler("start", start)],
        allow_reentry=True,
    )

    reprofile_conversation = ConversationHandler(
        entry_points=[
            MessageHandler(filters.Regex("^Заполнить анкету заново$"), restart_profile),
            CommandHandler("reset", restart_profile),
            MessageHandler(filters.Regex("^Изменить описание$"), change_bio),
        ],
        states={
            ASK_NAME: [MessageHandler(filters.TEXT & ~filters.COMMAND, ask_name)],
            ASK_AGE: [MessageHandler(filters.TEXT & ~filters.COMMAND, ask_age)],
            ASK_BIO: [MessageHandler(filters.TEXT & ~filters.COMMAND, finish_profile)],
        },
        fallbacks=[CommandHandler("start", start)],
        allow_reentry=True,
    )

    application.add_handler(profile_conversation)
    application.add_handler(reprofile_conversation)
    application.add_handler(CommandHandler("myprofile", show_profile))
    application.add_handler(MessageHandler(filters.Regex("^Моя анкета$"), show_profile))
    application.add_handler(MessageHandler(filters.Regex("^Смотреть анкеты$"), browse_profiles))
    application.add_handler(MessageHandler(filters.Regex("^❤️ Нравится$"), handle_like))
    application.add_handler(MessageHandler(filters.Regex("^➡️ Пропустить$"), handle_skip))
    application.add_handler(MessageHandler(filters.Regex("^⬅️ В меню$"), back_to_menu))
    application.add_handler(MessageHandler(filters.COMMAND, unknown))
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, unknown))

    logger.info("Bot is up and running")
    application.run_polling()


if __name__ == "__main__":
    main()

