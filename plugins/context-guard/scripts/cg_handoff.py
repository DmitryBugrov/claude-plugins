"""Поставщик передачи сессии: куда записывается передача и откуда она загружается.

Контракт:
1. Дескриптор `<каталог журнала>/handoff-provider.json` (его пишет внешний поставщик, например инструмент ведения проекта):
   `{"schema": 1, "provider": ..., "handoff_path": ..., "template": ..., "loads_on_start": true}`.
   Передача пишется в `handoff_path`, шаблон берётся из `template`; при `loads_on_start: true` поставщик сам
   загружает раздел при старте, плагин этого не делает. Неизвестная схема или битый дескриптор — плагин молчит.
2. Нет дескриптора — собственный файл `${CLAUDE_PLUGIN_DATA}/handoff/<имя каталога журнала>.md`
   (вне репозитория проекта), встроенный шаблон, загрузка последнего раздела при старте сессии.
Плагин знает только дескриптор; о формате конфигов поставщиков он не осведомлён.
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

DESCRIPTOR_NAME = "handoff-provider.json"
SCHEMA = 1
HANDOFF_LINES = 40
MAX_TEMPLATE_CHARS = 400
BUILTIN_TEMPLATE = ("состояние (что сделано, где остановились), решения и ответы пользователя, "
                    "открытые вопросы, следующие шаги")


@dataclass
class Target:
    """Адресат передачи: путь файла, шаблон, загружает ли раздел при старте внешний поставщик."""
    path: Path
    template: str
    loads_on_start: bool
    external: bool


def resolve_target(transcript_path, data: Path | None) -> Target | None:
    """Адресат передачи для сессии или None (плагин молчит).

    None: путь журнала не задан; дескриптор нечитаем, неизвестной схемы, без provider, без handoff_path
    или с относительным handoff_path; либо нет каталога данных плагина при отсутствии дескриптора.
    """
    if not isinstance(transcript_path, str) or not transcript_path:
        return None
    logdir = Path(transcript_path).parent
    try:
        raw = json.loads((logdir / DESCRIPTOR_NAME).read_text(encoding="utf-8"))
    except FileNotFoundError:
        return _own_target(logdir, data)
    except (OSError, ValueError):
        return None
    if not isinstance(raw, dict):
        return None
    schema, hp, provider = raw.get("schema"), raw.get("handoff_path"), raw.get("provider")
    if schema != SCHEMA or isinstance(schema, bool) or not isinstance(hp, str) or not hp.strip():
        return None
    if not isinstance(provider, str) or not provider.strip():
        return None  # дескриптор без поставщика считается неизвестным
    if not Path(hp).is_absolute():
        return None  # относительный путь разрешался бы от cwd хука: адресат непредсказуем
    template = raw.get("template")
    if not (isinstance(template, str) and template.strip()):
        template = BUILTIN_TEMPLATE
    return Target(Path(hp), template.strip()[:MAX_TEMPLATE_CHARS], raw.get("loads_on_start") is True, True)


def _own_target(logdir: Path, data: Path | None) -> Target | None:
    """Собственный файл передачи плагина: по одному на каталог журнала (проект Claude)."""
    name = re.sub(r"[^\w.-]", "_", logdir.name)
    if data is None or not name.strip("._"):
        return None
    return Target(data / "handoff" / f"{name}.md", BUILTIN_TEMPLATE, False, False)


def file_mtime(path: Path) -> float:
    """Время последней правки файла; 0.0, если файла нет."""
    return path.stat().st_mtime if path.exists() else 0.0


def last_section(path: Path, n: int = HANDOFF_LINES) -> list:
    """Последний раздел второго уровня (`## ...`) файла, не более n строк; без разделов — последние n строк."""
    try:
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return []
    idx = [i for i, line in enumerate(lines) if line.startswith("## ")]
    if not idx:
        return lines[-n:]
    return lines[idx[-1]:idx[-1] + n]


def now_text(now: datetime | None = None) -> str:
    """Локальное время хука `ГГГГ-ММ-ДД ЧЧ:ММ`: модель своих часов не имеет и время в заголовке выдумывает."""
    return (now or datetime.now()).strftime("%Y-%m-%d %H:%M")


def handoff_instruction(target: Target, now: datetime | None = None) -> str:
    """Фраза о том, как записывается передача (общая для сигнала внутри хода и блоков Stop).

    Свой файл: готовый заголовок раздела со временем сигнала. Внешний поставщик: только текущее время,
    формат заголовка задаёт его шаблон.
    """
    stamp = now_text(now)
    if target.external:
        tail = f" Время сигнала: {stamp}."
    else:
        tail = f" Раздел `## {stamp}` (время сигнала подставлено плагином) добавляется в конец файла."
    return (f"передача сессии записывается в {target.path.as_posix()} (шаблон: {target.template}; "
            f"плюс решения и ответы пользователя, не записанные в артефакты; до {HANDOFF_LINES} строк).{tail} "
            "Открытые пункты прошлого раздела переносятся в новый или явно закрываются (сделано / отменено). "
            "Открытые вопросы пользователю — дословно, с вариантами, по одному в строке (после /clear их задают заново); "
            "не помещаются в лимит — полный текст в отдельный файл рядом с передачей и ссылка на него.")
