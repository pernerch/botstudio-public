import json
import re


_SECRET_NAME = re.compile(
    r"(?i)token|password|passwd|secret|authorization|credential|api[_-]?key|connection[_-]?string"
)
_SECRET_ASSIGNMENT = re.compile(
    r"""(?i)(\b(?:[\w.-]{0,128}(?:token|password|passwd|secret|authorization|credential|api[_-]?key|connection[_-]?string)[\w.-]{0,128})["']?\s*[:=]\s*)(?:"[^"]*"|'[^']*'|[^\s,;]+)"""
)
_TELEGRAM_TOKEN = re.compile(r"\b\d{6,}:[A-Za-z0-9_-]{20,}\b")
_AUTH_HEADER = re.compile(r"(?i)\b(Bearer|Basic)\s+[A-Za-z0-9_./+=:-]+")
_URL_CREDENTIALS = re.compile(r"(://)[^/\s:@]+:[^/\s@]+@")


def redact_log_text(text: str) -> str:
    text = _AUTH_HEADER.sub(r"\1 [redacted]", text)
    text = _SECRET_ASSIGNMENT.sub(r"\1[redacted]", text)
    text = _TELEGRAM_TOKEN.sub("[redacted Telegram token]", text)
    return _URL_CREDENTIALS.sub(r"\1[redacted]@", text)


def _redact_value(value):
    if isinstance(value, dict):
        sensitive_variable = any(
            isinstance(value.get(key), str) and _SECRET_NAME.search(value[key])
            for key in ("variable_name", "name", "key")
        )
        return {
            key: "[redacted]"
            if _SECRET_NAME.search(key)
            or (sensitive_variable and key in {"value", "old_value", "new_value"})
            else _redact_value(item)
            for key, item in value.items()
        }
    if isinstance(value, list):
        return [_redact_value(item) for item in value]
    if isinstance(value, str):
        return redact_log_text(value)
    return value


def redact_log_line(line: str) -> str:
    try:
        value = json.loads(line)
    except json.JSONDecodeError:
        if line.lstrip().startswith(("{", "[")):
            return "[Incomplete or malformed structured log record omitted]"
        return redact_log_text(line)
    if isinstance(value, dict):
        # The composed message duplicates detail values; export the structured
        # event/details instead so a sensitive variable cannot leak through it.
        if "details" in value and "event" in value:
            value.pop("message", None)
    return json.dumps(_redact_value(value), ensure_ascii=False)
