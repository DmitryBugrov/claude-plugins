"""Замер размера контекста по журналу сессии Claude Code.

Берётся последняя запись ``assistant`` основного журнала (не ``isSidechain``, не ``isApiErrorMessage``,
с ненулевым ``usage``); токены = input + cache_creation_input + cache_read_input + output.
Читается хвост файла (последние 4 МБ, первая неполная строка отбрасывается); если там записи нет —
полный проход. `last_assistant` дополнительно отдаёт epoch-время записи (`timestamp`). `last_human_ts` — время
последнего сообщения человека (только хвост): запись `user` с `origin.kind == "human"` (origin на верхнем уровне записи)
или вложение `attachment` типа `queued_command` с `attachment.origin.kind == "human"` (набрано во время хода). Битые и обрезанные строки пропускаются. Журнал может отставать от текущего хода —
допустимая погрешность в один ответ. Только стандартная библиотека; журнал читается байтами.
"""
from __future__ import annotations

import json
import time
from datetime import datetime
from pathlib import Path

TAIL_BYTES = 4 * 1024 * 1024
TS_PAST_SECONDS = 24 * 3600
TS_FUTURE_SECONDS = 5 * 60
_USAGE_KEYS = ("input_tokens", "cache_creation_input_tokens", "cache_read_input_tokens", "output_tokens")


def _parse_ts(value) -> float | None:
    """Epoch-время из ISO-строки `timestamp` записи или None.

    Суффикс Z заменяется на +00:00; наивное время, не строка, мусор и время вне окна правдоподобия
    (now-24ч .. now+5мин) дают None. Исключения не выходят наружу.
    """
    try:
        if not isinstance(value, str):
            return None
        dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if dt.tzinfo is None:
            return None
        ts = dt.timestamp()
        now = time.time()
        return ts if now - TS_PAST_SECONDS <= ts <= now + TS_FUTURE_SECONDS else None
    except Exception:  # noqa: BLE001
        return None


def _entry_of(line: bytes) -> tuple[int, float | None] | None:
    """(токены, время) записи или None, если запись не подходит."""
    line = line.strip()
    if not line or b'"assistant"' not in line:
        return None
    try:
        rec = json.loads(line.decode("utf-8", errors="replace"))
    except ValueError:
        return None
    if not isinstance(rec, dict) or rec.get("type") != "assistant":
        return None
    if rec.get("isSidechain") or rec.get("isApiErrorMessage"):
        return None
    msg = rec.get("message")
    usage = msg.get("usage") if isinstance(msg, dict) else None
    if not isinstance(usage, dict):
        return None
    total = sum(v for k in _USAGE_KEYS if isinstance(v := usage.get(k), int) and not isinstance(v, bool))
    return (total, _parse_ts(rec.get("timestamp"))) if total else None


def _last_entry(data: bytes, drop_first: bool) -> tuple[int, float | None] | None:
    """Последняя подходящая запись в блоке байт; при чтении хвоста первая (неполная) строка отбрасывается."""
    lines = data.splitlines()
    if drop_first and lines:
        lines = lines[1:]
    for line in reversed(lines):
        e = _entry_of(line)
        if e is not None:
            return e
    return None


def last_assistant(path, tail_bytes: int = TAIL_BYTES) -> tuple[int, float | None] | None:
    """(токены, epoch-время записи или None) последней подходящей записи assistant; None, если её нет.

    Время берётся из `timestamp` записи (ISO, Z = UTC); наивное, отсутствующее, битое или вне окна
    правдоподобия время даёт None при валидных токенах. Любая ошибка чтения → None.
    """
    if not path:
        return None
    try:
        p = Path(path)
        size = p.stat().st_size
        with open(p, "rb") as f:
            if size > tail_bytes:
                f.seek(size - tail_bytes)
                e = _last_entry(f.read(), drop_first=True)
                if e is not None:
                    return e
                f.seek(0)
            return _last_entry(f.read(), drop_first=False)
    except Exception:  # noqa: BLE001 - по контракту любая ошибка чтения даёт None
        return None


def context_tokens(path, tail_bytes: int = TAIL_BYTES) -> int | None:
    """Размер контекста в токенах или None, если определить нельзя (любая ошибка → None)."""
    e = last_assistant(path, tail_bytes)
    return e[0] if e is not None else None


def _is_human(rec: dict) -> bool:
    """Запись основной цепочки — сообщение человека (по `origin.kind`, без текстовых эвристик)."""
    if rec.get("isSidechain"):
        return False
    kind = rec.get("type")
    if kind == "user":
        origin = rec.get("origin")
        if rec.get("isMeta") or not isinstance(origin, dict) or origin.get("kind") != "human":
            return False
        msg = rec.get("message")
        content = msg.get("content") if isinstance(msg, dict) else None
        if isinstance(content, list):
            return not any(isinstance(b, dict) and b.get("type") == "tool_result" for b in content)
        return True
    if kind == "attachment":
        att = rec.get("attachment")
        if not isinstance(att, dict) or att.get("type") != "queued_command":
            return False
        origin = att.get("origin")
        return isinstance(origin, dict) and origin.get("kind") == "human"
    return False


def last_human_ts(path, tail_bytes: int = TAIL_BYTES) -> float | None:
    """Epoch-время последнего сообщения человека в хвосте журнала или None (нет записи, нет времени, ошибка).

    Поиск с конца; строки без `"human"` пропускаются до разбора JSON; первая (неполная) строка хвоста отбрасывается.
    Время — `timestamp` записи (окно правдоподобия как в `_parse_ts`).
    """
    if not path:
        return None
    try:
        p = Path(path)
        size = p.stat().st_size
        with open(p, "rb") as f:
            drop = size > tail_bytes
            if drop:
                f.seek(size - tail_bytes)
            lines = f.read().splitlines()
        if drop and lines:
            lines = lines[1:]
        for line in reversed(lines):
            if b'"human"' not in line:
                continue
            try:
                rec = json.loads(line.decode("utf-8", errors="replace"))
            except ValueError:
                continue
            if isinstance(rec, dict) and _is_human(rec):
                return _parse_ts(rec.get("timestamp"))
        return None
    except Exception:  # noqa: BLE001 - по контракту любая ошибка чтения даёт None
        return None
