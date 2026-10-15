"""Stop: контекстный триггер — не даёт завершить ход без передачи сессии, когда контекст перерос порог.

Размер контекста — по журналу сессии (cg_transcript.context_tokens). Порог и шаг повтора — userConfig плагина.
Состояние цикла — `${CLAUDE_PLUGIN_DATA}/sessions/<sid>.ctx.json` (ctx_last_trigger, ctx_trigger_at, ctx_pending
и для П2: rec_cycle, p2_base, p2_seen_ts, p2_pending, p2_at; старый файл без этих полей читается с умолчаниями);
без `CLAUDE_PLUGIN_DATA`, без session_id вида [\\w-]+ и без адресата передачи (cg_handoff) хук молчит.
Если передача уже записана по сигналу внутри хода (cg_posttool, файл `<sid>.mid.json` — только чтение:
файл передачи новее сигнала, рост с него меньше шага), блока нет, ctx_last_trigger = tokens.

П2 (устаревшая передача): рекомендация /clear уже была в этом цикле (rec_cycle == ctx_last_trigger > 0), контекст >= порога
и после записи передачи (mtime файла) человек отправил сообщение (cg_transcript.last_human_ts), ещё не обработанное
проверкой (p2_seen_ts), а контекст вырос не меньше чем на P2_STEP_TOKENS (25 тыс.) относительно
max(p2_base, rec_cycle). p2_base — размер контекста при последней проверке П2 или записи передачи (ставится при
записи после ctx-блока, в ветке Д3, при срабатывании П2 и при обновлённой передаче после П2); умолчание 0.
Тогда блок с просьбой обновить передачу — не более одного на сообщение человека. Порядок на Stop:
ctx-блок -> ветка Д3 -> П2; если сработали первые, сообщения человека до этого момента помечаются обработанными.
Повторный Stop после блока П2 (p2_pending): если файл передачи обновлён после p2_at — флаг полосы и systemMessage
«передача обновлена», иначе тишина. Блок П2 без systemMessage (только decision и reason).

Ответы — только JSON в stdout с кодом 0 (хук никогда не возвращает 2):
- блокировка: {"decision": "block", "reason": <для модели>, "systemMessage": <для пользователя>};
- повторный Stop после блокировки (stop_hook_active): только systemMessage о результате;
- передача уже записана по сигналу внутри хода: только systemMessage с рекомендацией /clear (без блока), один раз на цикл.
Рекомендация (`clear_message`) начинается с «⚠ /clear». Вместе с ней, после записи состояния, пишется флаг полосы
`sessions/<sid>.clear.json` {tokens, handoff, at} (для внешнего UI; сбой записи флага сообщение не гасит);
его удаляет SessionStart при source == compact.
Вывод — ensure_ascii=True. Любая внутренняя ошибка — выход 0 без вывода.
"""
from __future__ import annotations

import json
import time
from pathlib import Path

import cg_common
import cg_handoff
from cg_transcript import context_tokens, last_human_ts

MAX_BG_TASKS = 10
MAX_BG_CHARS = 10_000
MAX_BG_DESC = 120
PREFIX = "context-guard"
P2_STEP_TOKENS = 25_000  # минимальный рост контекста между проверками П2
# общий хвост reason обоих блоков
REASON_TAIL = ("В конце ответа пользователю (не в файле передачи) повтори открытый вопрос, если он есть. "
               "Затем завершай ход.")


def _running_tasks(payload: dict) -> list | None:
    """Выполняющиеся фоновые задачи из payload; None, если поле отсутствует или не список."""
    if "background_tasks" not in payload:
        return None
    tasks = payload["background_tasks"]
    if not isinstance(tasks, list):
        return None
    return [t for t in tasks if isinstance(t, dict) and t.get("status") == "running"]


def background_summary(payload: dict) -> str:
    """Текст о фоновых задачах: «неизвестно» / «нет» / список (тип и описание, без command)."""
    running = _running_tasks(payload)
    if running is None:
        return "неизвестно"
    if not running:
        return "нет"
    items = []
    for t in running[:MAX_BG_TASKS]:
        kind = str(t.get("agent_type") or t.get("type") or "задача")
        desc = str(t.get("description") or "")[:MAX_BG_DESC]
        items.append(f"{kind}: {desc}" if desc else kind)
    text = "; ".join(items)
    if len(running) > MAX_BG_TASKS:
        text += f"; ещё {len(running) - MAX_BG_TASKS}"
    return text[:MAX_BG_CHARS]


def _bg_hint(payload: dict) -> str:
    """Просьба записать в передачу ожидания от работающих фоновых задач; пусто, если их нет."""
    return " Запиши в передачу, чего ждёшь от каждой." if _running_tasks(payload) else ""


def _load_ctx_state(path: Path) -> dict:
    """Читает состояние контекстного триггера; битый файл и неверные типы дают нулевое состояние."""
    state = {"ctx_last_trigger": 0, "ctx_trigger_at": 0.0, "ctx_pending": False,
             "rec_cycle": 0, "p2_base": 0, "p2_seen_ts": 0.0, "p2_pending": False, "p2_at": 0.0}
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return state
    if not isinstance(raw, dict):
        return state
    for key in ("ctx_last_trigger", "rec_cycle", "p2_base"):
        v = raw.get(key)
        if isinstance(v, int) and not isinstance(v, bool) and v >= 0:
            state[key] = v
    for key in ("ctx_trigger_at", "p2_seen_ts", "p2_at"):
        v = raw.get(key)
        if isinstance(v, (int, float)) and not isinstance(v, bool):
            state[key] = float(v)
    for key in ("ctx_pending", "p2_pending"):
        state[key] = raw.get(key) is True
    return state


def context_trigger(tokens: int, settings: dict, state: dict) -> bool:
    """Контекстный триггер; изменяет `state`.

    Сначала сброс цикла (признак сжатия): только если last > 0 и (tokens < порога или падение >= шага);
    небольшие колебания выше порога цикл не сбрасывают. Затем проверка порога и шага.
    """
    step = settings["repeat_step_tokens"]
    last = state["ctx_last_trigger"]
    if last > 0 and (tokens < settings["threshold_tokens"] or last - tokens >= step):
        state["ctx_last_trigger"] = last = 0
    if tokens >= settings["threshold_tokens"] and (last == 0 or tokens >= last + step):
        state["ctx_last_trigger"] = tokens
        state["ctx_trigger_at"] = time.time()
        state["ctx_pending"] = True
        return True
    return False


def midturn_handoff_done(mid_path: Path, tokens: int, settings: dict, target: cg_handoff.Target) -> bool:
    """Передача уже записана по сигналу внутри хода (файл `<sid>.mid.json` только читается).

    Да, если сигнал был (mid_last_trigger > 0), рост с него меньше шага (тот же цикл) и файл передачи новее сигнала.
    """
    try:
        raw = json.loads(mid_path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return False
    if not isinstance(raw, dict):
        return False
    last, at = raw.get("mid_last_trigger"), raw.get("mid_trigger_at")
    if not all(isinstance(v, (int, float)) and not isinstance(v, bool) for v in (last, at)):
        return False
    return (last > 0 and tokens < last + settings["repeat_step_tokens"]
            and target.path.exists() and cg_handoff.file_mtime(target.path) > at)


def _handoff_done_safe(mid_path: Path, tokens: int, settings: dict, target: cg_handoff.Target) -> bool:
    """midturn_handoff_done с защитой: любая ошибка (например, stat файла) → False, блок сохраняется."""
    try:
        return midturn_handoff_done(mid_path, tokens, settings, target)
    except Exception:  # noqa: BLE001
        return False


def _mid_ctx_ignore(mid_path: Path) -> int:
    """Поле `ctx_ignore` из `<sid>.mid.json` (только чтение): значение ctx_last_trigger, уже сброшенное хуком; иначе 0."""
    try:
        raw = json.loads(mid_path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return 0
    v = raw.get("ctx_ignore") if isinstance(raw, dict) else None
    return v if isinstance(v, int) and not isinstance(v, bool) and v > 0 else 0


def clear_message(tokens: int, target: cg_handoff.Target, payload: dict, verb: str = "записана") -> str:
    """Рекомендация /clear: размер контекста, имя файла передачи (verb: записана | обновлена) и число фоновых задач."""
    msg = f"⚠ /clear: контекст {tokens // 1000} тыс. — передача {verb} в {target.path.name}."
    running = _running_tasks(payload)
    if running:
        if all(t.get("agent_type") for t in running):
            # отчёты фоновых агентов доходят в новую сессию (проверено вживую); для прочих задач не проверялось
            msg += f" Работают фоновые агенты: {len(running)} — их отчёты дойдут после /clear."
        else:
            msg += f" Работают фоновые задачи: {len(running)} — дождитесь завершения до /clear."
    return msg


def _write_clear_flag(sessions: Path, sid: str, tokens: int, target: cg_handoff.Target) -> None:
    """Флаг полосы `<sid>.clear.json` (атомарно); любой сбой записи молча игнорируется."""
    try:
        cg_common.write_raw(sessions / f"{sid}.clear.json",
                            {"tokens": tokens, "handoff": target.path.name, "at": time.time()})
    except Exception:  # noqa: BLE001
        pass


def _report_after_block(payload: dict, state: dict, state_path: Path, target: cg_handoff.Target) -> None:
    """Повторный Stop после блокировки: один раз сообщает пользователю результат."""
    if not state["ctx_pending"]:
        return
    state["ctx_pending"] = False
    written = target.path.exists() and cg_handoff.file_mtime(target.path) > state["ctx_trigger_at"]
    if written:
        state["rec_cycle"] = state["ctx_last_trigger"]  # рекомендация /clear выводится: цикл «с рекомендацией»
        state["p2_base"] = state["ctx_last_trigger"]  # база роста для П2: размер на момент записи передачи
    cg_common.write_raw(state_path, state)  # сброс до вывода: сообщение показывается один раз
    if written:
        _write_clear_flag(state_path.parent, state_path.name.removesuffix(".ctx.json"),
                          state["ctx_last_trigger"], target)
        msg = clear_message(state["ctx_last_trigger"], target, payload)
    else:
        msg = (f"{PREFIX}: файл передачи не обновлялся после сигнала ({state['ctx_last_trigger'] // 1000} тыс.); "
               "если передача нужна — запишите её до /clear.")
    cg_common.emit(cg_common.dumps({"systemMessage": msg}))


def _report_after_p2(payload: dict, state: dict, state_path: Path, target: cg_handoff.Target) -> None:
    """Повторный Stop после блока П2: сброс p2_pending; если передача обновлена после блока — флаг и сообщение."""
    if not state["p2_pending"]:
        return
    state["p2_pending"] = False
    updated = target.path.exists() and cg_handoff.file_mtime(target.path) > state["p2_at"]
    tokens = (context_tokens(payload.get("transcript_path")) or state["rec_cycle"]) if updated else 0
    if updated:
        state["p2_base"] = tokens  # передача обновлена: следующий П2 — после роста на шаг от текущего размера
    cg_common.write_raw(state_path, state)  # сброс до вывода: сообщение показывается один раз
    if not updated:
        return
    _write_clear_flag(state_path.parent, state_path.name.removesuffix(".ctx.json"), tokens, target)
    cg_common.emit(cg_common.dumps({"systemMessage": clear_message(tokens, target, payload, "обновлена")}))


def build_p2_block(target: cg_handoff.Target) -> str:
    """JSON-строка блокировки П2: после последней записи передачи были сообщения человека."""
    reason = (f"{PREFIX}: после последней записи передачи были сообщения пользователя. Допиши в передачу новый полный "
              f"раздел (он заменяет предыдущий; до {cg_handoff.HANDOFF_LINES} строк): прежнее состояние, "
              "ответы пользователя и изменения. Если новое в сообщениях несущественно и обновлять нечего, раздел не нужен. "
              "Если о передаче просит и другой хук, достаточно одного раздела. "
              f"Сейчас {cg_handoff.handoff_instruction(target)} {REASON_TAIL}")
    return cg_common.dumps({"decision": "block", "reason": reason})


def _p2_candidate(state: dict, tokens: int | None, settings: dict, transcript_path, target: cg_handoff.Target):
    """Время сообщения человека h для блока П2 или None. Дешёвые условия — до чтения журнала."""
    last = state["ctx_last_trigger"]
    if not (last > 0 and state["rec_cycle"] == last and tokens is not None and tokens >= settings["threshold_tokens"]):
        return None
    if tokens - max(state["p2_base"], state["rec_cycle"]) < P2_STEP_TOKENS:
        return None  # контекст не вырос на шаг с прошлой проверки/записи: журнал не читаем, p2_seen_ts не двигаем
    h = last_human_ts(transcript_path)
    if h is None or h <= max(cg_handoff.file_mtime(target.path), state["p2_seen_ts"]):
        return None
    return h


def build_block(target: cg_handoff.Target, payload: dict, tokens: int, settings: dict) -> str:
    """Собирает JSON-строку блокировки по контексту."""
    n, t = tokens // 1000, settings["threshold_tokens"] // 1000
    reason = (f"{PREFIX}: контекст сессии {n} тыс. токенов, порог {t} тыс.; каждый ход перечитывает весь контекст. "
              "Если с последнего раздела ничего не изменилось и о передаче не просит другой хук, раздел не нужен. "
              "Если о передаче просит и другой хук, достаточно одного раздела. "
              "После записи пользователю предлагается /clear. "
              f"Фоновые задачи: {background_summary(payload)}.{_bg_hint(payload)} "
              f"Сейчас {cg_handoff.handoff_instruction(target)} {REASON_TAIL}")
    system = f"{PREFIX}: контекст {n} тыс. токенов (порог {t} тыс.) — записывается передача сессии"
    return cg_common.dumps({"decision": "block", "reason": reason, "systemMessage": system})


def run() -> None:
    """Основная логика хука; исключения гасит main()."""
    data = cg_common.plugin_data()
    payload = cg_common.read_stdin_json()
    sid = payload.get("session_id")
    if data is None or not cg_common.valid_session_id(sid):
        return
    target = cg_handoff.resolve_target(payload.get("transcript_path"), data)
    if target is None:
        return
    settings = cg_common.context_settings()
    sessions = data / "sessions"
    state_path, mid_path = sessions / f"{sid}.ctx.json", sessions / f"{sid}.mid.json"

    if payload.get("stop_hook_active"):
        st = _load_ctx_state(state_path)
        if st["ctx_pending"]:
            _report_after_block(payload, st, state_path, target)
        else:
            _report_after_p2(payload, st, state_path, target)
        return

    state = _load_ctx_state(state_path)
    before = dict(state)
    tokens = context_tokens(payload.get("transcript_path"))
    ctx_fired = False
    recommend = False
    if tokens is not None:
        trial = dict(state)
        ignore = _mid_ctx_ignore(mid_path)
        if ignore > 0 and ignore == trial["ctx_last_trigger"]:
            trial["ctx_last_trigger"] = 0  # цикл сброшен хуком внутри хода (сжатие): старый last не учитывается
        ctx_fired = context_trigger(tokens, settings, trial)
        if ctx_fired and _handoff_done_safe(mid_path, tokens, settings, target):
            # передача уже записана после сигнала внутри хода: не блокируем, цикл сдвигаем на текущий размер
            state["ctx_last_trigger"] = tokens
            state["rec_cycle"] = tokens  # рекомендация /clear выводится ниже
            state["p2_base"] = tokens
            ctx_fired = False
            recommend = True  # сообщение с рекомендацией /clear — после успешной записи состояния
        else:
            state = trial
    if not ctx_fired:
        state["ctx_pending"] = False  # обычный Stop без срабатывания: устаревший pending снимается

    out = build_block(target, payload, tokens, settings) if ctx_fired else None  # строка вывода — до записи состояния
    if ctx_fired or recommend:
        # ctx-блок или Д3 уже обработали этот Stop: сообщения человека к этому моменту П2 не вызывают
        h = last_human_ts(payload.get("transcript_path"))
        if h is not None:
            state["p2_seen_ts"] = h
        state["p2_pending"] = False
    else:
        h = _p2_candidate(state, tokens, settings, payload.get("transcript_path"), target)
        state["p2_pending"] = h is not None
        if h is not None:
            state["p2_seen_ts"], state["p2_at"] = h, time.time()
            state["p2_base"] = tokens  # база — момент проверки, а не записи: без роста на шаг повтора нет
            out = build_p2_block(target)
    if state != before:
        try:
            cg_common.write_raw(state_path, state)
        except Exception:  # noqa: BLE001 - не сохранили состояние: не блокируем (иначе повтор на каждом Stop)
            out = None
            recommend = False
    if recommend and tokens is not None:
        _write_clear_flag(sessions, sid, tokens, target)
        out = cg_common.dumps({"systemMessage": clear_message(tokens, target, payload)})
    if out is not None:
        cg_common.emit(out)


def main() -> None:
    """Точка входа: любая ошибка → выход 0 без вывода (хук не должен ломать ход)."""
    try:
        run()
    except Exception:  # noqa: BLE001
        return
