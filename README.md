# Telegram Userbot Support Relay

This project uses an existing Telegram user account as the support account. It is not a BotFather bot and does not use a bot token.

Flow:

```text
User -> Support user account -> Admin supergroup forum topic -> Admin reply -> Support user account -> User
```

One Telegram user maps to one persistent forum topic.

## Telegram Requirements

- A Telegram account to act as the support account.
- A Telegram API app from <https://my.telegram.org/apps>.
- A Telegram supergroup with Forum Topics enabled.
- The support account must be in the admin supergroup.
- The support account must have permission to create and manage forum topics.
- A PostgreSQL database.

## Setup

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
copy .env.example .env
```

Edit `.env`:

```text
TG_API_ID=123456
TG_API_HASH=your_api_hash_here
ADMIN_GROUP_ID=-1001234567890
ADMIN_IDS=111111111,222222222
DATABASE_URL=postgresql://username:password@host:5432/database
```

`ADMIN_IDS` is optional. If left empty, anyone who can post inside a user topic can relay messages to users. For production, set it to your admin Telegram user IDs.

## Generate A Session String

For local-only use, you can skip this and let Telethon create a `.session` file.

For cloud/free hosting, generate a reusable session string:

```powershell
python generate_session.py
```

Put the printed value into `.env` or your host secret settings as:

```text
TELETHON_SESSION_STRING=...
```

Keep this private. Anyone with this value can access the support Telegram account.

## Run

```powershell
python support_userbot.py
```

The app creates the required PostgreSQL tables automatically on startup.

## Deploy On Render Free

Render free web services can spin down when idle, so the userbot may not be online 24/7. While the service is awake, it will keep the Telegram user session connected.

1. Put this folder in a GitHub repo.
2. In Render, create a new **Web Service** from that repo.
3. Use:

```text
Build Command: pip install -r requirements.txt
Start Command: python support_userbot.py
```

4. Add these environment variables in Render:

```text
TG_API_ID
TG_API_HASH
ADMIN_GROUP_ID
ADMIN_IDS
TELETHON_SESSION_STRING
DATABASE_URL
UNANSWERED_TIMEOUT_MINUTES
REOPEN_CLOSED_TOPICS
CLOSE_TOPIC_ON_CLOSE
```

5. Deploy.

The app exposes:

```text
/
/health
```

Opening the Render URL wakes the service if it has gone to sleep.

## Keep Render Awake With Cloudflare Cron

The `keepalive-worker/` folder contains a tiny Cloudflare Worker that calls your Render `/health` URL every 13 minutes.

After your Render service is deployed and you have its URL:

```powershell
cd C:\Users\Tanio\ADMINbot\keepalive-worker
npx wrangler deploy
```

Then set the real Render health URL:

```powershell
npx wrangler secret put TARGET_URL
```

Paste a value like:

```text
https://your-render-service.onrender.com/health
```

The cron schedule is in `keepalive-worker/wrangler.toml`:

```text
*/13 0-17,22-23 * * *
```

Cloudflare cron uses UTC. This schedule skips 18:00-21:59 UTC, which is 2:00 AM-5:59 AM in Manila. Pinging resumes at 6:00 AM Manila.

You can also open the Worker URL manually; it will ping Render once and return the ping result as JSON.

## Admin Commands

Use these inside a user's forum topic:

```text
/take
/release
/waiting
/close
/note User is waiting for payment confirmation.
```

Notes stay inside the admin topic and are never sent to the user.

## Behavior

- New user message creates a forum topic if needed.
- Existing users reuse their existing topic.
- Closed conversations reopen when the user messages again.
- User messages get sender info added only on the admin side.
- Admin replies are copied back to the user without admin-group metadata.
- Duplicate source messages are ignored through the `processed_messages` table.
- Delivery failures are reported inside the user's admin topic.
- Conversation mappings live in PostgreSQL and survive app restarts/redeploys.

## MTProto Notes

The implementation uses Telethon and MTProto methods/events:

- `events.NewMessage` for incoming private messages and admin-topic messages.
- `messages.CreateForumTopicRequest` for creating topics.
- `send_message` with topic reply targeting for topic text.
- `send_file` for media copies where Telethon supports the message media.

Telegram and Telethon do not guarantee that every possible exotic Telegram message can be copied perfectly without forwarding metadata. The code attempts to copy media without visible forwarding information and reports failures in the admin topic instead of silently losing messages.
