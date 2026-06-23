"""
Telegram bot — receives photos and saves them to TELEGRAM_GALLERY_DIR.

Reacts with 👍 on success. Run alongside the FastAPI server:

    TELEGRAM_BOT_TOKEN=<token> python telegram_bot.py

Environment variables:
    TELEGRAM_BOT_TOKEN       (required) token from @BotFather
    TELEGRAM_GALLERY_DIR     directory to save images (default: ./telegram-gallery)
    TELEGRAM_ALLOWED_CHAT_ID if set, only accept messages from this chat ID
"""

from __future__ import annotations

import logging
import mimetypes
import os
from pathlib import Path

from telegram import Update
from telegram.constants import ReactionEmoji
from telegram.ext import Application, ContextTypes, MessageHandler, filters

BOT_TOKEN    = os.environ["TELEGRAM_BOT_TOKEN"]
GALLERY_DIR  = Path(os.environ.get("TELEGRAM_GALLERY_DIR",
                    str(Path(__file__).parent / "telegram-gallery")))
ALLOWED_CHAT = os.environ.get("TELEGRAM_ALLOWED_CHAT_ID")

logging.basicConfig(
    format="%(asctime)s %(levelname)s %(name)s — %(message)s",
    level=logging.INFO,
)
log = logging.getLogger(__name__)

GALLERY_DIR.mkdir(parents=True, exist_ok=True)
log.info("Gallery directory: %s", GALLERY_DIR)


def _allowed(update: Update) -> bool:
    return not ALLOWED_CHAT or str(update.effective_chat.id) == ALLOWED_CHAT


async def _save_and_react(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    file_id: str,
    file_unique_id: str,
    ext: str,
) -> None:
    dest = GALLERY_DIR / f"{file_unique_id}{ext}"
    if not dest.exists():
        tg_file = await context.bot.get_file(file_id)
        await tg_file.download_to_drive(dest)
        log.info("Saved %s", dest.name)
    await update.message.set_reaction(ReactionEmoji.THUMBS_UP)


async def handle_photo(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not _allowed(update):
        return
    photo = update.message.photo[-1]  # largest available resolution
    await _save_and_react(update, context, photo.file_id, photo.file_unique_id, ".jpg")


async def handle_document(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not _allowed(update):
        return
    doc = update.message.document
    ext = mimetypes.guess_extension(doc.mime_type or "") or ".jpg"
    await _save_and_react(update, context, doc.file_id, doc.file_unique_id, ext)


def main() -> None:
    app = Application.builder().token(BOT_TOKEN).build()
    app.add_handler(MessageHandler(filters.PHOTO, handle_photo))
    app.add_handler(MessageHandler(filters.Document.IMAGE, handle_document))
    log.info("Bot polling…")
    app.run_polling()


if __name__ == "__main__":
    main()
