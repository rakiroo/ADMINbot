import asyncio
import logging
import os
import random
import re
from datetime import datetime, timedelta, timezone

from aiohttp import web
from dotenv import load_dotenv
import psycopg
from psycopg.rows import dict_row
from telethon import TelegramClient, events, functions, types
from telethon.errors import RPCError
from telethon.sessions import StringSession


load_dotenv()

DATABASE_URL = os.getenv("DATABASE_URL", "")
API_ID = int(os.getenv("TG_API_ID", "0"))
API_HASH = os.getenv("TG_API_HASH", "")
SESSION_NAME = os.getenv("SESSION_NAME", "support_session")
SESSION_STRING = os.getenv("TELETHON_SESSION_STRING", "")
ADMIN_GROUP_ID = int(os.getenv("ADMIN_GROUP_ID", "0"))
PORT = int(os.getenv("PORT", "8080"))
UNANSWERED_TIMEOUT_MINUTES = int(os.getenv("UNANSWERED_TIMEOUT_MINUTES", "10"))
REOPEN_CLOSED_TOPICS = os.getenv("REOPEN_CLOSED_TOPICS", "true").lower() == "true"
CLOSE_TOPIC_ON_CLOSE = os.getenv("CLOSE_TOPIC_ON_CLOSE", "false").lower() == "true"
ADMIN_IDS = {
    int(value.strip())
    for value in os.getenv("ADMIN_IDS", "").split(",")
    if value.strip().isdigit()
}

STATUS_OPEN = "OPEN"
STATUS_IN_PROGRESS = "IN_PROGRESS"
STATUS_WAITING_FOR_USER = "WAITING_FOR_USER"
STATUS_CLOSED = "CLOSED"

ICON_USER = "\U0001f464"
ICON_BADGE = "\U0001f4db"
ICON_ID = "\U0001f194"
ICON_CALENDAR = "\U0001f4c5"
ICON_ASSIGNED = "\U0001f468\u200d\U0001f4bc"
ICON_NOTE = "\U0001f4dd"
ICON_WARNING = "\u26a0\ufe0f"
ICON_FAILED = "\u274c"
ICON_OK = "\u2705"
ICON_WAITING = "\u23f3"
ICON_RETURNED = "\U0001f504"
SEPARATOR = "\u2501" * 14
ADMIN_GROUP_ENTITY = None


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def connect_db():
    if not DATABASE_URL:
        raise RuntimeError("Set DATABASE_URL before starting.")
    return psycopg.connect(DATABASE_URL, row_factory=dict_row)


def init_db() -> None:
    with connect_db() as db:
        db.execute(
            """
            CREATE TABLE IF NOT EXISTS conversations (
              id BIGSERIAL PRIMARY KEY,
              telegram_user_id BIGINT NOT NULL UNIQUE,
              admin_group_id BIGINT NOT NULL,
              message_thread_id BIGINT NOT NULL,
              status TEXT NOT NULL DEFAULT 'OPEN',
              assigned_admin_id BIGINT,
              topic_name TEXT,
              created_at TIMESTAMPTZ NOT NULL,
              updated_at TIMESTAMPTZ NOT NULL,
              closed_at TIMESTAMPTZ,
              last_user_message_at TIMESTAMPTZ,
              last_admin_message_at TIMESTAMPTZ,
              unanswered_since TIMESTAMPTZ,
              unanswered_notified_at TIMESTAMPTZ
            );

            CREATE UNIQUE INDEX IF NOT EXISTS idx_conversations_topic
              ON conversations (admin_group_id, message_thread_id);

            CREATE TABLE IF NOT EXISTS processed_messages (
              id BIGSERIAL PRIMARY KEY,
              direction TEXT NOT NULL,
              source_chat_id BIGINT NOT NULL,
              source_message_id BIGINT NOT NULL,
              target_chat_id BIGINT,
              target_message_id BIGINT,
              created_at TIMESTAMPTZ NOT NULL,
              UNIQUE(direction, source_chat_id, source_message_id)
            );

            CREATE TABLE IF NOT EXISTS notes (
              id BIGSERIAL PRIMARY KEY,
              conversation_id BIGINT NOT NULL REFERENCES conversations(id),
              admin_id BIGINT,
              note TEXT NOT NULL,
              created_at TIMESTAMPTZ NOT NULL
            );
            """
        )


def claim_message(direction: str, source_chat_id: int, source_message_id: int) -> bool:
    with connect_db() as db:
        row = db.execute(
            """
            INSERT INTO processed_messages
              (direction, source_chat_id, source_message_id, created_at)
            VALUES (%s, %s, %s, %s)
            ON CONFLICT (direction, source_chat_id, source_message_id) DO NOTHING
            RETURNING id
            """,
            (direction, source_chat_id, source_message_id, utcnow()),
        ).fetchone()
        return row is not None


def mark_message_delivered(
    direction: str,
    source_chat_id: int,
    source_message_id: int,
    target_chat_id: int,
    target_message_id: int,
) -> None:
    with connect_db() as db:
        db.execute(
            """
            UPDATE processed_messages
            SET target_chat_id = %s, target_message_id = %s
            WHERE direction = %s AND source_chat_id = %s AND source_message_id = %s
            """,
            (target_chat_id, target_message_id, direction, source_chat_id, source_message_id),
        )


def get_conversation_by_user(user_id: int) -> dict | None:
    with connect_db() as db:
        return db.execute(
            "SELECT * FROM conversations WHERE telegram_user_id = %s",
            (user_id,),
        ).fetchone()


def get_conversation_by_topic(topic_id: int) -> dict | None:
    with connect_db() as db:
        return db.execute(
            "SELECT * FROM conversations WHERE admin_group_id = %s AND message_thread_id = %s",
            (ADMIN_GROUP_ID, topic_id),
        ).fetchone()


def create_conversation(user_id: int, topic_id: int, name: str) -> dict:
    timestamp = utcnow()
    with connect_db() as db:
        db.execute(
            """
            INSERT INTO conversations
              (telegram_user_id, admin_group_id, message_thread_id, status, topic_name, created_at, updated_at)
            VALUES (%s, %s, %s, %s, %s, %s, %s)
            """,
            (user_id, ADMIN_GROUP_ID, topic_id, STATUS_OPEN, name, timestamp, timestamp),
        )
    return get_conversation_by_user(user_id)


def update_conversation(conversation_id: int, **fields) -> None:
    if not fields:
        return
    fields["updated_at"] = utcnow()
    columns = ", ".join(f"{key} = %s" for key in fields)
    values = list(fields.values()) + [conversation_id]
    with connect_db() as db:
        db.execute(f"UPDATE conversations SET {columns} WHERE id = %s", values)


def add_note(conversation_id: int, admin_id: int | None, note: str) -> None:
    with connect_db() as db:
        db.execute(
            """
            INSERT INTO notes (conversation_id, admin_id, note, created_at)
            VALUES (%s, %s, %s, %s)
            """,
            (conversation_id, admin_id, note, utcnow()),
        )


def unanswered_conversations(cutoff: datetime) -> list[dict]:
    with connect_db() as db:
        return db.execute(
            """
            SELECT * FROM conversations
            WHERE status IN ('OPEN', 'IN_PROGRESS')
              AND unanswered_since IS NOT NULL
              AND unanswered_since <= %s
              AND unanswered_notified_at IS NULL
            LIMIT 50
            """,
            (cutoff,),
        ).fetchall()


def user_name(user: types.User, fallback: str) -> str:
    name = " ".join(part for part in [user.first_name, user.last_name] if part).strip()
    return name or fallback


def topic_name(user: types.User) -> str:
    readable = user_name(user, str(user.id))
    handle = f"@{user.username}" if user.username else str(user.id)
    return trim(f"{ICON_USER} {readable} - {handle}", 120)


def user_footer(user: types.User) -> str:
    lines = [
        SEPARATOR,
        f"{ICON_USER} {user_name(user, str(user.id))}",
    ]
    if user.username:
        lines.append(f"{ICON_BADGE} @{user.username}")
    lines.append(f"{ICON_ID} {user.id}")
    return "\n".join(lines)


def topic_intro(user: types.User) -> str:
    date_format = "%B %#d, %Y" if os.name == "nt" else "%B %-d, %Y"
    lines = [f"{ICON_USER} {user_name(user, str(user.id))}"]
    if user.username:
        lines.append(f"{ICON_BADGE} @{user.username}")
    lines.extend(
        [
            f"{ICON_ID} {user.id}",
            f"{ICON_CALENDAR} First contact: {datetime.now().strftime(date_format)}",
        ]
    )
    return "\n".join(lines)


def trim(value: str, max_length: int) -> str:
    return value if len(value) <= max_length else value[: max_length - 1] + "..."


def append_footer(text: str | None, footer: str) -> str:
    return f"{text}\n\n{footer}" if text else footer


def topic_id_from_message(message) -> int | None:
    reply = getattr(message, "reply_to", None)
    if not reply:
        return None
    return getattr(reply, "reply_to_top_id", None) or getattr(reply, "reply_to_msg_id", None)


def topic_id_from_updates(updates) -> int:
    for update in getattr(updates, "updates", []):
        message = getattr(update, "message", None)
        if isinstance(message, types.MessageService) and isinstance(message.action, types.MessageActionTopicCreate):
            return message.id
    raise RuntimeError("Telegram did not return the created forum topic id.")


def command_parts(text: str) -> tuple[str, str] | None:
    match = re.match(r"^/(close|take|release|waiting|note)(?:@\w+)?(?:\s+([\s\S]*))?$", text.strip(), re.I)
    if not match:
        return None
    return f"/{match.group(1).lower()}", match.group(2) or ""


def admin_allowed(user_id: int) -> bool:
    return not ADMIN_IDS or user_id in ADMIN_IDS


def safe_error(error: Exception) -> str:
    return re.sub(r"\d+:[A-Za-z0-9_-]+", "[hidden]", str(error))


async def resolve_admin_group(client: TelegramClient):
    global ADMIN_GROUP_ENTITY
    if ADMIN_GROUP_ENTITY is not None:
        return ADMIN_GROUP_ENTITY

    try:
        ADMIN_GROUP_ENTITY = await client.get_entity(ADMIN_GROUP_ID)
    except ValueError:
        logging.info("Admin group entity was not cached. Loading dialogs once.")
        await client.get_dialogs(limit=None)
        ADMIN_GROUP_ENTITY = await client.get_entity(ADMIN_GROUP_ID)

    group_id = getattr(ADMIN_GROUP_ENTITY, "id", None)
    group_title = getattr(ADMIN_GROUP_ENTITY, "title", None)
    logging.info("Resolved admin group: %s (%s)", group_title, group_id)
    return ADMIN_GROUP_ENTITY


async def create_topic(client: TelegramClient, user: types.User) -> int:
    admin_group = await resolve_admin_group(client)
    result = await client(
        functions.channels.CreateForumTopicRequest(
            channel=admin_group,
            title=topic_name(user),
            random_id=random.getrandbits(63),
        )
    )
    return topic_id_from_updates(result)


async def send_copy(client: TelegramClient, target, source_message, *, reply_to=None, caption=None):
    if source_message.media:
        return await client.send_file(
            target,
            source_message.media,
            caption=caption if caption is not None else source_message.text,
            reply_to=reply_to,
            formatting_entities=source_message.entities,
        )
    return await client.send_message(
        target,
        source_message.text or "",
        reply_to=reply_to,
        formatting_entities=source_message.entities,
        link_preview=False,
    )


async def relay_user_to_admin(client: TelegramClient, message, conversation: dict, user: types.User) -> None:
    admin_group = await resolve_admin_group(client)
    footer = user_footer(user)
    try:
        if message.media:
            sent = await send_copy(
                client,
                admin_group,
                message,
                reply_to=conversation["message_thread_id"],
                caption=append_footer(message.text, footer),
            )
        else:
            sent = await client.send_message(
                admin_group,
                append_footer(message.text, footer),
                reply_to=conversation["message_thread_id"],
                formatting_entities=message.entities,
                link_preview=False,
            )
    except Exception as error:
        logging.exception("Failed to copy user message exactly")
        sent = await client.send_message(
            admin_group,
            f"{ICON_WARNING} Could not relay this user message exactly.\n\nReason: {safe_error(error)}\n\n{footer}",
            reply_to=conversation["message_thread_id"],
        )

    mark_message_delivered("user_to_admin", user.id, message.id, ADMIN_GROUP_ID, sent.id)


async def relay_admin_to_user(client: TelegramClient, message, conversation: dict) -> None:
    admin_group = await resolve_admin_group(client)
    user_id = int(conversation["telegram_user_id"])
    try:
        sent = await send_copy(client, user_id, message)
        mark_message_delivered("admin_to_user", ADMIN_GROUP_ID, message.id, user_id, sent.id)
    except Exception as error:
        logging.exception("Failed to deliver admin reply")
        await client.send_message(
            admin_group,
            f"{ICON_FAILED} DELIVERY FAILED\n\nUser ID: {user_id}\n\nReason:\n{safe_error(error)}",
            reply_to=conversation["message_thread_id"],
        )


async def ensure_conversation(client: TelegramClient, user: types.User) -> dict:
    admin_group = await resolve_admin_group(client)
    conversation = get_conversation_by_user(user.id)
    if conversation:
        if conversation["status"] == STATUS_CLOSED and REOPEN_CLOSED_TOPICS:
            if CLOSE_TOPIC_ON_CLOSE:
                await client(
                    functions.channels.EditForumTopicRequest(
                        channel=admin_group,
                        topic_id=conversation["message_thread_id"],
                        closed=False,
                    )
                )
            update_conversation(
                conversation["id"],
                status=STATUS_OPEN,
                closed_at=None,
                unanswered_since=utcnow(),
                unanswered_notified_at=None,
            )
            await client.send_message(
                admin_group,
                f"{ICON_RETURNED} User returned. Conversation reopened.",
                reply_to=conversation["message_thread_id"],
            )
            return get_conversation_by_user(user.id)
        return conversation

    name = topic_name(user)
    thread_id = await create_topic(client, user)
    conversation = create_conversation(user.id, thread_id, name)
    await client.send_message(admin_group, topic_intro(user), reply_to=thread_id)
    return conversation


async def handle_admin_command(client: TelegramClient, message, conversation: dict, command: str, args: str) -> None:
    admin_group = await resolve_admin_group(client)
    admin = await message.get_sender()
    admin_id = admin.id if admin else None

    if command == "/close":
        update_conversation(conversation["id"], status=STATUS_CLOSED, closed_at=utcnow())
        await client.send_message(admin_group, f"{ICON_OK} Conversation closed.", reply_to=conversation["message_thread_id"])
        if CLOSE_TOPIC_ON_CLOSE:
            await client(
                functions.channels.EditForumTopicRequest(
                    channel=admin_group,
                    topic_id=conversation["message_thread_id"],
                    closed=True,
                )
            )
        return

    if command == "/take":
        label = f"@{admin.username}" if getattr(admin, "username", None) else user_name(admin, str(admin_id))
        update_conversation(conversation["id"], status=STATUS_IN_PROGRESS, assigned_admin_id=admin_id)
        await client.send_message(admin_group, f"{ICON_ASSIGNED} Assigned to: {label}", reply_to=conversation["message_thread_id"])
        return

    if command == "/release":
        update_conversation(conversation["id"], assigned_admin_id=None)
        await client.send_message(admin_group, f"{ICON_OK} Assignment released.", reply_to=conversation["message_thread_id"])
        return

    if command == "/waiting":
        update_conversation(conversation["id"], status=STATUS_WAITING_FOR_USER)
        await client.send_message(admin_group, f"{ICON_WAITING} Status: waiting for user.", reply_to=conversation["message_thread_id"])
        return

    if command == "/note":
        note = args.strip()
        if not note:
            return
        add_note(conversation["id"], admin_id, note)
        await client.send_message(admin_group, f"{ICON_NOTE} INTERNAL NOTE\n\n{note}", reply_to=conversation["message_thread_id"])


async def unanswered_loop(client: TelegramClient) -> None:
    admin_group = await resolve_admin_group(client)
    while True:
        await asyncio.sleep(60)
        cutoff = utcnow() - timedelta(minutes=UNANSWERED_TIMEOUT_MINUTES)
        for conversation in unanswered_conversations(cutoff):
            await client.send_message(
                admin_group,
                f"{ICON_WARNING} UNANSWERED\nWaiting for admin response: {UNANSWERED_TIMEOUT_MINUTES} minutes",
                reply_to=conversation["message_thread_id"],
            )
            update_conversation(conversation["id"], unanswered_notified_at=utcnow())


async def start_health_server() -> web.AppRunner:
    app = web.Application()
    app.router.add_get("/", lambda _: web.Response(text="Telegram userbot support relay is running."))
    app.router.add_get("/health", lambda _: web.Response(text="ok"))

    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "0.0.0.0", PORT)
    await site.start()
    logging.info("Health server listening on port %s", PORT)
    return runner


async def main() -> None:
    if not API_ID or not API_HASH or not ADMIN_GROUP_ID or not DATABASE_URL:
        raise RuntimeError("Set TG_API_ID, TG_API_HASH, ADMIN_GROUP_ID, and DATABASE_URL before starting.")

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    init_db()
    await start_health_server()

    session = StringSession(SESSION_STRING) if SESSION_STRING else SESSION_NAME
    client = TelegramClient(session, API_ID, API_HASH)

    @client.on(events.NewMessage(incoming=True))
    async def on_private_message(event):
        if not event.is_private:
            return
        sender = await event.get_sender()
        if not isinstance(sender, types.User) or sender.bot:
            return
        if not claim_message("user_to_admin", sender.id, event.message.id):
            return

        conversation = await ensure_conversation(client, sender)
        await relay_user_to_admin(client, event.message, conversation, sender)
        update_conversation(
            conversation["id"],
            status=STATUS_OPEN if conversation["status"] in (STATUS_CLOSED, STATUS_WAITING_FOR_USER) else conversation["status"],
            last_user_message_at=utcnow(),
            unanswered_since=utcnow(),
            unanswered_notified_at=None,
        )

    @client.on(events.NewMessage(chats=ADMIN_GROUP_ID, incoming=True))
    async def on_admin_topic_message(event):
        message = event.message
        if getattr(message, "action", None):
            return
        topic_id = topic_id_from_message(message)
        if not topic_id:
            return
        conversation = get_conversation_by_topic(topic_id)
        if not conversation:
            return

        sender = await event.get_sender()
        if not sender or not admin_allowed(sender.id):
            return

        parsed = command_parts(message.raw_text or "")
        if parsed:
            await handle_admin_command(client, message, conversation, parsed[0], parsed[1])
            return

        if not claim_message("admin_to_user", ADMIN_GROUP_ID, message.id):
            return
        await relay_admin_to_user(client, message, conversation)
        update_conversation(
            conversation["id"],
            status=STATUS_IN_PROGRESS if conversation["status"] == STATUS_OPEN else conversation["status"],
            last_admin_message_at=utcnow(),
            unanswered_since=None,
            unanswered_notified_at=None,
        )

    await client.start()
    me = await client.get_me()
    logging.info("Support userbot running as %s (%s)", getattr(me, "username", None), me.id)
    await resolve_admin_group(client)
    asyncio.create_task(unanswered_loop(client))
    await client.run_until_disconnected()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except (KeyboardInterrupt, RPCError) as error:
        if isinstance(error, RPCError):
            logging.exception("Telegram RPC error")
