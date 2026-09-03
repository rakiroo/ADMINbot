import asyncio
import logging
import os
import random
import re
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

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
ADMIN_TIMEZONE = os.getenv("ADMIN_TIMEZONE", "Asia/Manila")
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
ICON_NOTE = "\U0001f4dd"
ICON_WARNING = "\u26a0\ufe0f"
ICON_FAILED = "\u274c"
ICON_OK = "\u2705"
ICON_WAITING = "\u23f3"
ICON_RETURNED = "\U0001f504"
ICON_STARTED = "\u26a1"
SEPARATOR = "\u2501" * 18
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
            CREATE TABLE IF NOT EXISTS users (
              telegram_user_id BIGINT PRIMARY KEY,
              display_name TEXT,
              current_username TEXT,
              first_contacted TIMESTAMPTZ NOT NULL,
              last_active TIMESTAMPTZ NOT NULL,
              created_at TIMESTAMPTZ NOT NULL,
              updated_at TIMESTAMPTZ NOT NULL
            );

            CREATE TABLE IF NOT EXISTS username_history (
              id BIGSERIAL PRIMARY KEY,
              telegram_user_id BIGINT NOT NULL REFERENCES users(telegram_user_id),
              username TEXT NOT NULL,
              first_seen_at TIMESTAMPTZ NOT NULL,
              last_seen_at TIMESTAMPTZ NOT NULL,
              UNIQUE (telegram_user_id, username)
            );

            CREATE TABLE IF NOT EXISTS conversations (
              id BIGSERIAL PRIMARY KEY,
              telegram_user_id BIGINT NOT NULL UNIQUE,
              admin_group_id BIGINT NOT NULL,
              message_thread_id BIGINT NOT NULL,
              profile_message_id BIGINT,
              control_message_id BIGINT,
              status TEXT NOT NULL DEFAULT 'OPEN',
              topic_name TEXT,
              first_contacted TIMESTAMPTZ,
              last_active TIMESTAMPTZ,
              created_at TIMESTAMPTZ NOT NULL,
              updated_at TIMESTAMPTZ NOT NULL,
              closed_at TIMESTAMPTZ,
              last_user_message_at TIMESTAMPTZ,
              last_admin_message_at TIMESTAMPTZ,
              unanswered_since TIMESTAMPTZ,
              unanswered_notified_at TIMESTAMPTZ
            );

            ALTER TABLE conversations ADD COLUMN IF NOT EXISTS profile_message_id BIGINT;
            ALTER TABLE conversations ADD COLUMN IF NOT EXISTS control_message_id BIGINT;
            ALTER TABLE conversations ADD COLUMN IF NOT EXISTS first_contacted TIMESTAMPTZ;
            ALTER TABLE conversations ADD COLUMN IF NOT EXISTS last_active TIMESTAMPTZ;

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

            CREATE TABLE IF NOT EXISTS settings (
              key TEXT PRIMARY KEY,
              value TEXT NOT NULL,
              updated_at TIMESTAMPTZ NOT NULL
            );

            INSERT INTO settings (key, value, updated_at)
            VALUES ('support_enabled', 'true', NOW())
            ON CONFLICT (key) DO NOTHING;
            """
        )


def get_setting(key: str, default: str | None = None) -> str | None:
    with connect_db() as db:
        row = db.execute("SELECT value FROM settings WHERE key = %s", (key,)).fetchone()
        return row["value"] if row else default


def set_setting(key: str, value: str) -> None:
    with connect_db() as db:
        db.execute(
            """
            INSERT INTO settings (key, value, updated_at)
            VALUES (%s, %s, %s)
            ON CONFLICT (key)
            DO UPDATE SET value = EXCLUDED.value, updated_at = EXCLUDED.updated_at
            """,
            (key, value, utcnow()),
        )


def support_enabled() -> bool:
    return get_setting("support_enabled", "true") == "true"


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


def unclaim_message(direction: str, source_chat_id: int, source_message_id: int) -> None:
    with connect_db() as db:
        db.execute(
            """
            DELETE FROM processed_messages
            WHERE direction = %s AND source_chat_id = %s AND source_message_id = %s
              AND target_message_id IS NULL
            """,
            (direction, source_chat_id, source_message_id),
        )


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


def get_user(user_id: int) -> dict | None:
    with connect_db() as db:
        return db.execute(
            "SELECT * FROM users WHERE telegram_user_id = %s",
            (user_id,),
        ).fetchone()


def get_username_history(user_id: int) -> list[dict]:
    with connect_db() as db:
        return db.execute(
            """
            SELECT username, first_seen_at, last_seen_at
            FROM username_history
            WHERE telegram_user_id = %s
            ORDER BY last_seen_at DESC, id DESC
            """,
            (user_id,),
        ).fetchall()


def remember_previous_username(user_id: int, username: str, seen_at: datetime) -> None:
    if not username:
        return
    with connect_db() as db:
        db.execute(
            """
            INSERT INTO username_history (telegram_user_id, username, first_seen_at, last_seen_at)
            VALUES (%s, %s, %s, %s)
            ON CONFLICT (telegram_user_id, username)
            DO UPDATE SET last_seen_at = EXCLUDED.last_seen_at
            """,
            (user_id, username, seen_at, seen_at),
        )


def sync_user_profile(user: types.User, active_at: datetime) -> tuple[dict, bool]:
    user_id = user.id
    display_name = user_name(user, str(user_id))
    current_username = user.username or None
    existing = get_user(user_id)
    username_changed = False

    with connect_db() as db:
        if not existing:
            db.execute(
                """
                INSERT INTO users
                  (telegram_user_id, display_name, current_username, first_contacted,
                   last_active, created_at, updated_at)
                VALUES (%s, %s, %s, %s, %s, %s, %s)
                """,
                (user_id, display_name, current_username, active_at, active_at, active_at, active_at),
            )
        else:
            old_username = existing["current_username"]
            username_changed = old_username != current_username
            if username_changed and old_username:
                remember_previous_username(user_id, old_username, active_at)

            db.execute(
                """
                UPDATE users
                SET display_name = %s, current_username = %s, last_active = %s, updated_at = %s
                WHERE telegram_user_id = %s
                """,
                (display_name, current_username, active_at, active_at, user_id),
            )

    return get_user(user_id), username_changed


def create_conversation(user_id: int, topic_id: int, name: str, active_at: datetime) -> dict:
    with connect_db() as db:
        db.execute(
            """
            INSERT INTO conversations
              (telegram_user_id, admin_group_id, message_thread_id, status, topic_name,
               first_contacted, last_active, created_at, updated_at)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
            """,
            (user_id, ADMIN_GROUP_ID, topic_id, STATUS_OPEN, name, active_at, active_at, active_at, active_at),
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


def record_user_activity(conversation_id: int, active_at: datetime, status: str | None = None) -> dict:
    assignments = [
        "last_active = %s",
        "last_user_message_at = %s",
        "unanswered_since = %s",
        "unanswered_notified_at = NULL",
        "updated_at = %s",
    ]
    values = [active_at, active_at, active_at, active_at]
    if status:
        assignments.insert(0, "status = %s")
        values.insert(0, status)
    values.append(conversation_id)

    with connect_db() as db:
        return db.execute(
            f"UPDATE conversations SET {', '.join(assignments)} WHERE id = %s RETURNING *",
            values,
        ).fetchone()


def add_note(conversation_id: int, admin_id: int | None, note: str) -> None:
    with connect_db() as db:
        db.execute(
            """
            INSERT INTO notes (conversation_id, admin_id, note, created_at)
            VALUES (%s, %s, %s, %s)
            """,
            (conversation_id, admin_id, note, utcnow()),
        )


def delete_conversation_data(conversation: dict) -> None:
    user_id = conversation["telegram_user_id"]
    topic_id = conversation["message_thread_id"]
    with connect_db() as db:
        db.execute("DELETE FROM notes WHERE conversation_id = %s", (conversation["id"],))
        db.execute(
            """
            DELETE FROM processed_messages
            WHERE (direction = 'user_to_admin' AND source_chat_id = %s)
               OR (direction = 'admin_to_user' AND target_chat_id = %s)
               OR (source_chat_id = %s AND target_chat_id = %s)
               OR (source_chat_id = %s AND target_chat_id = %s)
            """,
            (user_id, user_id, user_id, ADMIN_GROUP_ID, ADMIN_GROUP_ID, user_id),
        )
        db.execute("DELETE FROM conversations WHERE id = %s", (conversation["id"],))
        remaining = db.execute(
            "SELECT 1 FROM conversations WHERE telegram_user_id = %s LIMIT 1",
            (user_id,),
        ).fetchone()
        if not remaining:
            db.execute("DELETE FROM username_history WHERE telegram_user_id = %s", (user_id,))
            db.execute("DELETE FROM users WHERE telegram_user_id = %s", (user_id,))
        logging.info("Deleted conversation data for user %s topic %s", user_id, topic_id)


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


def trim(value: str, max_length: int) -> str:
    return value if len(value) <= max_length else value[: max_length - 1] + "..."


def format_time(value: datetime | None) -> str:
    if not value:
        return "Unknown"
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    local = value.astimezone(ZoneInfo(ADMIN_TIMEZONE))
    return local.strftime("%b %-d, %Y %-I:%M %p") if os.name != "nt" else local.strftime("%b %#d, %Y %#I:%M %p")


def status_label(status: str) -> str:
    return {
        STATUS_OPEN: "\U0001f7e2 Open",
        STATUS_IN_PROGRESS: "\U0001f7e1 In Progress",
        STATUS_WAITING_FOR_USER: "\U0001f535 Waiting for User",
        STATUS_CLOSED: "\U0001f534 Closed",
    }.get(status, status)


def username_label(username: str | None) -> str:
    return f"@{username}" if username else "None"


def profile_card(user_row: dict, conversation: dict) -> str:
    history = get_username_history(user_row["telegram_user_id"])
    lines = [
        SEPARATOR,
        f"{ICON_USER} USER PROFILE",
        "",
        f"Display name: {user_row['display_name'] or user_row['telegram_user_id']}",
        f"Username: {username_label(user_row['current_username'])}",
        f"Telegram ID: {user_row['telegram_user_id']}",
    ]

    if history:
        lines.extend(["", "Previous usernames:"])
        lines.extend(f"• {username_label(row['username'])}" for row in history)

    lines.extend(
        [
            "",
            f"First contacted: {format_time(conversation.get('first_contacted') or user_row['first_contacted'])}",
            f"Last active: {format_time(conversation.get('last_active') or user_row['last_active'])}",
            "",
            f"Conversation status: {status_label(conversation['status'])}",
            SEPARATOR,
        ]
    )
    return "\n".join(lines)


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
    match = re.match(
        r"^/(close|waiting|note|delete|d|confirm_delete|y|cancel_delete|n|support_on|on|support_off|off|support_status)(?:@\w+)?(?:\s+([\s\S]*))?$",
        text.strip(),
        re.I,
    )
    if not match:
        return None
    return f"/{match.group(1).lower()}", match.group(2) or ""


def admin_allowed(user_id: int) -> bool:
    return not ADMIN_IDS or user_id in ADMIN_IDS


def safe_error(error: Exception) -> str:
    return re.sub(r"\d+:[A-Za-z0-9_-]+", "[hidden]", str(error))


def is_forwarded(message) -> bool:
    return bool(getattr(message, "fwd_from", None))


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


async def send_clean_copy(client: TelegramClient, target, source_message, *, reply_to=None):
    if is_forwarded(source_message):
        sent = await client.forward_messages(
            target,
            source_message.id,
            from_peer=source_message.chat_id,
            silent=True,
        )
        return sent[0] if isinstance(sent, list) else sent

    if source_message.media:
        return await client.send_file(
            target,
            source_message.media,
            caption=source_message.text,
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


async def forward_message(client: TelegramClient, target, source_message, *, top_msg_id=None):
    result = await client(
        functions.messages.ForwardMessagesRequest(
            from_peer=source_message.chat_id,
            id=[source_message.id],
            to_peer=target,
            random_id=[random.getrandbits(63)],
            silent=True,
            top_msg_id=top_msg_id,
        )
    )
    for update in getattr(result, "updates", []):
        sent = getattr(update, "message", None)
        if isinstance(sent, types.Message):
            return sent
    raise RuntimeError("Telegram did not return the forwarded message.")


async def forward_to_topic(client: TelegramClient, admin_group, message, topic_id: int):
    if is_forwarded(message):
        return await forward_message(client, admin_group, message, top_msg_id=topic_id)

    return await send_clean_copy(client, admin_group, message, reply_to=topic_id)


async def send_profile_message(client: TelegramClient, conversation: dict, user_row: dict) -> int:
    admin_group = await resolve_admin_group(client)
    sent = await client.send_message(
        admin_group,
        profile_card(user_row, conversation),
        reply_to=conversation["message_thread_id"],
        link_preview=False,
    )
    await client.pin_message(admin_group, sent.id, notify=False)
    update_conversation(conversation["id"], profile_message_id=sent.id)
    logging.info(
        "Pinned profile message %s for user %s in topic %s",
        sent.id,
        conversation["telegram_user_id"],
        conversation["message_thread_id"],
    )
    return sent.id


async def send_control_panel(client: TelegramClient, conversation: dict) -> int:
    admin_group = await resolve_admin_group(client)
    sent = await client.send_message(
        admin_group,
        (
            "\u2699\ufe0f CONVERSATION CONTROLS\n\n"
            "Delete Conversation: /d\n\n"
            "Support On: /on\n"
            "Support Off: /off\n"
            "Support Status: /support_status\n\n"
            "Only authorized admins can use this control."
        ),
        reply_to=conversation["message_thread_id"],
        link_preview=False,
    )
    update_conversation(conversation["id"], control_message_id=sent.id)
    return sent.id


async def create_or_replace_profile_message(
    client: TelegramClient,
    conversation: dict,
    user_row: dict,
    *,
    unpin_previous: bool = False,
) -> dict:
    admin_group = await resolve_admin_group(client)
    fresh = get_conversation_by_user(conversation["telegram_user_id"])
    old_message_id = fresh.get("profile_message_id") if fresh else None

    if unpin_previous and old_message_id:
        try:
            await client.unpin_message(admin_group, int(old_message_id), notify=False)
        except Exception as error:
            logging.warning("Could not unpin previous profile message: %s", safe_error(error))

    await send_profile_message(client, fresh, user_row)
    return get_conversation_by_user(conversation["telegram_user_id"])


async def relay_user_to_admin(client: TelegramClient, message, conversation: dict, user: types.User) -> None:
    admin_group = await resolve_admin_group(client)
    try:
        sent = await forward_to_topic(client, admin_group, message, conversation["message_thread_id"])
    except Exception as error:
        logging.exception("Failed to relay user message")
        sent = await client.send_message(
            admin_group,
            f"{ICON_WARNING} Could not relay this user message.\n\nReason: {safe_error(error)}",
            reply_to=conversation["message_thread_id"],
        )

    mark_message_delivered("user_to_admin", user.id, message.id, ADMIN_GROUP_ID, sent.id)


async def relay_admin_to_user(client: TelegramClient, message, conversation: dict) -> None:
    admin_group = await resolve_admin_group(client)
    user_id = int(conversation["telegram_user_id"])
    try:
        sent = await send_clean_copy(client, user_id, message)
        mark_message_delivered("admin_to_user", ADMIN_GROUP_ID, message.id, user_id, sent.id)
    except Exception as error:
        logging.exception("Failed to deliver admin reply")
        await client.send_message(
            admin_group,
            f"{ICON_FAILED} DELIVERY FAILED\n\nUser ID: {user_id}\n\nReason:\n{safe_error(error)}",
            reply_to=conversation["message_thread_id"],
        )


async def ensure_conversation(client: TelegramClient, user: types.User, active_at: datetime) -> tuple[dict, bool]:
    admin_group = await resolve_admin_group(client)
    conversation = get_conversation_by_user(user.id)
    if conversation:
        if not conversation.get("profile_message_id"):
            user_row = get_user(user.id)
            conversation = await create_or_replace_profile_message(client, conversation, user_row)
        if not conversation.get("control_message_id"):
            await send_control_panel(client, conversation)
            conversation = get_conversation_by_user(user.id)

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
                unanswered_notified_at=None,
            )
            await client.send_message(
                admin_group,
                f"{ICON_RETURNED} User returned after conversation was closed.\n{ICON_RETURNED} Conversation reopened.",
                reply_to=conversation["message_thread_id"],
            )
            return get_conversation_by_user(user.id), False
        return conversation, False

    name = topic_name(user)
    thread_id = await create_topic(client, user)
    conversation = create_conversation(user.id, thread_id, name, active_at)
    logging.info("Created new conversation topic %s for user %s", thread_id, user.id)
    user_row = get_user(user.id)
    await send_profile_message(client, conversation, user_row)
    conversation = get_conversation_by_user(user.id)
    await send_control_panel(client, conversation)
    await client.send_message(admin_group, f"{ICON_STARTED} New conversation started", reply_to=thread_id)
    return get_conversation_by_user(user.id), True


def requested_conversation_id(args: str) -> int | None:
    args = args.strip()
    return int(args) if args.isdigit() else None


async def send_unauthorized(client: TelegramClient, conversation: dict) -> None:
    admin_group = await resolve_admin_group(client)
    await client.send_message(
        admin_group,
        "\u26d4 You are not authorized to perform this action.",
        reply_to=conversation["message_thread_id"],
    )


async def handle_delete_confirmation(client: TelegramClient, conversation: dict) -> None:
    admin_group = await resolve_admin_group(client)
    await client.send_message(
        admin_group,
        (
            f"{ICON_WARNING} DELETE CONVERSATION?\n\n"
            "This will permanently delete:\n\n"
            "• This forum topic\n"
            "• All messages in this conversation\n"
            "• User conversation data\n"
            "• Username history\n"
            "• Conversation mappings\n\n"
            "This action cannot be undone.\n\n"
            f"Cancel: /n {conversation['id']}\n"
            f"Yes, Delete: /y {conversation['id']}"
        ),
        reply_to=conversation["message_thread_id"],
    )


async def delete_conversation(client: TelegramClient, conversation: dict) -> None:
    admin_group = await resolve_admin_group(client)
    await client.send_message(
        admin_group,
        f"{ICON_OK} Conversation deletion started.",
        reply_to=conversation["message_thread_id"],
    )
    try:
        await client(
            functions.channels.DeleteTopicHistoryRequest(
                channel=admin_group,
                top_msg_id=conversation["message_thread_id"],
            )
        )
    except Exception as error:
        logging.warning("Could not delete full topic history: %s", safe_error(error))

    delete_conversation_data(conversation)


async def handle_admin_command(client: TelegramClient, message, conversation: dict, command: str, args: str) -> None:
    admin_group = await resolve_admin_group(client)
    admin = await message.get_sender()
    admin_id = admin.id if admin else None

    if not admin_id or not admin_allowed(admin_id):
        await send_unauthorized(client, conversation)
        return

    if command in {"/delete", "/d"}:
        await handle_delete_confirmation(client, conversation)
        return

    if command in {"/confirm_delete", "/cancel_delete", "/y", "/n"}:
        requested_id = requested_conversation_id(args)
        if requested_id != conversation["id"]:
            await client.send_message(
                admin_group,
                f"{ICON_FAILED} This delete confirmation does not belong to this topic.",
                reply_to=conversation["message_thread_id"],
            )
            return

        if command in {"/cancel_delete", "/n"}:
            await client.send_message(
                admin_group,
                f"{ICON_OK} Delete cancelled.",
                reply_to=conversation["message_thread_id"],
            )
            return

        await delete_conversation(client, conversation)
        return

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


async def handle_global_admin_command(client: TelegramClient, message, command: str) -> bool:
    admin_group = await resolve_admin_group(client)
    sender = await message.get_sender()
    if not sender:
        return True

    if not admin_allowed(sender.id):
        await client.send_message(
            admin_group,
            "\u26d4 You are not authorized to perform this action.",
            reply_to=topic_id_from_message(message),
        )
        return True

    if command in {"/support_off", "/off"}:
        set_setting("support_enabled", "false")
        await client.send_message(
            admin_group,
            f"{ICON_OK} Support relay is now OFF. Buyer messages will not be forwarded until /support_on.",
            reply_to=topic_id_from_message(message),
        )
        return True

    if command in {"/support_on", "/on"}:
        set_setting("support_enabled", "true")
        await client.send_message(
            admin_group,
            f"{ICON_OK} Support relay is now ON.",
            reply_to=topic_id_from_message(message),
        )
        return True

    if command == "/support_status":
        state = "ON" if support_enabled() else "OFF"
        await client.send_message(
            admin_group,
            f"Support relay status: {state}",
            reply_to=topic_id_from_message(message),
        )
        return True

    return False


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
        if not support_enabled():
            logging.info("Support relay is off; ignored private message %s from %s", event.message.id, sender.id)
            return
        if not claim_message("user_to_admin", sender.id, event.message.id):
            return

        active_at = utcnow()
        try:
            user_row, username_changed = sync_user_profile(sender, active_at)
            conversation, _is_new = await ensure_conversation(client, sender, active_at)
            next_status = STATUS_OPEN if conversation["status"] in (STATUS_CLOSED, STATUS_WAITING_FOR_USER) else conversation["status"]
            conversation = record_user_activity(conversation["id"], active_at, next_status)
            if username_changed:
                conversation = await create_or_replace_profile_message(
                    client,
                    conversation,
                    user_row,
                    unpin_previous=True,
                )
            await relay_user_to_admin(client, event.message, conversation, sender)
        except Exception:
            unclaim_message("user_to_admin", sender.id, event.message.id)
            raise

    @client.on(events.NewMessage(chats=ADMIN_GROUP_ID, incoming=True))
    async def on_admin_topic_message(event):
        message = event.message
        if getattr(message, "action", None):
            return
        parsed = command_parts(message.raw_text or "")
        if parsed and parsed[0] in {"/support_on", "/support_off", "/support_status"}:
            await handle_global_admin_command(client, message, parsed[0])
            return

        topic_id = topic_id_from_message(message)
        if not topic_id:
            return
        conversation = get_conversation_by_topic(topic_id)
        if not conversation:
            return

        if conversation.get("profile_message_id") and message.id == int(conversation["profile_message_id"]):
            return

        sender = await event.get_sender()
        if not sender:
            return

        if parsed:
            await handle_admin_command(client, message, conversation, parsed[0], parsed[1])
            return

        if not claim_message("admin_to_user", ADMIN_GROUP_ID, message.id):
            return
        try:
            await relay_admin_to_user(client, message, conversation)
            next_status = STATUS_IN_PROGRESS if conversation["status"] == STATUS_OPEN else conversation["status"]
            update_conversation(
                conversation["id"],
                status=next_status,
                last_admin_message_at=utcnow(),
                unanswered_since=None,
                unanswered_notified_at=None,
            )
        except Exception:
            unclaim_message("admin_to_user", ADMIN_GROUP_ID, message.id)
            raise

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
