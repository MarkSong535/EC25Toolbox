from __future__ import annotations

from dataclasses import dataclass
import csv
import io
import re


@dataclass(frozen=True)
class SMSMessage:
    storage: str
    index: int
    status: str
    sender: str
    service_date: str
    body: str


def decode_ucs2_if_needed(value: str) -> str:
    clean = value.strip()
    if len(clean) < 4 or len(clean) % 4 != 0 or re.fullmatch(r"[0-9A-Fa-f]+", clean) is None:
        return value
    try:
        decoded = bytes.fromhex(clean).decode("utf-16-be")
    except (ValueError, UnicodeDecodeError):
        return value
    if not decoded or any(ord(character) < 0x20 and character not in "\r\n\t" for character in decoded):
        return value
    return decoded


def _csv_fields(header: str, prefix: str) -> list[str]:
    value = header[len(prefix):].strip()
    return next(csv.reader(io.StringIO(value), skipinitialspace=True), [])


def parse_cmgr(lines: list[str], storage: str, index: int) -> SMSMessage | None:
    header_index = next((position for position, line in enumerate(lines) if line.startswith("+CMGR:")), None)
    if header_index is None:
        return None
    fields = _csv_fields(lines[header_index], "+CMGR:")
    if not fields:
        return None
    body = "\n".join(lines[header_index + 1:]).strip()
    return SMSMessage(
        storage=storage,
        index=index,
        status=fields[0],
        sender=decode_ucs2_if_needed(fields[1]) if len(fields) > 1 else "",
        service_date=fields[3] if len(fields) > 3 else "",
        body=decode_ucs2_if_needed(body),
    )


def parse_cmgl(lines: list[str], storage: str) -> list[SMSMessage]:
    messages: list[SMSMessage] = []
    position = 0
    while position < len(lines):
        line = lines[position]
        if not line.startswith("+CMGL:"):
            position += 1
            continue
        fields = _csv_fields(line, "+CMGL:")
        position += 1
        body_lines: list[str] = []
        while position < len(lines) and not lines[position].startswith("+CMGL:"):
            body_lines.append(lines[position])
            position += 1
        if len(fields) < 2:
            continue
        try:
            index = int(fields[0])
        except ValueError:
            continue
        messages.append(
            SMSMessage(
                storage=storage,
                index=index,
                status=fields[1],
                sender=decode_ucs2_if_needed(fields[2]) if len(fields) > 2 else "",
                service_date=fields[4] if len(fields) > 4 else "",
                body=decode_ucs2_if_needed("\n".join(body_lines).strip()),
            )
        )
    return messages
