with open("support_userbot.py", "r", encoding="utf-8") as f:
    content = f.read()

content = content.replace(
    r"r\"^/(close|waiting|note|delete|confirm_delete|cancel_delete|support_on|support_off|support_status)(?:@\\w+)?(?:\\s+([\\s\\S]*))?$\"",
    r'r"^/(close|waiting|note|delete|d|confirm_delete|y|cancel_delete|n|support_on|on|support_off|off|support_status)(?:@\\w+)?(?:\\s+([\\s\\S]*))?$"'
)

with open("support_userbot.py", "w", encoding="utf-8") as f:
    f.write(content)
print("Done")
