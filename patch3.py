with open("support_userbot.py", "r", encoding="utf-8") as f:
    lines = f.readlines()

for i, line in enumerate(lines):
    if "r\"^/(close|waiting|note|delete|confirm_delete|cancel_delete|support_on|support_off|support_status)(?:@\w+)?(?:\s+([\s\S]*))?$\"" in line:
        lines[i] = line.replace(
            "r\"^/(close|waiting|note|delete|confirm_delete|cancel_delete|support_on|support_off|support_status)(?:@\w+)?(?:\s+([\s\S]*))?$\"",
            "r\"^/(close|waiting|note|delete|d|confirm_delete|y|cancel_delete|n|support_on|on|support_off|off|support_status)(?:@\w+)?(?:\s+([\s\S]*))?$\""
        )
        print("Replaced!")

with open("support_userbot.py", "w", encoding="utf-8") as f:
    f.writelines(lines)
