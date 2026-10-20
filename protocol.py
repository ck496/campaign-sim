"""Message parsing and builders (PRD section 6.3)."""
from __future__ import annotations

from typing import Any, Dict, Optional, Tuple

# --- client command specs ---
# Maps each cmd to its fields; used by parse_client_msg below.

_STR = (str, True)
_STR_OPT = (str, False)
_INT = (int, True)
_INT_OPT = (int, False)
_LIST_INT = (list, True)

CLIENT_CMDS: Dict[str, Dict[str, Tuple]] = {
    "join":     {"name": _STR},
    "act":      {"kind": _STR, "district": _STR_OPT,
                 "target": _INT_OPT},
    "end":      {},
    "say":      {"text": _STR},
    "whisper":  {"to": _INT, "text": _STR},
    "group":    {"to": _LIST_INT, "text": _STR},
    "threaten": {"to": _LIST_INT, "text": _STR},
    "truce":    {"to": _INT},
    "me":       {},
    "map":      {},
    "help":     {},
}

# --- server message types ---
# Valid values for the "t" field; checked by build_server_msg.

SERVER_MSGS = frozenset([
    "welcome", "round", "queued", "error", "warn", "news",
    "result", "poll", "chat", "threat", "truce", "election",
])


# --- parsing ---
# Validates client messages; server.py calls these on every inbound dict.

def parse_client_msg(raw: dict) -> dict:
    """Validate a client message dict. Returns a clean copy.

    Raises ValueError on unknown cmd, missing required field,
    or wrong field type.
    """
    if not isinstance(raw, dict):
        raise ValueError("message must be a dict")
    cmd = raw.get("cmd")
    if cmd is None:
        raise ValueError("missing 'cmd'")
    if not isinstance(cmd, str):
        raise ValueError("'cmd' must be a string")
    if cmd not in CLIENT_CMDS:
        raise ValueError(f"unknown cmd '{cmd}'")

    spec = CLIENT_CMDS[cmd]
    out: Dict[str, Any] = {"cmd": cmd}

    for field, (expected_type, required) in spec.items():
        val = raw.get(field)
        if val is None:
            if required:
                raise ValueError(
                    f"cmd '{cmd}': missing required field '{field}'"
                )
            continue
        if not isinstance(val, expected_type):
            raise ValueError(
                f"cmd '{cmd}': field '{field}' must be "
                f"{expected_type.__name__}, got "
                f"{type(val).__name__}"
            )
        out[field] = val

    return out


def build_server_msg(t: str, **kw: Any) -> dict:
    """Build a server message dict.

    Raises ValueError for unknown message type.
    """
    if t not in SERVER_MSGS:
        raise ValueError(f"unknown server message type '{t}'")
    msg: Dict[str, Any] = {"t": t}
    msg.update(kw)
    return msg


# --- helpers ---
# Sanitization; server.py calls strip_control on chat text before relay.

def strip_control(text: str) -> str:
    """Remove control characters except newline (S-6)."""
    return "".join(
        c for c in text if c == "\n" or (ord(c) >= 0x20)
    )
