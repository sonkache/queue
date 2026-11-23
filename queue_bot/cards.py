import asyncio
import logging
from datetime import datetime
from html import escape
from time import time
from zoneinfo import ZoneInfo

from aiogram import Bot
from aiogram.exceptions import TelegramAPIError, TelegramBadRequest, TelegramRetryAfter
from aiogram.filters.callback_data import CallbackData
from aiogram.types import InputRichMessage, ReplyParameters

from .store import Queue, QueueStore


log = logging.getLogger(__name__)
DATE_FORMAT = "%d.%m.%Y %H:%M"
STATUS_TEXT = {
    "waiting": "Запись пока закрыта",
    "open": "Запись открыта",
    "closed": "Запись завершена",
}
STATUS_ICON = {"waiting": "🔒", "open": "🟢", "closed": "🔒"}
OPENING_EMOJI = ("5393497987605361869", "➡️")
CLOSING_EMOJI = ("5393368163628905240", "⬅️")
CLOSED_EMOJI = ("5393332373666427291", "❌")


class QueueAction(CallbackData, prefix="queue"):
    action: str
    queue_id: int


def format_time(timestamp: int, timezone: ZoneInfo) -> str:
    return datetime.fromtimestamp(timestamp, timezone).strftime(DATE_FORMAT)


def rich_button(label: str, *, data: str | None = None, url: str | None = None, style: str | None = None) -> str:
    if data is not None:
        attributes = f'type="callback_data" data="{escape(data)}"'
    elif url is not None:
        attributes = f'type="url" url="{escape(url)}"'
    else:
        attributes = 'type="disabled"'
    if style is not None:
        attributes += f' style="{escape(style)}"'
    return f'<tg-button {attributes}>{escape(label)}</tg-button>'


def rich_button_row(*buttons: str) -> str:
    return f'<tg-button-row align="center">{"".join(buttons)}</tg-button-row>'


def button_row(queue_id: int, action: str, label: str, style: str | None = None) -> str:
    data = None if action == "disabled" else QueueAction(action=action, queue_id=queue_id).pack()
    return rich_button_row(rich_button(label, data=data, style=style))


def choice_rows(queue_id: int, can_cancel: bool) -> str:
    rows = button_row(queue_id, "view", "Посмотреть очередь")
    if can_cancel:
        rows += button_row(queue_id, "cancel", "Отменить запись", "danger")
    return rows


def card_emoji(emoji: tuple[str, str], custom_emoji: bool) -> str:
    emoji_id, fallback = emoji
    if custom_emoji:
        return f'<tg-emoji emoji-id="{escape(emoji_id)}">{escape(fallback)}</tg-emoji>'
    return escape(fallback)


def render_card(queue: Queue, names: list[str], now: float, timezone: ZoneInfo,
                *, custom_emoji: bool = True) -> InputRichMessage:
    status = queue.status(now)
    status_text = "Администратор завершил запись досрочно" if queue.closed_early else STATUS_TEXT[status]
    status_icon = STATUS_ICON[status] if status == "open" else card_emoji(CLOSED_EMOJI, custom_emoji)
    opening_icon = card_emoji(OPENING_EMOJI, custom_emoji)
    closing_icon = card_emoji(CLOSING_EMOJI, custom_emoji)
    blocks = [
        f"<h3>{escape(queue.title)}</h3>",
        f"<p>{opening_icon} Открытие записи: {format_time(queue.opens_at, timezone)}</p>",
        f"<p>{closing_icon} Закрытие по расписанию: {format_time(queue.closes_at, timezone)}</p>",
        f"<p>Записалось: {len(names)}</p>",
        f"<p><b>{status_icon} {status_text}</b></p>",
    ]
    if names:
        preview = "".join(f"<li>{escape(name)}</li>" for name in names[:5])
        blocks.append(f"<ol>{preview}</ol>")
        if len(names) > 5:
            blocks.append(f"<p>И ещё {len(names) - 5}</p>")

    blocks.append("<hr/>")
    if status == "open":
        blocks.append(button_row(queue.id, "join", "Занять очередь", "primary"))
    if status == "waiting":
        blocks.append(button_row(queue.id, "disabled", "Занять очередь"))
    else:
        blocks.append(choice_rows(queue.id, can_cancel=bool(names)))
    return InputRichMessage(html="\n".join(blocks), skip_entity_detection=True)


def render_confirmation(queue_id: int, text: str, can_cancel: bool) -> InputRichMessage:
    html = f"<p>{escape(text)}</p><hr/>{choice_rows(queue_id, can_cancel)}"
    return InputRichMessage(html=html, skip_entity_detection=True)


async def publish_card(bot: Bot, queue: Queue, rich_message: InputRichMessage) -> int | None:
    if queue.message_id is None:
        # Rich emoji can be stripped when Telegram edits a standalone group message.
        reply = None
        if queue.reply_to_message_id is not None:
            reply = ReplyParameters(message_id=queue.reply_to_message_id, allow_sending_without_reply=True)
        message = await bot.send_rich_message(
            chat_id=queue.chat_id, message_thread_id=queue.thread_id,
            rich_message=rich_message, reply_parameters=reply, request_timeout=8,
        )
        return message.message_id
    await bot.edit_message_text(
        chat_id=queue.chat_id, message_id=queue.message_id,
        rich_message=rich_message, request_timeout=8,
    )
    return None


async def sync_card(bot: Bot, store: QueueStore, pending: Queue, timezone: ZoneInfo):
    snapshot = store.roster(pending.id, pending.chat_id)
    if snapshot is None:
        return
    queue, names = snapshot
    now = time()
    next_at = queue.next_transition(now)
    message_id = None
    try:
        try:
            message_id = await publish_card(bot, queue, render_card(queue, names, now, timezone))
        except TelegramBadRequest as error:
            if "emoji" not in str(error).lower():
                raise
            log.info("Telegram отклонил кастомные эмодзи очереди %s", queue.id)
            message_id = await publish_card(bot, queue, render_card(queue, names, now, timezone, custom_emoji=False))
    except TelegramRetryAfter as error:
        next_at = time() + error.retry_after
    except TelegramBadRequest as error:
        description = str(error).lower()
        if "message to edit not found" in description:
            log.info("Карточка очереди %s была удалена", queue.id)
            next_at = None
        elif "message is not modified" not in description:
            log.warning("Не удалось обновить карточку %s: %s", queue.id, error)
            next_at = time() + 60
    except TelegramAPIError as error:
        log.warning("Не удалось обновить карточку %s: %s", queue.id, type(error).__name__)
        next_at = time() + 30
    store.finish_sync(queue, next_at, message_id)


async def sync_cards(bot: Bot, store: QueueStore, timezone: ZoneInfo):
    while True:
        for queue in store.due():
            await sync_card(bot, store, queue, timezone)
        await asyncio.sleep(1)
