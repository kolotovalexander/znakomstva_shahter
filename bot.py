from __future__ import annotations

import argparse
import io
import logging
import os
import sqlite3
from pathlib import Path
from typing import Optional, Any, Sequence, Dict, Set

import httpx
from dotenv import load_dotenv

from telegram import (
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    ReplyKeyboardMarkup,
    ReplyKeyboardRemove,
    Update,
)
from telegram.error import NetworkError, BadRequest, TelegramError
from telegram.ext import (
    Application,
    ApplicationBuilder,
    CallbackQueryHandler,
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

# Lower verbosity for httpx noise
logging.getLogger("httpx").setLevel(logging.WARNING)
logger = logging.getLogger(__name__)

ASK_NAME, ASK_AGE, ASK_GENDER, ASK_LOOKING_FOR, ASK_PHOTO, ASK_BIO = range(6)

db = Database()

ADMIN_IDS: Set[int] = {438466803}

MAIN_KEYBOARD = ReplyKeyboardMarkup(
    [
        ["Смотреть анкеты"],
        ["Моя анкета", "Редактировать анкету"],
        ["Поддержка"],
    ],
    resize_keyboard=True,
)

LIKE_BUTTON = "❤️"
SKIP_BUTTON = "👎"
MENU_BUTTON = "⬅️"
CANCEL_BUTTON = "Отменить"

BROWSE_KEYBOARD = ReplyKeyboardMarkup(
    [
        [LIKE_BUTTON, SKIP_BUTTON],
        [MENU_BUTTON],
    ],
    resize_keyboard=True,
)

PHOTO_SKIP_BUTTON = "Оставить текущие фото"

GENDER_OPTIONS = ["Я парень", "Я девушка"]
LOOKING_OPTIONS = ["Ищу друга", "Ищу подругу"]

CANCEL_KEYBOARD = ReplyKeyboardMarkup([[CANCEL_BUTTON]], resize_keyboard=True)

PROFILE_ACTIONS_KEYBOARD = InlineKeyboardMarkup(
    [
        [InlineKeyboardButton("Редактировать анкету", callback_data="edit_profile")],
        [InlineKeyboardButton("Заполнить заново", callback_data="reset_profile")],
        [InlineKeyboardButton("Удалить анкету", callback_data="delete_profile")],
    ]
)

GENDER_TEXT_TO_CODE = {
    "Я парень": "male",
    "Я девушка": "female",
}

CODE_TO_GENDER_TEXT = {
    "male": "Парень",
    "female": "Девушка",
}

LOOKING_TEXT_TO_CODE = {
    "Ищу друга": "male",
    "Ищу подругу": "female",
}

CODE_TO_LOOKING_TEXT = {
    "male": "друга",
    "female": "подругу",
}

CANCEL_PATTERN = filters.Regex(f"^{CANCEL_BUTTON}$")


def _keep_name_button(name: str) -> str:
    return f"Оставить имя: {name}"


def _keep_age_button(age: int) -> str:
    return f"Оставить возраст: {age}"


def _keep_gender_button(code: Optional[str]) -> Optional[str]:
    if not code:
        return None
    return f"Оставить пол: {_format_gender(code)}"


def _keep_preference_button(code: Optional[str]) -> Optional[str]:
    if not code:
        return None
    return f"Оставить поиск: {_format_preference(code)}"


def _name_keyboard(previous: Optional[str]) -> ReplyKeyboardMarkup:
    rows = []
    if previous:
        rows.append([_keep_name_button(previous)])
    rows.append([CANCEL_BUTTON])
    return ReplyKeyboardMarkup(rows, resize_keyboard=True)


def _age_keyboard(previous: Optional[int]) -> ReplyKeyboardMarkup:
    rows = []
    if previous is not None:
        rows.append([_keep_age_button(previous)])
    rows.append([CANCEL_BUTTON])
    return ReplyKeyboardMarkup(rows, resize_keyboard=True)


def _gender_keyboard(previous_code: Optional[str]) -> ReplyKeyboardMarkup:
    rows = [[option] for option in GENDER_OPTIONS]
    keep_button = _keep_gender_button(previous_code)
    if keep_button:
        rows.append([keep_button])
    rows.append([CANCEL_BUTTON])
    return ReplyKeyboardMarkup(rows, resize_keyboard=True)


def _preference_keyboard(previous_code: Optional[str]) -> ReplyKeyboardMarkup:
    rows = [[option] for option in LOOKING_OPTIONS]
    keep_button = _keep_preference_button(previous_code)
    if keep_button:
        rows.append([keep_button])
    rows.append([CANCEL_BUTTON])
    return ReplyKeyboardMarkup(rows, resize_keyboard=True)


def _photo_keyboard(has_existing: bool) -> ReplyKeyboardMarkup:
    rows = [[PHOTO_SKIP_BUTTON]]
    rows.append([CANCEL_BUTTON])
    return ReplyKeyboardMarkup(rows, resize_keyboard=True)


def _compose_preview_text(user_data: dict[str, Any], bio: Optional[str] = None) -> str:
    previous = user_data.get("profile_previous", {})
    name = user_data.get("profile_name") or previous.get("name") or "Имя не указано"
    age = user_data.get("profile_age", previous.get("age"))
    age_text = f", {age}" if age is not None else ""
    gender_text = _format_gender(user_data.get("profile_gender", previous.get("gender")))
    looking_text = _format_preference(user_data.get("profile_preference", previous.get("preference")))
    if bio is None:
        bio_text = previous.get("bio") or "Описание появится после того, как ты добавишь текст."
    else:
        bio_text = bio.strip() or "Описание отсутствует"
    return (
        f"{name}{age_text}\n"
        f"Пол: {gender_text}\n"
        f"Ищет: {looking_text}\n"
        f"{bio_text}"
    )


def _media_kind(ref: dict[str, Optional[str]]) -> str:
    media_type = ref.get("type")
    if media_type in {"photo", "video"}:
        return media_type
    return "photo"


async def _resolve_media_input(ref: dict[str, Optional[str]]) -> tuple[Optional[object], bool, str]:
    media_type = _media_kind(ref)
    file_id = ref.get("file_id")
    if file_id:
        return file_id, False, media_type
    url = ref.get("url")
    if not url:
        return None, False, media_type
    async with httpx.AsyncClient(timeout=10.0) as client:
        response = await client.get(url)
        response.raise_for_status()
        buffer = io.BytesIO(response.content)
        suffix = Path(url).suffix or (".mp4" if media_type == "video" else ".jpg")
        buffer.name = f"media{suffix}"
        buffer.seek(0)
        return buffer, True, media_type


async def _safe_send_photo(bot, chat_id: int, photo_input, caption: Optional[str] = None, reply_markup=None):
    try:
        return await bot.send_photo(
            chat_id=chat_id, photo=photo_input, caption=caption, reply_markup=reply_markup
        )
    except BadRequest as exc:
        logger.warning("Failed to send photo to %s: %s", chat_id, exc)
        return None


async def _safe_send_video(bot, chat_id: int, video_input, caption: Optional[str] = None, reply_markup=None):
    try:
        return await bot.send_video(
            chat_id=chat_id, video=video_input, caption=caption, reply_markup=reply_markup
        )
    except BadRequest as exc:
        logger.warning("Failed to send video to %s: %s", chat_id, exc)
        return None


async def _safe_send_media(
    bot,
    chat_id: int,
    media_input,
    media_type: str,
    caption: Optional[str] = None,
    reply_markup=None,
):
    if media_type == "video":
        return await _safe_send_video(bot, chat_id, media_input, caption=caption, reply_markup=reply_markup)
    return await _safe_send_photo(bot, chat_id, media_input, caption=caption, reply_markup=reply_markup)


async def _safe_send_message(bot, chat_id: int, text: str, reply_markup=None, **kwargs):
    try:
        return await bot.send_message(
            chat_id=chat_id, text=text, reply_markup=reply_markup, **kwargs
        )
    except BadRequest as exc:
        logger.warning("Failed to send message to %s: %s", chat_id, exc)
        return None


async def _fetch_telegram_profile_media(
    context: ContextTypes.DEFAULT_TYPE, user_id: int
) -> list[dict[str, Optional[str]]]:
    try:
        photos = await context.bot.get_user_profile_photos(user_id=user_id, limit=1)
    except TelegramError as exc:
        logger.debug("No profile photos for %s: %s", user_id, exc)
        return []

    if not photos.total_count:
        return []

    entry = photos.photos[0]
    largest = entry[-1]
    file_path = None
    try:
        file = await context.bot.get_file(largest.file_id)
        file_path = file.file_path
    except TelegramError as exc:
        logger.warning("Failed to fetch file path for %s: %s", user_id, exc)

    return [
        {
            "file_id": largest.file_id,
            "url": file_path,
            "type": "photo",
        }
    ]


async def _send_profile_card(
    bot,
    chat_id: int,
    photo_refs: Sequence[dict[str, Optional[str]]],
    text: str,
    header: Optional[str] = None,
    inline_markup: Optional[InlineKeyboardMarkup] = None,
) -> bool:
    caption = f"{header}\n\n{text}" if header else text
    updated = False
    if photo_refs:
        first_input, fetched, media_type = await _resolve_media_input(photo_refs[0])
        if first_input:
            message = await _safe_send_media(
                bot,
                chat_id,
                first_input,
                media_type,
                caption=caption,
                reply_markup=inline_markup,
            )
            if message:
                media_list = None
                if media_type == "photo" and message.photo:
                    media_list = message.photo
                elif media_type == "video" and message.video:
                    media_list = [message.video]
                if media_list and (fetched or not photo_refs[0].get("file_id")):
                    photo_refs[0]["file_id"] = media_list[-1].file_id
                    photo_refs[0]["type"] = media_type
                    updated = True
            for ref in photo_refs[1:3]:
                extra_input, extra_fetched, extra_type = await _resolve_media_input(ref)
                if extra_input:
                    msg = await _safe_send_media(bot, chat_id, extra_input, extra_type)
                    if msg:
                        media_list = None
                        if extra_type == "photo" and msg.photo:
                            media_list = msg.photo
                        elif extra_type == "video" and msg.video:
                            media_list = [msg.video]
                        if media_list and (extra_fetched or not ref.get("file_id")):
                            ref["file_id"] = media_list[-1].file_id
                            ref["type"] = extra_type
                            updated = True
        else:
            await _safe_send_message(bot, chat_id, caption, reply_markup=inline_markup)
    else:
        await _safe_send_message(bot, chat_id, caption, reply_markup=inline_markup)
    return updated


def _format_gender(code: str | None) -> str:
    return CODE_TO_GENDER_TEXT.get(code, "Не указан")


def _format_preference(code: str | None) -> str:
    value = CODE_TO_LOOKING_TEXT.get(code)
    if not value:
        return "Не указано"
    return value


def _store_profile_context(user_id: int, context: ContextTypes.DEFAULT_TYPE) -> Optional[sqlite3.Row]:
    profile = db.get_user(user_id)
    if not profile or not profile["profile_completed"]:
        return None

    existing_photos = db.extract_photo_refs(profile)
    context.user_data["profile_previous"] = {
        "name": profile["display_name"],
        "age": profile["age"],
        "gender": profile["gender"],
        "preference": profile["preferred_gender"],
        "photos": [dict(ref) for ref in existing_photos],
        "bio": profile["bio"],
    }
    context.user_data["profile_photos"] = [dict(ref) for ref in existing_photos]
    context.user_data.pop("profile_name", None)
    context.user_data.pop("profile_age", None)
    context.user_data.pop("profile_gender", None)
    context.user_data.pop("profile_preference", None)
    context.user_data["editing_profile"] = True
    return profile


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
            "Сейчас мы пройдём небольшую анкету: имя, возраст, пол, кого ищешь, фото (по желанию) и описание.\n"
            "Когда дойдём до фото, можно отправить несколько снимков или нажать 'Оставить текущие фото'.\n"
            "Если захочешь остановиться, нажми 'Отменить'.\n\n"
            "Для начала напиши, как тебя будут видеть другие участники.",
            reply_markup=_name_keyboard(None),
        )
        context.user_data.pop("profile_previous", None)
        context.user_data.pop("editing_profile", None)
        return ASK_NAME

    await update.message.reply_text(
        "Рад видеть тебя снова! 👋\n\n"
        "Меню ниже подскажет дальнейшие шаги:\n"
        "- Смотреть анкеты — найди новые профили участников.\n"
        "- Моя анкета — проверь, как тебя видят другие.\n"
        "- Редактировать анкету — легко обнови данные или сохрани текущие.\n"
        "Чтобы начать заново, отправь /reset.",
        reply_markup=MAIN_KEYBOARD,
    )
    return ConversationHandler.END


async def ask_name(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    text = update.message.text.strip()
    previous = context.user_data.get("profile_previous", {})
    previous_name = previous.get("name")
    keep_name = _keep_name_button(previous_name) if previous_name else None

    if keep_name and text == keep_name:
        context.user_data["profile_name"] = previous_name
    else:
        if len(text) < 2:
            await update.message.reply_text(
                "Имя должно содержать хотя бы 2 символа. Попробуй ещё раз или нажми 'Отменить'.",
                reply_markup=_name_keyboard(previous_name),
            )
            return ASK_NAME
        context.user_data["profile_name"] = text

    await update.message.reply_text(
        "Супер! Теперь напиши свой возраст (числом).",
        reply_markup=_age_keyboard(previous.get("age")),
    )
    return ASK_AGE


async def ask_age(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    text = update.message.text.strip()
    previous = context.user_data.get("profile_previous", {})
    previous_age = previous.get("age")
    keep_age = _keep_age_button(previous_age) if previous_age is not None else None

    if keep_age and text == keep_age:
        context.user_data["profile_age"] = previous_age
    else:
        if not text.isdigit():
            await update.message.reply_text(
                "Возраст нужно указать числом. Попробуй снова или нажми 'Отменить'.",
                reply_markup=_age_keyboard(previous_age),
            )
            return ASK_AGE

        age = int(text)
        if age < 16 or age > 100:
            await update.message.reply_text(
                "Возраст должен быть от 16 до 100 лет. Попробуй снова или нажми 'Отменить'.",
                reply_markup=_age_keyboard(previous_age),
            )
            return ASK_AGE

        context.user_data["profile_age"] = age

    await update.message.reply_text(
        "Выбери свой пол.",
        reply_markup=_gender_keyboard(previous.get("gender")),
    )
    return ASK_GENDER


async def ask_gender(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    choice = update.message.text.strip()
    code = GENDER_TEXT_TO_CODE.get(choice)
    previous = context.user_data.get("profile_previous", {})
    keep_gender = _keep_gender_button(previous.get("gender"))
    if keep_gender and choice == keep_gender:
        context.user_data["profile_gender"] = previous.get("gender")
    elif code:
        context.user_data["profile_gender"] = code
    else:
        await update.message.reply_text(
            "Пожалуйста, выбери один из вариантов или нажми 'Отменить'.",
            reply_markup=_gender_keyboard(previous.get("gender")),
        )
        return ASK_GENDER

    await update.message.reply_text(
        "Кого ты хочешь найти?",
        reply_markup=_preference_keyboard(previous.get("preference")),
    )
    return ASK_LOOKING_FOR


async def ask_preference(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    choice = update.message.text.strip()
    code = LOOKING_TEXT_TO_CODE.get(choice)
    previous = context.user_data.get("profile_previous", {})
    keep_pref = _keep_preference_button(previous.get("preference"))
    if keep_pref and choice == keep_pref:
        context.user_data["profile_preference"] = previous.get("preference")
    elif code:
        context.user_data["profile_preference"] = code
    else:
        await update.message.reply_text(
            "Выбери один из вариантов на клавиатуре или нажми 'Отменить'.",
            reply_markup=_preference_keyboard(previous.get("preference")),
        )
        return ASK_LOOKING_FOR

    if "profile_photos" not in context.user_data:
        context.user_data["profile_photos"] = []
    await update.message.reply_text(
        "Пришли одну или несколько фотографий. Когда будешь готов(а) продолжить без новых фото, нажми 'Оставить текущие фото'.",
        reply_markup=_photo_keyboard(bool(context.user_data.get("profile_photos"))),
    )
    return ASK_PHOTO


async def receive_photo(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    if not update.message.photo:
        await update.message.reply_text(
            "Пришли фотографию или нажми 'Оставить текущие фото'.",
            reply_markup=_photo_keyboard(bool(context.user_data.get("profile_photos"))),
        )
        return ASK_PHOTO

    photos = context.user_data.setdefault("profile_photos", [])

    if update.message.video:
        media = update.message.video
        file_id = media.file_id
        file = await context.bot.get_file(file_id)
        photos.append({"file_id": file_id, "url": file.file_path, "type": "video"})
    else:
        # Telegram sends multiple sizes; pick the largest.
        photo_sizes = update.message.photo
        file_id = photo_sizes[-1].file_id
        file = await context.bot.get_file(file_id)
        photos.append({"file_id": file_id, "url": file.file_path, "type": "photo"})

    # After any media submission, proceed automatically to bio stage.
    chat_id = update.effective_chat.id
    preview_text = _compose_preview_text(context.user_data)
    await _send_profile_card(
        context.bot,
        chat_id,
        photos,
        preview_text,
        header="Так выглядит твоя анкета сейчас:",
    )
    await context.bot.send_message(
        chat_id=chat_id,
        text="Теперь напиши пару предложений о себе или нажми 'Отменить'.",
        reply_markup=CANCEL_KEYBOARD,
    )
    return ASK_BIO


async def skip_photo(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    # If user never sent photos, ensure the key exists for downstream logic.
    photos = context.user_data.setdefault("profile_photos", [])
    if not photos:
        user = update.effective_user
        photos = await _fetch_telegram_profile_media(context, user.id)
        if photos:
            context.user_data["profile_photos"] = photos
    chat_id = update.effective_chat.id
    photos = context.user_data.get("profile_photos") or []
    preview_text = _compose_preview_text(context.user_data)
    await _send_profile_card(
        context.bot,
        chat_id,
        photos,
        preview_text,
        header="Так выглядит твоя анкета сейчас:",
    )
    await context.bot.send_message(
        chat_id=chat_id,
        text="Хорошо, теперь напиши пару предложений о себе или нажми 'Отменить'.",
        reply_markup=CANCEL_KEYBOARD,
    )
    return ASK_BIO


async def invalid_photo_text(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    await update.message.reply_text(
        "Не удалось распознать сообщение. Пришли фотографию или нажми 'Оставить текущие фото'.",
        reply_markup=_photo_keyboard(bool(context.user_data.get("profile_photos"))),
    )
    return ASK_PHOTO


async def finish_profile(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    bio = update.message.text.strip()
    if len(bio) < 5:
        await update.message.reply_text("Опиши себя чуть подробнее, минимум 5 символов.")
        return ASK_BIO

    user = update.effective_user
    previous = context.user_data.get("profile_previous", {})
    name = context.user_data.get("profile_name", previous.get("name"))
    age = context.user_data.get("profile_age", previous.get("age"))
    photos = context.user_data.get("profile_photos") or previous.get("photos", [])
    if not photos:
        photos = await _fetch_telegram_profile_media(context, user.id) or []
    gender = context.user_data.get("profile_gender", previous.get("gender"))
    preference = context.user_data.get("profile_preference", previous.get("preference"))

    db.set_profile(
        user.id,
        name,
        age,
        bio,
        gender,
        preference,
        photos,
        user.username,
    )
    context.user_data.pop("profile_name", None)
    context.user_data.pop("profile_age", None)
    context.user_data.pop("profile_photos", None)
    context.user_data.pop("profile_gender", None)
    context.user_data.pop("profile_preference", None)
    context.user_data.pop("profile_previous", None)
    context.user_data.pop("editing_profile", None)

    preview_text = _compose_preview_text(
        {
            "profile_name": name,
            "profile_age": age,
            "profile_gender": gender,
            "profile_preference": preference,
        },
        bio=bio,
    )
    chat_id = update.effective_chat.id
    updated_refs = await _send_profile_card(
        context.bot,
        chat_id,
        photos,
        preview_text,
        header="Твоя анкета обновлена:",
        inline_markup=PROFILE_ACTIONS_KEYBOARD,
    )
    if updated_refs:
        db.update_photo_refs(user.id, list(photos))

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
            reply_markup=MAIN_KEYBOARD,
        )
        return

    bio = profile["bio"] or "Описание отсутствует"
    photo_refs = db.extract_photo_refs(profile)
    preview_text = _compose_preview_text(
        {
            "profile_name": profile["display_name"],
            "profile_age": profile["age"],
            "profile_gender": profile["gender"],
            "profile_preference": profile["preferred_gender"],
        },
        bio=bio,
    )
    chat_id = update.effective_chat.id
    updated_refs = await _send_profile_card(
        context.bot,
        chat_id,
        photo_refs,
        preview_text,
        inline_markup=PROFILE_ACTIONS_KEYBOARD,
    )
    if updated_refs:
        db.update_photo_refs(user_id, list(photo_refs))
    await context.bot.send_message(
        chat_id=chat_id,
        text="Возвращаем основное меню.",
        reply_markup=MAIN_KEYBOARD,
    )


async def cancel_profile(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    if update.callback_query:
        await update.callback_query.answer()
        chat_id = update.callback_query.message.chat_id
    else:
        chat_id = update.effective_chat.id

    for key in ("profile_name", "profile_age", "profile_photos", "profile_gender", "profile_preference"):
        context.user_data.pop(key, None)
    context.user_data.pop("profile_previous", None)
    context.user_data.pop("editing_profile", None)

    await context.bot.send_message(
        chat_id=chat_id,
        text="Заполнение анкеты остановлено. Используй меню, чтобы продолжить позже.",
        reply_markup=MAIN_KEYBOARD,
    )
    return ConversationHandler.END


async def restart_profile(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    db.reset_profile(update.effective_user.id)
    context.user_data.pop("profile_photos", None)
    context.user_data.pop("profile_gender", None)
    context.user_data.pop("profile_preference", None)
    context.user_data.pop("profile_name", None)
    context.user_data.pop("profile_age", None)
    context.user_data.pop("profile_previous", None)
    context.user_data.pop("editing_profile", None)
    await update.message.reply_text(
        "Заполним всё заново! Как тебя будут видеть другие участники?",
        reply_markup=_name_keyboard(None),
    )
    return ASK_NAME

async def edit_profile_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    if not _ensure_profile(update):
        await update.message.reply_text(
            "Сначала заполни анкету с помощью /start.",
            reply_markup=MAIN_KEYBOARD,
        )
        return ConversationHandler.END

    profile = _store_profile_context(update.effective_user.id, context)
    if not profile:
        await update.message.reply_text(
            "Не удалось найти данные анкеты. Попробуй снова через /start.",
            reply_markup=MAIN_KEYBOARD,
        )
        return ConversationHandler.END

    previous = context.user_data.get("profile_previous", {})
    prompt = (
        "Редактируем анкету. Текущее имя:"
        f" {previous.get('name')}\nНапиши новое или нажми кнопку, чтобы оставить как есть."
    )
    await update.message.reply_text(prompt, reply_markup=_name_keyboard(previous.get("name")))
    return ASK_NAME


async def edit_profile_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()

    profile = _store_profile_context(query.from_user.id, context)
    if not profile:
        await context.bot.send_message(
            chat_id=query.message.chat_id,
            text="Сначала заполни анкету с помощью /start.",
            reply_markup=MAIN_KEYBOARD,
        )
        return ConversationHandler.END

    previous = context.user_data.get("profile_previous", {})
    prompt = (
        "Редактируем анкету. Текущее имя:"
        f" {previous.get('name')}\nНапиши новое или нажми кнопку, чтобы оставить как есть."
    )
    await context.bot.send_message(
        chat_id=query.message.chat_id,
        text=prompt,
        reply_markup=_name_keyboard(previous.get("name")),
    )
    return ASK_NAME


async def delete_profile_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()

    user_id = query.from_user.id
    db.delete_user(user_id)
    context.user_data.pop("profile_previous", None)
    context.user_data.pop("editing_profile", None)
    context.user_data.pop("profile_name", None)
    context.user_data.pop("profile_age", None)
    context.user_data.pop("profile_photos", None)
    context.user_data.pop("profile_gender", None)
    context.user_data.pop("profile_preference", None)

    await context.bot.send_message(
        chat_id=query.message.chat_id,
        text="Анкета удалена. Отправь /start, когда захочешь создать новую.",
        reply_markup=MAIN_KEYBOARD,
    )
    return ConversationHandler.END


async def reset_profile_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()

    db.reset_profile(query.from_user.id)
    context.user_data.pop("profile_photos", None)
    context.user_data.pop("profile_gender", None)
    context.user_data.pop("profile_preference", None)
    context.user_data.pop("profile_name", None)
    context.user_data.pop("profile_age", None)

    await context.bot.send_message(
        chat_id=query.message.chat_id,
        text="Заполним всё заново! Как тебя будут видеть другие участники?",
        reply_markup=CANCEL_KEYBOARD,
    )
    return ASK_NAME


async def browse_profiles(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not _ensure_profile(update):
        await update.message.reply_text(
            "Сначала заполни анкету, и мы продолжим.",
            reply_markup=MAIN_KEYBOARD,
        )
        return

    await send_next_profile(update, context)


async def send_next_profile(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    viewer = db.get_user(update.effective_user.id)
    candidate = db.get_next_profile(
        update.effective_user.id,
        viewer["gender"] if viewer else None,
        viewer["preferred_gender"] if viewer else None,
    )
    if not candidate:
        await update.message.reply_text(
            "Пока нет новых анкет. Загляни позже!",
            reply_markup=MAIN_KEYBOARD,
        )
        context.user_data.pop("current_candidate", None)
        return

    context.user_data["current_candidate"] = candidate["telegram_id"]
    bio = candidate["bio"] or "Описание отсутствует"
    gender_text = _format_gender(candidate["gender"])
    looking_text = _format_preference(candidate["preferred_gender"])
    text = (
        f"{candidate['display_name']}, {candidate['age']}\n"
        f"Пол: {gender_text}\n"
        f"Ищет: {looking_text}\n"
        f"{bio}"
    )
    candidate_photos = db.extract_photo_refs(candidate)
    photos_updated = False
    if candidate_photos:
        first_input, fetched, media_type = await _resolve_media_input(candidate_photos[0])
        if first_input:
            if media_type == "video":
                msg = await _safe_send_media(
                    context.bot,
                    update.effective_chat.id,
                    first_input,
                    media_type,
                    caption=text,
                    reply_markup=BROWSE_KEYBOARD,
                )
            else:
                msg = await update.message.reply_photo(first_input, caption=text, reply_markup=BROWSE_KEYBOARD)
            if msg:
                media_list = None
                if media_type == "photo" and msg.photo:
                    media_list = msg.photo
                elif media_type == "video" and msg.video:
                    media_list = [msg.video]
                if media_list and (fetched or not candidate_photos[0].get("file_id")):
                    candidate_photos[0]["file_id"] = media_list[-1].file_id
                    candidate_photos[0]["type"] = media_type
                photos_updated = True
            for ref in candidate_photos[1:3]:
                extra_input, extra_fetched, extra_type = await _resolve_media_input(ref)
                if extra_input:
                    extra_msg = await _safe_send_media(
                        context.bot, update.effective_chat.id, extra_input, extra_type
                    )
                    if extra_msg:
                        media_list = None
                        if extra_type == "photo" and extra_msg.photo:
                            media_list = extra_msg.photo
                        elif extra_type == "video" and extra_msg.video:
                            media_list = [extra_msg.video]
                        if media_list and (extra_fetched or not ref.get("file_id")):
                            ref["file_id"] = media_list[-1].file_id
                            ref["type"] = extra_type
                            photos_updated = True
        else:
            await update.message.reply_text(text, reply_markup=BROWSE_KEYBOARD)
    else:
        await update.message.reply_text(text, reply_markup=BROWSE_KEYBOARD)
    if photos_updated:
        db.update_photo_refs(candidate["telegram_id"], list(candidate_photos))


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
    profile_a_row = db.get_user(user_a)
    profile_b_row = db.get_user(user_b)
    if not profile_a_row or not profile_b_row:
        return

    profile_a_text = db.get_profile_text(user_a)
    profile_b_text = db.get_profile_text(user_b)
    if not profile_a_text or not profile_b_text:
        return

    contact_b = db.build_contact_line(profile_b_row)
    contact_a = db.build_contact_line(profile_a_row)

    message_for_a = (
        "Отлично! Надеюсь, хорошо проведёте время 🙌\n\n"
        f"Начинай общаться 👉 {contact_b}\n\n"
        f"Анкета собеседника:\n{profile_b_text}"
    )
    message_for_b = (
        "Отлично! Надеюсь, хорошо проведёте время 🙌\n\n"
        f"Начинай общаться 👉 {contact_a}\n\n"
        f"Анкета собеседника:\n{profile_a_text}"
    )

    b_photos = db.extract_photo_refs(profile_b_row)
    b_updated = False
    if b_photos:
        first_input, fetched, media_type = await _resolve_media_input(b_photos[0])
        if first_input:
            msg = await _safe_send_media(context.bot, user_a, first_input, media_type, caption=message_for_a)
            if msg:
                media_list = None
                if media_type == "photo" and msg.photo:
                    media_list = msg.photo
                elif media_type == "video" and msg.video:
                    media_list = [msg.video]
                if media_list and (fetched or not b_photos[0].get("file_id")):
                    b_photos[0]["file_id"] = media_list[-1].file_id
                    b_photos[0]["type"] = media_type
                    b_updated = True
            for ref in b_photos[1:3]:
                extra_input, extra_fetched, extra_type = await _resolve_media_input(ref)
                if extra_input:
                    extra_msg = await _safe_send_media(context.bot, user_a, extra_input, extra_type)
                    if extra_msg:
                        media_list = None
                        if extra_type == "photo" and extra_msg.photo:
                            media_list = extra_msg.photo
                        elif extra_type == "video" and extra_msg.video:
                            media_list = [extra_msg.video]
                        if media_list and (extra_fetched or not ref.get("file_id")):
                            ref["file_id"] = media_list[-1].file_id
                            ref["type"] = extra_type
                            b_updated = True
        else:
            await _safe_send_message(context.bot, user_a, message_for_a)
    else:
        await _safe_send_message(context.bot, user_a, message_for_a)

    a_photos = db.extract_photo_refs(profile_a_row)
    a_updated = False
    if a_photos:
        first_input, fetched, media_type = await _resolve_media_input(a_photos[0])
        if first_input:
            msg = await _safe_send_media(context.bot, user_b, first_input, media_type, caption=message_for_b)
            if msg:
                media_list = None
                if media_type == "photo" and msg.photo:
                    media_list = msg.photo
                elif media_type == "video" and msg.video:
                    media_list = [msg.video]
                if media_list and (fetched or not a_photos[0].get("file_id")):
                    a_photos[0]["file_id"] = media_list[-1].file_id
                    a_photos[0]["type"] = media_type
                    a_updated = True
            for ref in a_photos[1:3]:
                extra_input, extra_fetched, extra_type = await _resolve_media_input(ref)
                if extra_input:
                    extra_msg = await _safe_send_media(context.bot, user_b, extra_input, extra_type)
                    if extra_msg:
                        media_list = None
                        if extra_type == "photo" and extra_msg.photo:
                            media_list = extra_msg.photo
                        elif extra_type == "video" and extra_msg.video:
                            media_list = [extra_msg.video]
                        if media_list and (extra_fetched or not ref.get("file_id")):
                            ref["file_id"] = media_list[-1].file_id
                            ref["type"] = extra_type
                            a_updated = True
        else:
            await _safe_send_message(context.bot, user_b, message_for_b)
    else:
        await _safe_send_message(context.bot, user_b, message_for_b)

    if b_updated:
        db.update_photo_refs(user_b, list(b_photos))
    if a_updated:
        db.update_photo_refs(user_a, list(a_photos))

    # Send main keyboard so user can continue browsing easily
    await _safe_send_message(context.bot, user_a, "Продолжай знакомиться!", reply_markup=MAIN_KEYBOARD)
    await _safe_send_message(context.bot, user_b, "Продолжай знакомиться!", reply_markup=MAIN_KEYBOARD)


async def back_to_menu(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    context.user_data.pop("current_candidate", None)
    await update.message.reply_text("Возвращаемся в меню.", reply_markup=MAIN_KEYBOARD)


async def unknown(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if update.message and update.message.text:
        text = update.message.text.strip()
        if text.startswith("/allmessage"):
            await broadcast_message(update, context)
            return

    await update.message.reply_text(
        "Не понял запрос. Используй кнопки меню или команды бота.",
        reply_markup=MAIN_KEYBOARD,
    )


async def refresh_keyboards(app: Application) -> None:
    # Клавиатура обновляется при следующем взаимодействии пользователя,
    # поэтому никаких сообщений не рассылаем.
    logger.debug("Keyboard refresh skipped: silent mode")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run matchmaking Telegram bot")
    parser.add_argument(
        "--offline",
        action="store_true",
        default=os.getenv("BOT_OFFLINE_MODE", "0") in {"1", "true", "True"},
        help="Не подключаться к Telegram и использовать офлайн-режим (для разработки)",
    )
    parser.add_argument(
        "--skip-keyboard-refresh",
        action="store_true",
        help="Не отправлять сообщения с обновлением клавиатур при старте",
    )
    return parser.parse_args()


async def support_link(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text(
        "Если нужна помощь, напиши мне в поддержку:",
        reply_markup=MAIN_KEYBOARD,
    )
    await update.message.reply_text(
        "👉 https://t.me/kolotovalexander",
        disable_web_page_preview=True,
    )


async def broadcast_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if update.effective_user.id not in ADMIN_IDS:
        await update.message.reply_text("Команда недоступна.")
        return

    text = " ".join(context.args).strip()
    if not text and update.message.reply_to_message:
        text = update.message.reply_to_message.text or ""

    if not text:
        await update.message.reply_text(
            "Использование: /allmessage текст или отправьте команду в ответ на сообщение."
        )
        return

    user_ids = db.list_user_ids()
    sent = 0
    for uid in user_ids:
        result = await _safe_send_message(context.bot, uid, text)
        if result:
            sent += 1

    await update.message.reply_text(
        f"Рассылка завершена: доставлено {sent} из {len(user_ids)} пользователей.",
        reply_markup=MAIN_KEYBOARD,
    )


async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    logger.error("Uncaught exception", exc_info=context.error)


def main() -> None:
    args = parse_args()
    token = os.getenv("BOT_TOKEN")
    if not token:
        raise RuntimeError("BOT_TOKEN не задан. Установите переменную окружения или добавьте её в .env")

    if args.offline:
        logger.info("Запуск в офлайн-режиме: сетевые запросы к Telegram выполняться не будут")
        return

    builder = ApplicationBuilder().token(token)
    if not args.skip_keyboard_refresh:
        builder = builder.post_init(refresh_keyboards)
    application = builder.build()

    profile_conversation = ConversationHandler(
        entry_points=[CommandHandler("start", start)],
        states={
            ASK_NAME: [MessageHandler(CANCEL_PATTERN, cancel_profile), MessageHandler(filters.TEXT & ~filters.COMMAND, ask_name)],
            ASK_AGE: [MessageHandler(CANCEL_PATTERN, cancel_profile), MessageHandler(filters.TEXT & ~filters.COMMAND, ask_age)],
            ASK_GENDER: [MessageHandler(CANCEL_PATTERN, cancel_profile), MessageHandler(filters.TEXT & ~filters.COMMAND, ask_gender)],
            ASK_LOOKING_FOR: [MessageHandler(CANCEL_PATTERN, cancel_profile), MessageHandler(filters.TEXT & ~filters.COMMAND, ask_preference)],
            ASK_PHOTO: [
                MessageHandler(CANCEL_PATTERN, cancel_profile),
                MessageHandler(filters.PHOTO, receive_photo),
                MessageHandler(filters.Regex(f"^{PHOTO_SKIP_BUTTON}$"), skip_photo),
                MessageHandler(filters.TEXT & ~filters.COMMAND, invalid_photo_text),
            ],
            ASK_BIO: [MessageHandler(CANCEL_PATTERN, cancel_profile), MessageHandler(filters.TEXT & ~filters.COMMAND, finish_profile)],
        },
        fallbacks=[CommandHandler("start", start), CommandHandler("cancel", cancel_profile)],
        allow_reentry=True,
    )

    reprofile_conversation = ConversationHandler(
        entry_points=[
            MessageHandler(filters.Regex("^Заполнить анкету заново$"), restart_profile),
            CommandHandler("reset", restart_profile),
            MessageHandler(filters.Regex("^Редактировать анкету$"), edit_profile_message),
        ],
        states={
            ASK_NAME: [MessageHandler(CANCEL_PATTERN, cancel_profile), MessageHandler(filters.TEXT & ~filters.COMMAND, ask_name)],
            ASK_AGE: [MessageHandler(CANCEL_PATTERN, cancel_profile), MessageHandler(filters.TEXT & ~filters.COMMAND, ask_age)],
            ASK_GENDER: [MessageHandler(CANCEL_PATTERN, cancel_profile), MessageHandler(filters.TEXT & ~filters.COMMAND, ask_gender)],
            ASK_LOOKING_FOR: [MessageHandler(CANCEL_PATTERN, cancel_profile), MessageHandler(filters.TEXT & ~filters.COMMAND, ask_preference)],
            ASK_PHOTO: [
                MessageHandler(CANCEL_PATTERN, cancel_profile),
                MessageHandler(filters.PHOTO, receive_photo),
                MessageHandler(filters.Regex(f"^{PHOTO_SKIP_BUTTON}$"), skip_photo),
                MessageHandler(filters.TEXT & ~filters.COMMAND, invalid_photo_text),
            ],
            ASK_BIO: [MessageHandler(CANCEL_PATTERN, cancel_profile), MessageHandler(filters.TEXT & ~filters.COMMAND, finish_profile)],
        },
        fallbacks=[CommandHandler("start", start), CommandHandler("cancel", cancel_profile)],
        allow_reentry=True,
    )

    application.add_handler(profile_conversation)
    application.add_handler(reprofile_conversation)
    application.add_handler(CallbackQueryHandler(reset_profile_callback, pattern="^reset_profile$"))
    application.add_handler(CallbackQueryHandler(edit_profile_callback, pattern="^edit_profile$"))
    application.add_handler(CallbackQueryHandler(delete_profile_callback, pattern="^delete_profile$"))
    application.add_handler(CommandHandler("allmessage", broadcast_message))
    application.add_handler(CommandHandler("myprofile", show_profile))
    application.add_handler(MessageHandler(filters.Regex("^Моя анкета$"), show_profile))
    application.add_handler(MessageHandler(filters.Regex("^Смотреть анкеты$"), browse_profiles))
    application.add_handler(MessageHandler(filters.Regex("^Поддержка$"), support_link))
    application.add_handler(MessageHandler(filters.Regex(f"^{LIKE_BUTTON}$"), handle_like))
    application.add_handler(MessageHandler(filters.Regex(f"^{SKIP_BUTTON}$"), handle_skip))
    application.add_handler(MessageHandler(filters.Regex(f"^{MENU_BUTTON}$"), back_to_menu))
    application.add_handler(MessageHandler(filters.COMMAND, unknown))
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, unknown))
    application.add_error_handler(error_handler)

    logger.info("Bot is up and running")
    try:
        application.run_polling()
    except NetworkError as exc:
        logger.error("Не удалось подключиться к Telegram: %s", exc)
        logger.info("Завершение работы. Используйте --offline для локального запуска без сети.")



if __name__ == "__main__":
    main()
