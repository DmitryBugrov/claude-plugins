"""Общие помощники context-guard: UTF-8 ввод-вывод, настройки из userConfig, каталог состояния, атомарная запись.

Только стандартная библиотека; git не вызывается. Состояние хранится в `${CLAUDE_PLUGIN_DATA}` (вне репозиториев
проектов, живёт между версиями плагина); без этой переменной хуки молчат.
"""
from __future__ import annotations

import json
import math
import os
import re
import sys
from pathlib import Path

DEFAULT_THRESHOLD = 170_000
DEFAULT_STEP = 50_000

# На Windows консоль по умолчанию не UTF-8: без этого кириллица в stdout/stderr хуков портится.
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8")
    except (AttributeError, ValueError):
        pass


def read_stdin_json() -> dict:
    """Payload хука из stdin (байты декодируются как UTF-8: sys.stdin на Windows читает в cp1252); при ошибке {}."""
    try:
        raw = sys.stdin.buffer.read().decode("utf-8", errors="replace")
        data = json.loads(raw) if raw.strip() else {}
    except ValueError:
        return {}
    return data if isinstance(data, dict) else {}


def dumps(obj: dict) -> str:
    """JSON ответа хука; ensure_ascii=True исключает UnicodeEncodeError на суррогатах в stdout."""
    return json.dumps(obj, ensure_ascii=True)


def emit(text: str) -> None:
    """Выводит текст в stdout байтами UTF-8; несимволизируемые знаки (суррогаты) заменяются."""
    sys.stdout.buffer.write((text + "\n").encode("utf-8", errors="replace"))
    sys.stdout.buffer.flush()


def plugin_data() -> Path | None:
    """Каталог данных плагина или None, если переменная не задана (тогда хук молчит)."""
    raw = os.environ.get("CLAUDE_PLUGIN_DATA")
    return Path(raw) if raw and raw.strip() else None


def valid_session_id(sid) -> bool:
    """Идентификатор сессии пригоден для имени файла состояния: только [\\w-]+."""
    return isinstance(sid, str) and re.fullmatch(r"[\w-]+", sid) is not None


def _option_int(name: str, default: int) -> int:
    """Положительное целое из `CLAUDE_PLUGIN_OPTION_<name>` (значение — строка); иначе умолчание, молча."""
    raw = os.environ.get("CLAUDE_PLUGIN_OPTION_" + name)
    if raw is None:
        return default
    try:
        value = float(raw.strip())
    except ValueError:
        return default
    if not math.isfinite(value) or value <= 0 or value != int(value):
        return default
    return int(value)


def context_settings() -> dict:
    """Настройки: threshold_tokens и repeat_step_tokens из userConfig; отсутствие и ошибки дают умолчания."""
    return {
        "threshold_tokens": _option_int("THRESHOLD_TOKENS", DEFAULT_THRESHOLD),
        "repeat_step_tokens": _option_int("REPEAT_STEP_TOKENS", DEFAULT_STEP),
    }


def write_raw(path: Path, raw: dict) -> None:
    """Атомарная запись JSON: временный файл рядом + os.replace. При сбое старый файл цел."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + f".{os.getpid()}.tmp")
    try:
        tmp.write_text(json.dumps(raw, ensure_ascii=False, indent=2), encoding="utf-8")
        os.replace(tmp, path)
    finally:
        if tmp.exists():
            try:
                tmp.unlink()
            except OSError:
                pass


def load_numbers(path: Path, keys: tuple) -> dict:
    """Читает неотрицательные числовые поля состояния; битый файл, неверные типы и отсутствие файла дают нули.

    Прочие ошибки чтения (например, PermissionError) пробрасываются: состояние неизвестно, хук молчит.
    """
    state = {k: 0 for k in keys}
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, ValueError):
        return state
    if isinstance(raw, dict):
        for k in keys:
            v = raw.get(k)
            if isinstance(v, (int, float)) and not isinstance(v, bool) and v >= 0:
                state[k] = v
    return state
