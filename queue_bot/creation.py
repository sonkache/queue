from datetime import datetime, timedelta
from html import escape
from time import time
from zoneinfo import ZoneInfo

from aiogram import Bot, F, Router
from aiogram.enums import ChatType
from aiogram.exceptions import TelegramAPIError
from aiogram.filters import Command, CommandObject, CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, InputRichMessage, Message
from aiogram.utils.deep_linking import create_start_link

from .cards import DATE_FORMAT, format_time, rich_button, rich_button_row
from .store import MAX_ACTIVE, QueueStore


router = Router()
GROUP_TYPES = (ChatType.GROUP, ChatType.SUPERGROUP)
CREATE_USAGE = "/queue Название | ДД.ММ.ГГГГ ЧЧ:ММ | ДД.ММ.ГГГГ ЧЧ:ММ"
LIMIT_TEXT = f"В группе уже {MAX_ACTIVE} запланированных или открытых очередей."
ADMIN_ONLY = "Эта команда доступна только администраторам группы."
ADMIN_CHECK_ERROR = "Не удалось проверить права администратора. Попробуйте ещё раз."
PERIOD_ERROR = "Закрытие должно быть позже открытия и текущего времени."
TITLE_ERROR = "Введите название длиной от 1 до 120 символов."
DAY_FORMAT = "%d.%m.%Y"
CANCEL_ROW = rich_button_row(rich_button("Отмена", data="draft:cancel", style="danger"))
BACK_ROW = rich_button_row(rich_button("Назад", data="draft:back"))


class Draft(StatesGroup):
    title = State()
    day = State()
    custom_day = State()
    clock = State()
    review = State()


async def is_admin(bot: Bot, chat_id: int, user_id: int) -> bool:
    admins = await bot.get_chat_administrators(chat_id, request_timeout=5)
    return any(admin.user.id == user_id for admin in admins)


async def check_admin(message: Message, bot: Bot) -> bool:
    if message.chat.type not in GROUP_TYPES:
        await message.answer("Эта команда работает в групповом чате.")
        return False
    try:
        if message.sender_chat is not None:
            allowed = message.sender_chat.id == message.chat.id
        else:
            allowed = message.from_user is not None and await is_admin(bot, message.chat.id, message.from_user.id)
    except TelegramAPIError:
        await message.answer(ADMIN_CHECK_ERROR)
        return False
    if not allowed:
        await message.answer(ADMIN_ONLY)
    return allowed


def parse_time(value: str, timezone: ZoneInfo) -> int:
    local = datetime.strptime(value, DATE_FORMAT)
    first = local.replace(tzinfo=timezone, fold=0)
    second = local.replace(tzinfo=timezone, fold=1)
    if first.utcoffset() != second.utcoffset():
        raise ValueError("Неоднозначное местное время")
    return int(first.timestamp())


def panel(heading: str, body: str = "", buttons: str = "") -> InputRichMessage:
    separator = "<hr/>" if buttons else ""
    return InputRichMessage(html=f"<h3>{escape(heading)}</h3>{body}{separator}{buttons}", skip_entity_detection=True)


async def show_step(bot: Bot, chat_id: int, state: FSMContext, stage: State,
                    content: InputRichMessage, **draft_values):
    prompt = await bot.send_rich_message(chat_id=chat_id, rich_message=content)
    await state.update_data(prompt_id=prompt.message_id, **draft_values)
    await state.set_state(stage)


def schedule_heading(opening: bool) -> str:
    return "2 из 3 · Открытие записи" if opening else "3 из 3 · Закрытие записи"


def day_prompt(opening: bool, timezone: ZoneInfo) -> InputRichMessage:
    buttons = rich_button_row(
        rich_button("Сегодня", data="draft:today", style="primary"),
        rich_button("Завтра", data="draft:tomorrow"),
    ) + rich_button_row(rich_button("Другая дата", data="draft:other"))
    return panel(schedule_heading(opening), f"<p>Выберите дату ({escape(timezone.key)}).</p>", buttons)


def clock_prompt(opening: bool, timezone: ZoneInfo, day: str) -> InputRichMessage:
    body = f"<p>Дата: <b>{escape(day)}</b> ({escape(timezone.key)})</p><p>Отправьте время в формате ЧЧ:ММ.</p>"
    return panel(schedule_heading(opening), body, BACK_ROW)


@router.message(Command("queue"))
async def create_queue(message: Message, command: CommandObject, bot: Bot, store: QueueStore, timezone: ZoneInfo):
    if not await check_admin(message, bot):
        return
    thread_id = message.message_thread_id if message.is_topic_message else None
    if not command.args:
        url = await create_start_link(bot, f"queue_{message.chat.id}_{thread_id or 0}_{message.message_id}")
        buttons = rich_button_row(rich_button("Создать очередь", url=url, style="primary"))
        await bot.send_rich_message(
            chat_id=message.chat.id, message_thread_id=thread_id,
            rich_message=panel("Управление очередями", "<p>Настройка откроется в личном чате с ботом.</p>", buttons),
        )
        return

    parts = [part.strip() for part in command.args.split("|")]
    if len(parts) != 3:
        await message.answer(f"{CREATE_USAGE}\nДаты указываются в часовом поясе {timezone.key}.")
        return
    title, opening, closing = parts
    title = " ".join(title.split())
    if not 1 <= len(title) <= 120:
        await message.answer(TITLE_ERROR)
        return
    try:
        opens_at = parse_time(opening, timezone)
        closes_at = parse_time(closing, timezone)
    except (ValueError, OverflowError, OSError):
        await message.answer(f"Укажите корректные даты в формате ДД.ММ.ГГГГ ЧЧ:ММ ({timezone.key}).")
        return
    if closes_at <= max(opens_at, time()):
        await message.answer(PERIOD_ERROR)
        return
    if store.create(message.chat.id, thread_id, title, opens_at, closes_at,
                    reply_to_message_id=message.message_id) is None:
        await message.answer(LIMIT_TEXT)


@router.message(F.chat.type == ChatType.PRIVATE, CommandStart(
    deep_link=True, magic=F.args.regexp(r"^queue_-[1-9][0-9]{0,15}_[0-9]{1,10}(?:_[1-9][0-9]{0,9})?$"),
))
async def start_draft(message: Message, command: CommandObject, bot: Bot, state: FSMContext):
    _, chat, thread, *origin = command.args.split("_")
    chat_id, thread_id = int(chat), int(thread) or None
    reply_to_message_id = int(origin[0]) if origin else None
    try:
        group = await bot.get_chat(chat_id, request_timeout=5)
        allowed = group.type in GROUP_TYPES and await is_admin(bot, chat_id, message.from_user.id)
    except TelegramAPIError:
        await message.answer(ADMIN_CHECK_ERROR)
        return
    if not allowed:
        await message.answer(ADMIN_ONLY)
        return
    body = f"<p>Группа: <b>{escape(group.title or 'Telegram')}</b></p><p>Отправьте название очереди.</p>"
    prompt = await bot.send_rich_message(
        chat_id=message.chat.id, rich_message=panel("1 из 3 · Название", body, CANCEL_ROW),
    )
    await state.set_data({
        "chat_id": chat_id,
        "thread_id": thread_id,
        "reply_to_message_id": reply_to_message_id,
        "group": group.title,
        "prompt_id": prompt.message_id,
    })
    await state.set_state(Draft.title)


@router.message(F.chat.type == ChatType.PRIVATE, Draft.title, F.text, ~F.text.startswith("/"))
async def draft_title(message: Message, bot: Bot, state: FSMContext, timezone: ZoneInfo):
    title = " ".join(message.text.split())
    if not 1 <= len(title) <= 120:
        await message.answer(TITLE_ERROR)
        return
    await show_step(bot, message.chat.id, state, Draft.day, day_prompt(True, timezone), title=title)


@router.message(F.chat.type == ChatType.PRIVATE, Draft.custom_day, F.text, ~F.text.startswith("/"))
async def draft_custom_day(message: Message, bot: Bot, state: FSMContext, timezone: ZoneInfo):
    try:
        day = datetime.strptime(message.text.strip(), DAY_FORMAT).strftime(DAY_FORMAT)
    except ValueError:
        await message.answer("Введите дату в формате ДД.ММ.ГГГГ.")
        return
    opening = "opens_at" not in await state.get_data()
    await show_step(bot, message.chat.id, state, Draft.clock, clock_prompt(opening, timezone, day), day=day)


@router.message(F.chat.type == ChatType.PRIVATE, Draft.clock, F.text, ~F.text.startswith("/"))
async def draft_clock(message: Message, bot: Bot, state: FSMContext, timezone: ZoneInfo):
    draft = await state.get_data()
    try:
        timestamp = parse_time(f"{draft['day']} {message.text.strip()}", timezone)
    except (ValueError, OverflowError, OSError):
        await message.answer("Введите корректное время в формате ЧЧ:ММ.")
        return
    chat_id = message.chat.id
    if "opens_at" not in draft:
        await show_step(bot, chat_id, state, Draft.day, day_prompt(False, timezone), opens_at=timestamp)
        return
    if timestamp <= max(draft["opens_at"], time()):
        await message.answer(PERIOD_ERROR)
        return
    body = (
        f"<p>Группа: <b>{escape(draft['group'] or 'Telegram')}</b></p>"
        f"<p>Очередь: <b>{escape(draft['title'])}</b></p>"
        f"<p>Открытие: {format_time(draft['opens_at'], timezone)}</p>"
        f"<p>Закрытие: {format_time(timestamp, timezone)}</p><p>{escape(timezone.key)}</p>"
    )
    buttons = rich_button_row(rich_button("Опубликовать", data="draft:publish", style="primary")) + CANCEL_ROW
    await show_step(bot, chat_id, state, Draft.review, panel("Проверьте очередь", body, buttons), closes_at=timestamp)


@router.callback_query(F.message.chat.type == ChatType.PRIVATE, F.data.startswith("draft:"))
async def draft_button(query: CallbackQuery, bot: Bot, state: FSMContext, store: QueueStore, timezone: ZoneInfo):
    draft = await state.get_data()
    if not isinstance(query.message, Message) or query.message.message_id != draft.get("prompt_id"):
        await query.answer("Откройте последнюю подсказку по созданию очереди.", show_alert=True)
        return
    active = await state.get_state()
    action = query.data.removeprefix("draft:")
    message = query.message
    opening = "opens_at" not in draft
    if action == "cancel" and active in (Draft.title.state, Draft.review.state):
        await state.clear()
        await query.answer()
        await message.edit_text(rich_message=panel("Создание отменено"))
    elif action == "publish" and active == Draft.review.state:
        await publish_draft(query, bot, state, store, timezone, draft)
    elif action == "back" and active in (Draft.custom_day.state, Draft.clock.state):
        await query.answer()
        await message.edit_text(rich_message=day_prompt(opening, timezone))
        await state.set_state(Draft.day)
    elif action == "other" and active == Draft.day.state:
        await query.answer()
        body = "<p>Отправьте дату в формате ДД.ММ.ГГГГ.</p>"
        await message.edit_text(rich_message=panel(schedule_heading(opening), body, BACK_ROW))
        await state.set_state(Draft.custom_day)
    elif action in ("today", "tomorrow") and active == Draft.day.state:
        day = datetime.now(timezone).date() + timedelta(days=int(action == "tomorrow"))
        formatted = day.strftime(DAY_FORMAT)
        await query.answer()
        await message.edit_text(rich_message=clock_prompt(opening, timezone, formatted))
        await state.update_data(day=formatted)
        await state.set_state(Draft.clock)
    else:
        await query.answer("Это действие больше недоступно.", show_alert=True)


async def publish_draft(query: CallbackQuery, bot: Bot, state: FSMContext, store: QueueStore,
                        timezone: ZoneInfo, draft: dict):
    try:
        allowed = await is_admin(bot, draft["chat_id"], query.from_user.id)
    except TelegramAPIError:
        await query.answer(ADMIN_CHECK_ERROR, show_alert=True)
        return
    if not allowed:
        await query.answer(ADMIN_ONLY, show_alert=True)
        return
    if draft["closes_at"] <= time():
        await query.answer("Выбранное время закрытия уже прошло.", show_alert=True)
        await show_step(bot, query.message.chat.id, state, Draft.day, day_prompt(False, timezone))
        return
    queue_id = store.create(
        draft["chat_id"], draft["thread_id"], draft["title"], draft["opens_at"], draft["closes_at"],
        reply_to_message_id=draft["reply_to_message_id"],
    )
    if queue_id is None:
        await query.answer(LIMIT_TEXT, show_alert=True)
        return
    await state.clear()
    await query.answer()
    await query.message.edit_text(rich_message=panel("Очередь создана", "<p>Карточка появится в группе.</p>"))
