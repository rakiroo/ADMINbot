import re

with open("support_userbot.py", "r", encoding="utf-8") as f:
    content = f.read()

content = re.sub(
    r"r\"\^/(close|waiting|note|delete|confirm_delete|cancel_delete|support_on|support_off|support_status)(?:@\\w+)?(?:\\s+([\\s\\S]*))?\$\"",
    r'r"^/(close|waiting|note|delete|d|confirm_delete|y|cancel_delete|n|support_on|on|support_off|off|support_status)(?:@\\w+)?(?:\\s+([\\s\\S]*))?$"',
    content
)


content = content.replace(
    "Cancel: /cancel_delete {conversation['id']}\\n",
    "Cancel: /n {conversation['id']}\\n"
)
content = content.replace(
    "Yes, Delete: /confirm_delete {conversation['id']}",
    "Yes, Delete: /y {conversation['id']}"
)

content = content.replace(
    'if command == "/delete":',
    'if command in {"/delete", "/d"}:'
)

content = content.replace(
    'if command in {"/confirm_delete", "/cancel_delete"}:',
    'if command in {"/confirm_delete", "/cancel_delete", "/y", "/n"}:'
)

content = content.replace(
    'if command == "/cancel_delete":',
    'if command in {"/cancel_delete", "/n"}:'
)

content = content.replace(
    'if command == "/support_off":',
    'if command in {"/support_off", "/off"}:'
)

content = content.replace(
    'if command == "/support_on":',
    'if command in {"/support_on", "/on"}:'
)

content = content.replace(
    "Support bot is now turned OFF. Incoming messages will be ignored.",
    "Support bot is now turned OFF (/off). Incoming messages will be ignored."
)

content = content.replace(
    "Support bot is now turned ON. Forwarding incoming messages.",
    "Support bot is now turned ON (/on). Forwarding incoming messages."
)

content = content.replace(
    "Support On: /support_on",
    "Support On: /on"
)

content = content.replace(
    "Support Off: /support_off",
    "Support Off: /off"
)

content = content.replace(
    "Delete Conversation: /delete\\n\\n",
    "Delete Conversation: /d\\n\\n"
)

with open("support_userbot.py", "w", encoding="utf-8") as f:
    f.write(content)
print("Done")
