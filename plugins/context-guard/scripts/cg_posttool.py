"""PostToolUse: мягкий сигнал о пороге контекста внутри хода. Ход не блокируется.

Когда размер контекста (по журналу сессии, cg_transcript.context_tokens) достигает порога userConfig
(повтор — после роста ещё на шаг), хук добавляет в контекст модели additionalContext с просьбой записать
передачу сессии в ближайшей точке завершения подзадачи. Stop (cg_stop.py) после этого не блокирует ход,
если файл передачи уже обновлён после сигнала. Если файл передачи записан тем же шагом, по которому
замерен контекст (mtime >= времени записи assistant), сигнал не выводится: состояние фиксируется,
mid_trigger_at = min(время записи, mtime) - 0.001, и Stop засчитает передачу.

Хук вызывается после каждого инструмента, поэтому путь без срабатывания дешёвый и без git:
- вызовы саб-агентов (в payload есть `agent_id`) игнорируются;
- адресат передачи — дескриптор в каталоге журнала или собственный файл плагина (cg_handoff);
- неизвестная схема дескриптора, нет `CLAUDE_PLUGIN_DATA`, нет session_id вида [\\w-]+ — тишина.

Состояние — `sessions/<sid>.mid.json` (mid_last_trigger, mid_trigger_at, ctx_ignore), пишет только этот хук;
`sessions/<sid>.ctx.json` пишет Stop, здесь он только читается. Состояние сохраняется до вывода сигнала;
параллельные вызовы разводит неблокирующий lock `sessions/<sid>.mid.lock` (O_CREAT|O_EXCL, устаревший >30 с снимается).
Всегда код 0; любая ошибка — выход без вывода; вывод — ensure_ascii=True.
"""
from __future__ import annotations

import os
import time
from pathlib import Path

import cg_common
import cg_handoff
from cg_transcript import last_assistant

LOCK_STALE_SECONDS = 30
MID_KEYS = ("mid_last_trigger", "mid_trigger_at", "ctx_ignore")


def _acquire_lock(path: Path) -> bool:
    """Неблокирующий lock-файл; занято (свежий файл) → False; устаревший (>30 с) снимается."""
    for _ in range(2):
        try:
            os.close(os.open(str(path), os.O_CREAT | os.O_EXCL | os.O_WRONLY))
            return True
        except FileExistsError:
            try:
                if time.time() - path.stat().st_mtime <= LOCK_STALE_SECONDS:
                    return False
                path.unlink()
            except OSError:
                return False
    return False


def _release_lock(path: Path) -> None:
    """Снимает lock-файл; ошибки игнорируются."""
    try:
        path.unlink()
    except OSError:
        pass


def _decide(tokens: int, settings: dict, mid_last: int, ctx_last: int, ctx_ignore: int = 0) -> tuple:
    """Правило срабатывания. Возвращает (сработал, новый mid_last, новый ctx_ignore).

    last = max(mid_last, ctx_last), где ctx_last, равный ctx_ignore, не учитывается (цикл Stop уже сброшен здесь);
    цикл сбрасывается, если last > 0 и (tokens < порога или падение >= шага); при сбросе
    ctx_ignore = ctx_last (Stop читает это поле и тоже считает свой last нулевым). Затем
    tokens >= порога и (last == 0 или tokens >= last + шаг) — сигнал.
    """
    step, threshold = settings["repeat_step_tokens"], settings["threshold_tokens"]
    last = max(mid_last, 0 if ctx_last == ctx_ignore else ctx_last)
    if last > 0 and (tokens < threshold or last - tokens >= step):
        last = mid_last = 0
        if ctx_last > 0:
            ctx_ignore = ctx_last
    if tokens >= threshold and (last == 0 or tokens >= last + step):
        return True, tokens, ctx_ignore
    return False, mid_last, ctx_ignore


def signal_text(tokens: int, settings: dict, target: cg_handoff.Target) -> str:
    """Текст additionalContext (фактами, без императивов)."""
    return (f"context-guard: контекст сессии {tokens // 1000} тыс. токенов, порог "
            f"{settings['threshold_tokens'] // 1000} тыс.; каждый вызов инструмента перечитывает весь контекст. "
            f"На ближайшей точке завершения подзадачи {cg_handoff.handoff_instruction(target)} "
            "Ход завершается с предложением /clear. Если с последнего раздела ничего не изменилось "
            "и о передаче не просит другой хук, раздел не нужен. Если о передаче просит и другой хук, "
            "достаточно одного раздела.")


def run() -> None:
    """Основная логика хука; исключения гасит main()."""
    data = cg_common.plugin_data()
    payload = cg_common.read_stdin_json()
    if data is None or payload.get("agent_id"):
        return  # нет каталога данных либо вызов саб-агента (в payload главного потока agent_id нет)
    sid, tp = payload.get("session_id"), payload.get("transcript_path")
    if not (cg_common.valid_session_id(sid) and isinstance(tp, str) and tp):
        return
    target = cg_handoff.resolve_target(tp, data)
    if target is None:
        return
    settings = cg_common.context_settings()
    last = last_assistant(tp)
    if last is None:
        return
    tokens, ts = last

    sessions = data / "sessions"
    mid_path, lock_path = sessions / f"{sid}.mid.json", sessions / f"{sid}.mid.lock"
    ctx_path = sessions / f"{sid}.ctx.json"
    mid = cg_common.load_numbers(mid_path, MID_KEYS)
    ctx = cg_common.load_numbers(ctx_path, ("ctx_last_trigger",))
    fire, new_last, new_ignore = _decide(tokens, settings, int(mid["mid_last_trigger"]),
                                         int(ctx["ctx_last_trigger"]), int(mid["ctx_ignore"]))
    if not fire and new_last == mid["mid_last_trigger"] and new_ignore == mid["ctx_ignore"]:
        return  # дешёвый путь: ничего не меняется

    sessions.mkdir(parents=True, exist_ok=True)
    if not _acquire_lock(lock_path):
        return
    try:
        mid = cg_common.load_numbers(mid_path, MID_KEYS)  # повторное чтение под lock
        ctx = cg_common.load_numbers(ctx_path, ("ctx_last_trigger",))
        fire, new_last, new_ignore = _decide(tokens, settings, int(mid["mid_last_trigger"]),
                                             int(ctx["ctx_last_trigger"]), int(mid["ctx_ignore"]))
        if not fire and new_last == mid["mid_last_trigger"] and new_ignore == mid["ctx_ignore"]:
            return
        state = {"mid_last_trigger": new_last,
                 "mid_trigger_at": time.time() if fire else mid["mid_trigger_at"],
                 "ctx_ignore": new_ignore}
        out = None
        fresh = False
        if fire and ts is not None:
            # передача записана тем же шагом, что замерен (файл не старше записи assistant): сигнал не нужен
            mtime = cg_handoff.file_mtime(target.path) if target.path.exists() else 0.0
            fresh = target.path.exists() and mtime >= ts
            if fresh:
                state["mid_trigger_at"] = min(ts, mtime) - 0.001  # Stop засчитает передачу (mtime > at)
        if fire and not fresh:
            out = cg_common.dumps({"hookSpecificOutput": {
                "hookEventName": "PostToolUse",
                "additionalContext": signal_text(tokens, settings, target)}})
        cg_common.write_raw(mid_path, state)  # сначала сохранить; при сбое записи — исключение, вывода нет
        if out is not None:
            cg_common.emit(out)
    finally:
        _release_lock(lock_path)


def main() -> None:
    """Точка входа: любая ошибка → выход 0 без вывода (хук не должен ломать ход)."""
    try:
        run()
    except Exception:  # noqa: BLE001
        return
