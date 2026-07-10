"""
Telegram bot — receives photos and saves them to the gallery at /app/gallery.

Reacts with 👍 on success. Run alongside the FastAPI server:

    TELEGRAM_BOT_TOKEN=<token> python telegram_bot.py

The gallery directory is fixed at /app/gallery; mount your host gallery there.

Environment variables:
    TELEGRAM_BOT_TOKEN       (required) token from @BotFather
    TELEGRAM_ALLOWED_CHAT_ID if set, only accept messages from this chat ID
"""

from __future__ import annotations

import logging
import mimetypes
import os
from pathlib import Path

from telegram import Update
from telegram.constants import ReactionEmoji
from telegram.ext import Application, CommandHandler, ContextTypes, MessageHandler, filters

BOT_TOKEN    = os.environ["TELEGRAM_BOT_TOKEN"].strip()
GALLERY_DIR  = Path("/app/gallery")
ALLOWED_CHAT = (os.environ.get("TELEGRAM_ALLOWED_CHAT_ID") or "").strip() or None

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


async def cmd_id(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text(str(update.effective_chat.id))


async def handle_photo(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = update.effective_user
    chat = update.effective_chat
    log.info("Photo from %s (user %s, chat %s)", user.username or user.full_name, user.id, chat.id)
    if not _allowed(update):
        log.warning("Ignored photo from chat %s — not in allowlist", chat.id)
        return
    photo = update.message.photo[-1]  # largest available resolution
    await _save_and_react(update, context, photo.file_id, photo.file_unique_id, ".jpg")


async def handle_document(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = update.effective_user
    chat = update.effective_chat
    doc = update.message.document
    log.info("Document from %s (user %s, chat %s): %s", user.username or user.full_name, user.id, chat.id, doc.file_name)
    if not _allowed(update):
        log.warning("Ignored document from chat %s — not in allowlist", chat.id)
        return
    ext = mimetypes.guess_extension(doc.mime_type or "") or ".jpg"
    await _save_and_react(update, context, doc.file_id, doc.file_unique_id, ext)


def main() -> None:
    app = Application.builder().token(BOT_TOKEN).build()
    app.add_handler(CommandHandler("id", cmd_id))
    app.add_handler(MessageHandler(filters.PHOTO, handle_photo))
    app.add_handler(MessageHandler(filters.Document.IMAGE, handle_document))
    log.info("Bot polling…")
    app.run_polling()


if __name__ == "__main__":
    main()
