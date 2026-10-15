"""SessionStart (startup|resume|clear|compact): строка после сжатия, загрузка передачи, чистка состояния.

- source == "compact": строка «Контекст сжат…» (решения после последнего раздела могли не попасть в передачу);
- передача загружается (последний раздел `## ` файла, до 40 строк) только когда поставщик сам её не загружает:
  нет дескриптора (собственный файл `${CLAUDE_PLUGIN_DATA}/handoff/<имя каталога журнала>.md`) либо в дескрипторе
  `loads_on_start` не true; при `loads_on_start: true` раздел загружает поставщик;
- source == "compact": удаляется флаг полосы `sessions/<sid>.clear.json` (ошибки игнорируются);
- чистка `${CLAUDE_PLUGIN_DATA}/sessions/` (включая `*.clear.json`): файлы старше 7 дней, кроме файлов текущей сессии; `handoff/` не трогается;
- неизвестная схема дескриптора, нет `CLAUDE_PLUGIN_DATA` — тишина.
Вывод — обычный текст в stdout (контекст сессии), код 0; любая ошибка — выход без вывода.
"""
from __future__ import annotations

import time
from pathlib import Path

import cg_common
import cg_handoff

RETENTION_SECONDS = 7 * 24 * 3600
MAX_SAFE_THRESHOLD = 180_000
COMPACT_LINE = ("Контекст сжат. Решения после последнего раздела передачи могли не попасть в неё — "
                "при следующей остановке дополни раздел.")


def cleanup_sessions(sessions: Path, current_sid: str, now: float | None = None) -> None:
    """Удаляет файлы состояния старше 7 дней (по времени правки), кроме файлов текущей сессии; ошибки молча."""
    now = time.time() if now is None else now
    try:
        entries = list(sessions.iterdir())
    except OSError:
        return
    for p in entries:
        try:
            if p.name.startswith(current_sid + ".") or not p.is_file():
                continue
            if now - p.stat().st_mtime > RETENTION_SECONDS:
                p.unlink()
        except OSError:
            continue


def threshold_warning() -> list:
    """Строка-предупреждение, если порог выше MAX_SAFE_THRESHOLD; сбой чтения настроек не ломает старт."""
    try:
        n = cg_common.context_settings()["threshold_tokens"]
    except Exception:  # noqa: BLE001
        return []
    if n > MAX_SAFE_THRESHOLD:
        return [f"Внимание: порог контекста {n} выше {MAX_SAFE_THRESHOLD} — близко к автосжатию.", ""]
    return []


def run() -> None:
    """Основная логика хука; исключения гасит main()."""
    data = cg_common.plugin_data()
    payload = cg_common.read_stdin_json()
    if data is None:
        return
    sid = payload.get("session_id")
    if cg_common.valid_session_id(sid):
        cleanup_sessions(data / "sessions", sid)
        if payload.get("source") == "compact":
            try:
                (data / "sessions" / f"{sid}.clear.json").unlink()  # после сжатия контекст мал: полоса не нужна
            except OSError:
                pass
    target = cg_handoff.resolve_target(payload.get("transcript_path"), data)
    out = threshold_warning()  # предупреждение не зависит от дескриптора и источника SessionStart
    if target is None:
        if out:
            cg_common.emit("\n".join(out))
        return
    if payload.get("source") == "compact":
        out += [COMPACT_LINE, ""]
    if not target.loads_on_start:
        section = cg_handoff.last_section(target.path)
        if section:
            out += [f"### Последняя передача сессии ({target.path.as_posix()})"] + section + [""]
    if out:
        cg_common.emit("\n".join(out))


def main() -> None:
    """Точка входа: любая ошибка → выход 0 без вывода (хук не должен ломать старт сессии)."""
    try:
        run()
    except Exception:  # noqa: BLE001
        return
