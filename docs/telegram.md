# Optional Telegram notifications

Version 1.1 can send one summary-only digest after an explicitly requested live scan. Telegram is an observer at the command-line boundary: it cannot approve a role, complete a handoff, change SQLite state, or submit an application.

## What the message contains

The deterministic plain-text template contains only:

- a stable scan ID and generated timestamp;
- attempted, successful, and failed source counts;
- discovered and new-role counts;
- the human-review queue count; and
- the approved-but-not-handed-off count.

It excludes role descriptions, reviewer notes, database and report paths, credentials, chat destinations, and production identifiers. No Telegram `parse_mode` is used.

## Preview without credentials

The offline demo renders the exact template without reading environment variables or contacting Telegram:

```bash
role-monitor demo --output-dir demo-output --reset --telegram-dry-run
```

The JSON output includes `notification.status` set to `dry-run` and a `preview` field.

## Configure a live send

1. Create a bot with Telegram's [official BotFather tutorial](https://core.telegram.org/bots/tutorial). Treat the bot token like a password.
2. Start a conversation with the bot or add it to the intended group or channel. Telegram bots cannot initiate a conversation with a user who has never contacted them.
3. Obtain the intended numeric chat ID or channel username using Telegram's documented bot tools. Do not put a token in shared terminal history, screenshots, issue reports, or committed files.
4. Supply both values to the process environment or a trusted secret manager:

```bash
export TELEGRAM_BOT_TOKEN='<bot token from BotFather>'
export TELEGRAM_CHAT_ID='<numeric chat ID or @channelusername>'
```

The committed `.env.example` contains empty placeholders only. This dependency-free project does not parse `.env` files; a shell, scheduler, container platform, or secret manager must provide the variables at runtime.

5. Add the explicit live-notification flag to a scan:

```bash
role-monitor scan \
  --config config.local.json \
  --allow-network \
  --notify-telegram
```

Without `--notify-telegram`, no Telegram credential is read and no Telegram request is made. Use `--telegram-dry-run` instead to render the digest after a live scan without sending it.

## Delivery and failure behavior

- Requests go only to Telegram's fixed HTTPS `sendMessage` endpoint described by the [official Bot API](https://core.telegram.org/bots/api#sendmessage); the destination host is not configurable.
- The message is plain text and bounded to Telegram's documented 4,096-character limit.
- The default policy makes at most three attempts, uses a 10-second timeout per attempt, retries transient transport failures and HTTP 408, 429, and common 5xx responses, and caps Telegram's `retry_after` delay at 30 seconds.
- Provider errors are bounded and redacted with the token and chat ID treated as explicit secrets.
- A requested send failure returns command status 1, but the completed scan, SQLite updates, and reports remain intact.
- Delivery is observational and at-least-once. If a request times out after Telegram accepted it, a retry can produce a duplicate digest; the stable scan ID makes duplicates recognizable.

Telegram's Bot API returns a message object after a successful `sendMessage` call. The CLI reports only the provider, status, attempt count, and message ID—never the token, chat ID, request URL, or response body.
