import asyncio
import logging
import os
from html import escape
from pathlib import Path
from time import time
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from aiogram import Bot, Dispatcher, F, Router
from aiogram.enums import ChatType
from aiogram.exceptions import TelegramAPIError
from aiogram.filters import Command, CommandObject
from aiogram.fsm.storage.memory import SimpleEventIsolation
from aiogram.types import (
    BotCommand,
    BotCommandScopeAllChatAdministrators,
    BufferedInputFile,
    CallbackQuery,
    EphemeralMessageParameters,
    LinkPreviewOptions,
    Message,
)

from .cards import STATUS_ICON, STATUS_TEXT, QueueAction, format_time, render_confirmation, sync_cards
from .creation import CREATE_USAGE, check_admin, router as creation_router
from .store import MAX_ACTIVE, QueueStore


router = Router()
log = logging.getLogger(__name__)
NO_PREVIEW = LinkPreviewOptions(is_disabled=True)


@router.message(Command("start", "queuehelp"))
async def help_message(message: Message, timezone: ZoneInfo):
    await message.answer(
        "Записывайтесь через кнопку «Занять очередь» под карточкой в группе. "
        "Там же можно посмотреть список или отменить свою запись.\n\n"
        "Команды администратора группы\n"
        "/queue — открыть кнопку «Создать очередь»\n"
        f"Быстрый ввод: {CREATE_USAGE}\n"
        f"Часовой пояс: {timezone.key}.\n"
        "/queues — запланированные и открытые очереди\n"
        "/closequeue НОМЕР — досрочно завершить запись\n"
        "/refreshqueue — переопубликовать карточку (ответом на неё)",
    )


@router.message(Command("queues"))
async def list_queues(message: Message, bot: Bot, store: QueueStore, timezone: ZoneInfo):
    if not await check_admin(message, bot):
        return
    active = store.active(message.chat.id)
    if not active:
        await message.answer("В группе пока нет запланированных или открытых очередей.")
        return
    now = time()
    lines = [f"Очереди группы ({len(active)} из {MAX_ACTIVE})"]
    for queue in active:
        opens = format_time(queue.opens_at, timezone)
        closes = format_time(queue.closes_at, timezone)
        status = queue.status(now)
        lines.append(f"#{queue.id} · {queue.title}\n{STATUS_ICON[status]} {STATUS_TEXT[status]} · {opens} → {closes}")
    await message.answer("\n\n".join(lines), link_preview_options=NO_PREVIEW)


@router.message(Command("closequeue"))
async def close_queue(message: Message, command: CommandObject, bot: Bot, store: QueueStore):
    if not await check_admin(message, bot):
        return
    value = (command.args or "").strip().removeprefix("#")
    if not value.isascii() or not value.isdecimal() or len(value) > 19 or int(value) >= 2**63:
        await message.answer("Укажите номер очереди из /queues, например /closequeue 3.")
        return
    if store.close(int(value), message.chat.id):
        await message.answer(f"Запись в очередь #{value} завершена.")
    else:
        await message.answer("Очередь с таким номером уже закрыта или отсутствует в этой группе.")


@router.message(Command("refreshqueue"))
async def refresh_queue(message: Message, bot: Bot, store: QueueStore):
    if not await check_admin(message, bot):
        return
    card = message.reply_to_message
    if card is None:
        await message.answer("Отправьте /refreshqueue ответом на карточку очереди.")
        return
    if not store.repost(message.chat.id, card.message_id):
        await message.answer("Карточка уже обновлена или не относится к этой очереди.")


async def show_roster(query: CallbackQuery, bot: Bot, store: QueueStore, queue_id: int, chat_id: int):
    snapshot = store.roster(queue_id, chat_id)
    if snapshot is None:
        await query.answer("Очередь не найдена.", show_alert=True)
        return
    queue, names = snapshot
    entries = "\n".join(f"{position}. {name}" for position, name in enumerate(names, 1))
    text = f"{queue.title}\n\n{entries or 'Пока никто не записался.'}"
    text_length = len(text.encode("utf-16-le")) // 2
    message_args = {"chat_id": chat_id, "message_thread_id": queue.thread_id, "request_timeout": 5}
    if text_length <= 4000:
        send = bot.send_message
        message_args.update(text=text, link_preview_options=NO_PREVIEW)
    else:
        send = bot.send_document
        message_args["document"] = BufferedInputFile(text.encode("utf-8-sig"), filename=f"queue-{queue_id}.txt")
    try:
        await send(**message_args, ephemeral_message_parameters=EphemeralMessageParameters(callback_query_id=query.id))
    except TelegramAPIError:
        if text_length <= 200:
            await query.answer(text, show_alert=True)
            return
        # Keep full rosters available to clients that cannot show personal messages in the group.
        await query.answer()
        await send(**message_args, disable_notification=True)
        return
    await query.answer()


@router.callback_query(QueueAction.filter(F.action.in_({"join", "cancel", "view"})))
async def handle_button(query: CallbackQuery, callback_data: QueueAction, bot: Bot, store: QueueStore):
    message = query.message
    if message is None or message.chat.type not in (ChatType.GROUP, ChatType.SUPERGROUP):
        await query.answer("Откройте карточку в групповом чате.", show_alert=True)
        return
    chat_id = message.chat.id
    queue_id = callback_data.queue_id
    if not 0 < queue_id < 2**63:
        await query.answer("Очередь не найдена.", show_alert=True)
        return
    if message.message_id:
        current = store.card_message_id(queue_id, chat_id)
        if current is not None and current != message.message_id:
            await query.answer("Откройте обновлённую карточку в ответе к этой.", show_alert=True)
            return
    if callback_data.action == "view":
        await show_roster(query, bot, store, queue_id, chat_id)
        return

    name = " ".join(query.from_user.full_name.split())[:130] or "Участник"
    outcome, position = store.change_entry(queue_id, chat_id, query.from_user.id, name, callback_data.action)
    if outcome in ("joined", "already"):
        prefix = "Вы записаны." if outcome == "joined" else "Вы уже в очереди."
        text = f"{prefix} Ваше место — {position}."
        try:
            await bot.send_rich_message(
                chat_id=chat_id, rich_message=render_confirmation(queue_id, text, can_cancel=True), request_timeout=5,
                message_thread_id=message.message_thread_id if isinstance(message, Message) and message.is_topic_message else None,
                ephemeral_message_parameters=EphemeralMessageParameters(callback_query_id=query.id),
            )
        except TelegramAPIError:
            await query.answer(f"{text} Остальные действия доступны под карточкой.", show_alert=True)
        else:
            await query.answer(text)
        return

    replies = {
        "cancelled": "Запись отменена.",
        "absent": "У вас нет записи в этой очереди.",
        "waiting": "Запись ещё не открылась.",
        "closed": "Запись уже завершена.",
        "missing": "Очередь не найдена.",
    }
    if outcome == "cancelled" and isinstance(message, Message) and message.ephemeral_message_id is not None:
        try:
            await bot.edit_ephemeral_message_text(
                chat_id=chat_id, receiver_user_id=query.from_user.id,
                ephemeral_message_id=message.ephemeral_message_id,
                rich_message=render_confirmation(queue_id, replies[outcome], can_cancel=False),
                request_timeout=5,
            )
        except TelegramAPIError:
            log.debug("Не удалось обновить личное подтверждение отмены.")
    await query.answer(replies[outcome], show_alert=outcome != "cancelled")


async def main():
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    token = os.environ.get("BOT_TOKEN", "").strip()
    if not token:
        raise SystemExit("Укажите BOT_TOKEN в окружении.")
    try:
        timezone = ZoneInfo(os.environ.get("BOT_TIMEZONE", "Europe/Moscow"))
    except ZoneInfoNotFoundError as error:
        raise SystemExit("Укажите часовой пояс IANA в BOT_TIMEZONE, например Europe/Moscow.") from error
    store = QueueStore(Path(os.environ.get("DB_PATH", "data/queues.sqlite3")))
    dispatcher = Dispatcher(events_isolation=SimpleEventIsolation())
    dispatcher.include_routers(creation_router, router)

    async with Bot(token) as bot:
        help_command = BotCommand(command="queuehelp", description="Как пользоваться очередями")
        await bot.set_my_commands([help_command])
        await bot.set_my_commands(
            [
                BotCommand(command="queue", description="Создать очередь"),
                BotCommand(command="queues", description="Показать активные очереди"),
                BotCommand(command="closequeue", description="Завершить запись досрочно"),
                BotCommand(command="refreshqueue", description="Переопубликовать карточку ответом"),
                help_command,
            ],
            scope=BotCommandScopeAllChatAdministrators(),
        )
        async with asyncio.TaskGroup() as tasks:
            worker = tasks.create_task(sync_cards(bot, store, timezone))
            try:
                await dispatcher.start_polling(bot, store=store, timezone=timezone, close_bot_session=False)
            finally:
                worker.cancel()


if __name__ == "__main__":
    asyncio.run(main())
