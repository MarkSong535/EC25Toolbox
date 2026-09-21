"""Single-segment UCS-2 SMS-SUBMIT encoder; user text never becomes AT syntax."""
import re


def encode_submit(number: str, body: str) -> tuple[str, int]:
    if not isinstance(number, str) or not re.fullmatch(r"\+[1-9][0-9]{6,14}", number):
        raise ValueError("Use an international number, e.g. +15025550123")
    if not isinstance(body, str) or not body.strip() or len(body) > 70:
        raise ValueError("SMS must contain 1–70 characters")
    if any(ord(c) > 0xFFFF or 0xD800 <= ord(c) <= 0xDFFF or
           (ord(c) < 32 and c not in "\n\t") or ord(c) == 127 for c in body):
        raise ValueError("SMS supports basic Unicode text; emoji and control characters are not supported")
    digits = number[1:]
    padded = digits + ("F" if len(digits) % 2 else "")
    address = "".join(padded[i + 1] + padded[i] for i in range(0, len(padded), 2))
    data = body.encode("utf-16-be")
    # Default SMSC, SUBMIT, MR=0, international destination, PID=0, UCS2 DCS.
    tpdu = f"0100{len(digits):02X}91{address}0008{len(data):02X}" + data.hex().upper()
    return "00" + tpdu, len(tpdu) // 2
