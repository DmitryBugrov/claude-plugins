"""Тесты контекстного триггера Stop (cg_stop.py через run.py): порог, шаг, сброс цикла, проверка результата."""
from __future__ import annotations

import json
import os
import stat
import sys
import time
from pathlib import Path

import pytest

T = 170_000

from cg_fixtures import human_user, queued_human  # noqa: E402

P2_STEP = 25_000  # cg_stop.P2_STEP_TOKENS


def blocked(res) -> dict:
    """Ожидает блокировку JSON-ом с кодом 0 и возвращает ответ."""
    assert res.code == 0, res.stderr
    out = json.loads(res.stdout)
    assert out["decision"] == "block"
    assert "reason" in out and "systemMessage" in out
    return out


def silent(res) -> None:
    """Ожидает код 0 и пустой stdout."""
    assert res.code == 0, res.stderr
    assert res.stdout.strip() == ""


def stop(run_hook, env, **extra):
    """Запуск Stop для сессии s1."""
    return run_hook("Stop", env.payload(**extra))


def test_below_threshold_silent(env, run_hook):
    env.set_tokens(T - 1)
    silent(stop(run_hook, env))
    assert env.ctx_state() is None


def test_crossing_blocks_once(env, run_hook):
    env.set_tokens(180_000)
    out = blocked(stop(run_hook, env))
    assert "180 тыс." in out["reason"] and "порог 170 тыс." in out["reason"]
    assert env.handoff.as_posix() in out["reason"] and "шаблон" in out["reason"]
    assert "ничего не изменилось и о передаче не просит другой хук, раздел не нужен" in out["reason"]
    assert "достаточно одного раздела" in out["reason"]
    assert "180 тыс." in out["systemMessage"]
    st = env.ctx_state()
    assert st["ctx_last_trigger"] == 180_000 and st["ctx_pending"] is True and st["ctx_trigger_at"] > 0
    silent(stop(run_hook, env))  # тот же размер: повторной блокировки нет


def test_repeat_needs_step(env, run_hook):
    env.set_tokens(180_000)
    blocked(stop(run_hook, env))
    env.set_tokens(209_999)
    silent(stop(run_hook, env))
    env.set_tokens(230_000)
    blocked(stop(run_hook, env))
    assert env.ctx_state()["ctx_last_trigger"] == 230_000


def test_drop_resets_cycle(env, run_hook):
    env.set_tokens(280_000)
    blocked(stop(run_hook, env))
    env.set_tokens(220_000)  # падение 60K >= шага 50K, но выше порога: цикл сброшен, срабатывает сразу
    blocked(stop(run_hook, env))
    assert env.ctx_state()["ctx_last_trigger"] == 220_000


def test_small_drop_above_threshold_no_reset(env, run_hook):
    env.set_tokens(220_000)
    blocked(stop(run_hook, env))
    env.set_tokens(196_789)  # падение < шага и выше порога: цикл не сбрасывается
    silent(stop(run_hook, env))
    assert env.ctx_state()["ctx_last_trigger"] == 220_000


def test_drop_below_threshold_then_cross_resets(env, run_hook):
    env.set_tokens(220_000)
    blocked(stop(run_hook, env))
    env.set_tokens(30_000)
    silent(stop(run_hook, env))
    assert env.ctx_state()["ctx_last_trigger"] == 0
    env.set_tokens(171_000)  # меньше last(200K)+step, но цикл обнулён
    blocked(stop(run_hook, env))


def test_none_tokens_silent(env, run_hook):
    silent(stop(run_hook, env))  # журнала нет
    env.transcript.write_bytes(b"")
    silent(stop(run_hook, env))


def test_missing_session_id_silent(env, run_hook):
    env.set_tokens(400_000)
    silent(run_hook("Stop", env.payload(sid=None)))
    assert not env.sessions.exists() or not list(env.sessions.glob("*.ctx.json"))


def test_bad_session_id_silent(env, run_hook):
    env.set_tokens(400_000)
    silent(run_hook("Stop", env.payload(sid="../x")))
    assert not (env.data / "x.ctx.json").exists()
    assert not env.sessions.exists() or not list(env.sessions.glob("*.ctx.json"))


def test_exception_silent_exit0(env, run_hook):
    """Сбои на разных уровнях дают код 0 и пустой stdout."""
    silent(run_hook("Stop", env.payload(sid="s2", transcript_path=str(env.logdir))))  # журнал — каталог
    env.set_tokens(400_000)
    (env.sessions / "s4.ctx.json").mkdir(parents=True)  # запись состояния невозможна
    silent(run_hook("Stop", env.payload(sid="s4")))
    silent(run_hook("Stop", None, raw=b"[]"))  # payload — не объект
    silent(run_hook("Stop", None, raw=b"\xff\xfe not json"))


def test_lone_surrogate_in_background_still_blocks(env, run_hook):
    env.set_tokens(180_000)
    payload = env.payload(background_tasks=[{"status": "running", "type": "bash", "description": "XDESC"}])
    text = json.dumps(payload).replace("XDESC", "\\ud800")  # в JSON-тексте остаётся экранированный \ud800
    assert "\\ud800" in text
    out = blocked(run_hook("Stop", None, raw=text.encode("utf-8")))
    assert "bash" in out["reason"]
    st = env.ctx_state()
    assert st["ctx_last_trigger"] == 180_000 and st["ctx_pending"] is True


def test_pending_cleared_by_normal_stop(env, run_hook):
    env.set_tokens(180_000)
    blocked(stop(run_hook, env))
    assert env.ctx_state()["ctx_pending"] is True
    env.set_tokens(160_500)  # продолжение не состоялось (Esc): обычный Stop без срабатывания
    silent(stop(run_hook, env))
    assert env.ctx_state()["ctx_pending"] is False
    silent(stop(run_hook, env, stop_hook_active=True))


def test_pending_cleared_when_tokens_unknown(env, run_hook):
    env.set_tokens(180_000)
    blocked(stop(run_hook, env))
    env.transcript.write_bytes(b"")  # журнал не читается: tokens is None
    silent(stop(run_hook, env))
    assert env.ctx_state()["ctx_pending"] is False


def test_stop_hook_active_reports_written(env, run_hook):
    env.set_tokens(180_000)
    blocked(stop(run_hook, env))
    env.write_handoff(mtime=time.time() + 100)
    res = stop(run_hook, env, stop_hook_active=True)
    out = json.loads(res.stdout)
    assert "decision" not in out
    assert "передача записана" in out["systemMessage"] and "/clear" in out["systemMessage"]
    assert "фоновые" not in out["systemMessage"]


def test_stop_hook_active_reports_missing(env, run_hook):
    env.set_tokens(180_000)
    blocked(stop(run_hook, env))
    out = json.loads(stop(run_hook, env, stop_hook_active=True).stdout)
    assert "decision" not in out
    assert "не обновлялся после сигнала (180 тыс.)" in out["systemMessage"]


def test_stop_not_updated_writes_no_clear_flag(env, run_hook):
    env.set_tokens(180_000)
    blocked(stop(run_hook, env))
    out = json.loads(stop(run_hook, env, stop_hook_active=True).stdout)
    assert "не обновлялся после сигнала" in out["systemMessage"]
    assert not (env.sessions / "s1.clear.json").exists()


def test_handoff_older_than_signal_reports_missing(env, run_hook):
    env.set_tokens(180_000)
    blocked(stop(run_hook, env))
    env.write_handoff(mtime=time.time() - 1000)  # файл существует, но старше сигнала
    out = json.loads(stop(run_hook, env, stop_hook_active=True).stdout)
    assert "не обновлялся после сигнала (180 тыс.)" in out["systemMessage"]


def test_verify_message_once(env, run_hook):
    env.set_tokens(180_000)
    blocked(stop(run_hook, env))
    assert stop(run_hook, env, stop_hook_active=True).stdout.strip() != ""
    assert env.ctx_state()["ctx_pending"] is False
    silent(stop(run_hook, env, stop_hook_active=True))


def test_more_than_ten_tasks_and_truncation(env, run_hook):
    env.set_tokens(180_000)
    tasks = [{"status": "running", "type": "bash", "description": f"задача{i:02d}"} for i in range(12)]
    r = blocked(stop(run_hook, env, background_tasks=tasks))["reason"]
    assert "задача09" in r and "задача10" not in r and "; ещё 2." in r
    import sys
    from cg_fixtures import SCRIPTS
    sys.path.insert(0, str(SCRIPTS))
    import cg_stop
    big = [{"status": "running", "agent_type": "a" * 5000, "description": "d"} for _ in range(10)]
    assert len(cg_stop.background_summary({"background_tasks": big})) == cg_stop.MAX_BG_CHARS


def test_background_tasks_running_only(env, run_hook):
    env.set_tokens(180_000)
    tasks = [
        {"status": "running", "agent_type": "general-purpose", "description": "прогон тестов " + "я" * 200,
         "command": "SECRET_CMD --token abc"},
        {"status": "completed", "type": "bash", "description": "закончено"},
        {"status": "running", "type": "bash", "description": "сборка"},
    ]
    r = blocked(stop(run_hook, env, background_tasks=tasks))["reason"]
    assert "general-purpose: прогон тестов" in r and "bash: сборка" in r
    assert "закончено" not in r and "SECRET_CMD" not in r and "я" * 121 not in r
    env.write_handoff(mtime=time.time() + 100)
    msg = json.loads(stop(run_hook, env, stop_hook_active=True, background_tasks=tasks).stdout)["systemMessage"]
    assert "Работают фоновые задачи: 2" in msg


def test_background_agents_only_soft_message_and_hint(env, run_hook):
    env.set_tokens(180_000)
    tasks = [{"status": "running", "agent_type": "general-purpose", "description": "ревью"}]
    r = blocked(stop(run_hook, env, background_tasks=tasks))["reason"]
    assert "Запиши в передачу, чего ждёшь от каждой." in r
    env.write_handoff(mtime=time.time() + 100)
    msg = json.loads(stop(run_hook, env, stop_hook_active=True, background_tasks=tasks).stdout)["systemMessage"]
    assert msg.endswith("Работают фоновые агенты: 1 — их отчёты дойдут после /clear.")


def test_block_without_background_tasks_has_no_hint(env, run_hook):
    env.set_tokens(180_000)
    assert "чего ждёшь от каждой" not in blocked(stop(run_hook, env, background_tasks=[]))["reason"]


def test_background_tasks_absent_unknown(env, run_hook):
    env.set_tokens(180_000)
    assert "Фоновые задачи: неизвестно." in blocked(stop(run_hook, env))["reason"]
    env.set_tokens(300_000)
    assert "Фоновые задачи: нет." in blocked(stop(run_hook, env, background_tasks=[]))["reason"]


def test_cyrillic_paths(tmp_path, run_hook, env):
    from cg_fixtures import Env
    cyr = Env(tmp_path / "кириллица", project_name="C--Проекты-пример")
    env.data = cyr.data  # run_hook берёт каталог данных из env
    cyr.set_tokens(180_000)
    out = blocked(run_hook("Stop", cyr.payload(cwd="C:/Проекты/пример")))
    assert "180 тыс." in out["reason"]
    assert cyr.ctx_state()["ctx_last_trigger"] == 180_000


def test_stop_blocks_on_new_cycle_despite_old_midturn_handoff(env, run_hook):
    """Сигнал внутри хода был и передача записана, но контекст вырос на шаг — новый цикл, Stop блокирует."""
    env.write_handoff(mtime=time.time() + 100)
    env.write_mid_state(180_000, time.time())
    env.set_tokens(230_000)  # >= mid_last + step
    assert "230 тыс." in blocked(stop(run_hook, env))["reason"]
    assert env.ctx_state()["ctx_pending"] is True


def test_stop_skips_block_after_midturn_handoff(env, run_hook):
    """Передача записана после сигнала внутри хода, рост меньше шага: Stop не блокирует, pending не ставится."""
    env.write_mid_state(180_000, time.time())
    env.write_handoff(mtime=time.time() + 100)
    env.set_tokens(200_000)
    res = stop(run_hook, env)
    assert res.code == 0, res.stderr
    out = json.loads(res.stdout)
    assert "decision" not in out and out["systemMessage"].startswith("⚠ /clear: контекст 200 тыс.")
    st = env.ctx_state()
    assert st["ctx_last_trigger"] == 200_000 and st["ctx_pending"] is False


def test_stop_blocks_if_no_handoff_after_midturn(env, run_hook):
    env.write_mid_state(180_000, time.time())
    env.write_handoff(mtime=time.time() - 1000)
    env.set_tokens(200_000)
    blocked(stop(run_hook, env))


def test_stop_honors_ctx_ignore(env, run_hook):
    """Хук внутри хода сбросил цикл Stop (ctx_ignore == ctx_last_trigger): Stop считает свой last нулевым."""
    env.write_state("ctx", {"ctx_last_trigger": 220_000, "ctx_trigger_at": 1.0, "ctx_pending": False})
    env.write_mid_state(0, 0, ctx_ignore=220_000)
    env.set_tokens(180_000)  # без ignore: 160K < 200K+step — тишина; с ignore — блок
    blocked(stop(run_hook, env))
    assert env.ctx_state()["ctx_last_trigger"] == 180_000


def _midturn_done(env):
    """Сигнал внутри хода был, передача записана после него, рост меньше шага."""
    env.write_mid_state(180_000, time.time())
    env.write_handoff(mtime=time.time() + 100)
    env.set_tokens(200_000)


def test_stop_midturn_handoff_emits_clear_message_once(env, run_hook):
    _midturn_done(env)
    out = json.loads(stop(run_hook, env).stdout)
    assert out == {"systemMessage": f"⚠ /clear: контекст 200 тыс. — передача записана в {env.handoff.name}."}
    silent(stop(run_hook, env))  # без роста - тишина


def test_stop_midturn_message_mentions_background_tasks(env, run_hook):
    _midturn_done(env)
    msg = json.loads(stop(run_hook, env, background_tasks=[{"status": "running"}]).stdout)["systemMessage"]
    assert msg.endswith("Работают фоновые задачи: 1 — дождитесь завершения до /clear.")


def test_stop_writes_clear_flag(env, run_hook):
    _midturn_done(env)
    before = time.time()
    stop(run_hook, env)
    flag = json.loads((env.sessions / "s1.clear.json").read_text(encoding="utf-8"))
    assert flag["tokens"] == 200_000 and flag["handoff"] == env.handoff.name and flag["at"] >= before - 1
    # ветка после блока: флаг пишется и там
    (env.sessions / "s1.clear.json").unlink()
    env.set_tokens(300_000)
    blocked(stop(run_hook, env))
    env.write_handoff(mtime=time.time() + 100)
    msg = json.loads(stop(run_hook, env, stop_hook_active=True).stdout)["systemMessage"]
    assert msg.startswith("⚠ /clear: контекст 300 тыс.")
    assert json.loads((env.sessions / "s1.clear.json").read_text(encoding="utf-8"))["tokens"] == 300_000


@pytest.mark.skipif(sys.platform != "win32", reason="на POSIX os.replace заменяет файл только для чтения")
def test_stop_no_message_when_state_write_fails(env, run_hook):
    _midturn_done(env)
    env.write_state("ctx", {"ctx_last_trigger": 0, "ctx_trigger_at": 0.0, "ctx_pending": False})
    p = env.sessions / "s1.ctx.json"
    os.chmod(p, stat.S_IREAD)
    try:
        silent(stop(run_hook, env))
        assert not (env.sessions / "s1.clear.json").exists()
    finally:
        os.chmod(p, stat.S_IWRITE | stat.S_IREAD)


def test_handoff_instruction_mentions_questions_verbatim(env, run_hook):
    env.set_tokens(180_000)
    reason = blocked(stop(run_hook, env))["reason"]
    assert "Открытые вопросы пользователю — дословно, с вариантами, по одному в строке" in reason
    assert "в отдельный файл рядом с передачей и ссылка на него" in reason


def _recommended(env, run_hook, tokens=180_000):
    """Цикл с рекомендацией: блок, передача записана, повторный Stop выводит «⚠ /clear» (rec_cycle == last)."""
    env.set_tokens(tokens)
    blocked(stop(run_hook, env))
    env.write_handoff(mtime=time.time() + 10)
    out = json.loads(stop(run_hook, env, stop_hook_active=True).stdout)
    assert out["systemMessage"].startswith("⚠ /clear")
    assert env.ctx_state()["rec_cycle"] == tokens


def _human_after(env, *, delay=120, tokens=180_000 + P2_STEP, kind="user"):
    """Журнал с сообщением человека через `delay` секунд (в окне правдоподобия); контекст вырос на шаг П2."""
    now = time.time()
    rec = human_user(now + delay) if kind == "user" else queued_human(now + delay)
    env.set_journal(tokens, rec)
    return now + delay


def _p2_block(res) -> dict:
    assert res.code == 0, res.stderr
    out = json.loads(res.stdout)
    assert out["decision"] == "block"
    assert "после последней записи передачи были сообщения пользователя" in out["reason"]
    assert "шаблон" in out["reason"] and "раздел не нужен" in out["reason"]
    assert "достаточно одного раздела" in out["reason"] and "дословно" in out["reason"]
    assert "systemMessage" not in out and "повтори открытый вопрос" in out["reason"]
    return out


def test_stop_p2_blocks_after_human_message_post_recommendation(env, run_hook):
    _recommended(env, run_hook)
    h = _human_after(env)
    _p2_block(stop(run_hook, env))
    st = env.ctx_state()
    assert st["p2_pending"] is True and st["p2_seen_ts"] == pytest.approx(h, abs=0.01) and st["p2_at"] > 0
    assert st["ctx_pending"] is False and st["rec_cycle"] == 180_000


def test_stop_p2_queued_command_also_blocks(env, run_hook):
    _recommended(env, run_hook)
    _human_after(env, kind="queued")
    _p2_block(stop(run_hook, env))


def test_stop_p2_once_per_message(env, run_hook):
    _recommended(env, run_hook)
    _human_after(env)
    _p2_block(stop(run_hook, env))
    silent(stop(run_hook, env))  # то же сообщение: повторного блока нет
    assert env.ctx_state()["p2_pending"] is False


def test_stop_p2_second_message_blocks_again(env, run_hook):
    wide = {"CLAUDE_PLUGIN_OPTION_REPEAT_STEP_TOKENS": "100000"}  # шаг ctx шире двух шагов П2
    _recommended(env, run_hook)
    _human_after(env, delay=100)
    _p2_block(run_hook("Stop", env.payload(), extra_env=wide))
    env.write_handoff(mtime=time.time() + 20)  # модель обновила передачу
    stop(run_hook, env, stop_hook_active=True)
    now = time.time()
    env.set_journal(180_000 + 2 * P2_STEP, human_user(now + 100), human_user(now + 200, "второе"))
    _p2_block(run_hook("Stop", env.payload(), extra_env=wide))
    assert env.ctx_state()["p2_seen_ts"] == pytest.approx(now + 200, abs=0.01)


def test_stop_p2_silent_without_recommendation(env, run_hook):
    env.set_tokens(180_000)
    blocked(stop(run_hook, env))  # блок был, но рекомендации (rec_cycle) ещё нет
    env.write_handoff(mtime=time.time() + 10)
    _human_after(env)
    silent(stop(run_hook, env))
    # старый файл состояния без новых полей читается с умолчаниями
    env.write_state("ctx", {"ctx_last_trigger": 180_000, "ctx_trigger_at": 1.0, "ctx_pending": False})
    silent(stop(run_hook, env))


def test_stop_p2_silent_below_growth_step(env, run_hook):
    _recommended(env, run_hook)
    h = _human_after(env, tokens=180_000 + P2_STEP - 1)
    silent(stop(run_hook, env))
    st = env.ctx_state()
    assert st["p2_pending"] is False and st["p2_seen_ts"] != pytest.approx(h, abs=0.01)
    env.set_journal(180_000 + P2_STEP, human_user(h))
    _p2_block(stop(run_hook, env))
    assert env.ctx_state()["p2_seen_ts"] == pytest.approx(h, abs=0.01)


def test_stop_p2_no_repeat_after_skipped_write(env, run_hook):
    _recommended(env, run_hook)
    os.utime(env.handoff, (time.time() - 100, time.time() - 100))  # передача записана давно, до блока П2
    _human_after(env, delay=100)
    _p2_block(stop(run_hook, env))
    assert env.ctx_state()["p2_base"] == 180_000 + P2_STEP
    silent(stop(run_hook, env, stop_hook_active=True))  # модель передачу не обновила
    now = time.time()
    env.set_journal(180_000 + P2_STEP + 1000, human_user(now + 100), human_user(now + 200, "второе"))
    silent(stop(run_hook, env))  # рост с момента проверки меньше шага


def test_stop_p2_base_after_update(env, run_hook):
    _recommended(env, run_hook)
    assert env.ctx_state()["p2_base"] == 180_000  # база после записи по ctx-блоку
    _human_after(env)
    _p2_block(stop(run_hook, env))
    env.write_handoff(mtime=time.time() + 30)
    stop(run_hook, env, stop_hook_active=True)
    st = env.ctx_state()
    assert st["p2_base"] == 180_000 + P2_STEP and st["p2_pending"] is False


def test_stop_old_state_without_p2_base(env, run_hook):
    _recommended(env, run_hook)
    env.write_state("ctx", {"ctx_last_trigger": 180_000, "ctx_trigger_at": 1.0, "ctx_pending": False,
                            "rec_cycle": 180_000})
    _human_after(env, tokens=180_000 + P2_STEP - 1)
    silent(stop(run_hook, env))  # база = rec_cycle, роста на шаг нет
    _human_after(env)
    _p2_block(stop(run_hook, env))


def test_reasons_ask_to_repeat_open_question(env, run_hook):
    import cg_handoff
    import cg_stop
    env.set_tokens(180_000)
    assert "повтори открытый вопрос" in blocked(stop(run_hook, env))["reason"]
    reason = json.loads(cg_stop.build_p2_block(cg_handoff.Target(Path("p.md"), "t", True, True)))["reason"]
    assert "повтори открытый вопрос" in reason and reason.endswith("Затем завершай ход.")


def test_stop_p2_silent_below_threshold(env, run_hook):
    _recommended(env, run_hook)
    _human_after(env, tokens=169_000)
    silent(stop(run_hook, env))
    assert env.ctx_state()["p2_pending"] is False


def test_stop_p2_report_updated(env, run_hook):
    _recommended(env, run_hook)
    _human_after(env)
    _p2_block(stop(run_hook, env))
    (env.sessions / "s1.clear.json").unlink()
    before = time.time()
    env.write_handoff(mtime=time.time() + 30)
    msg = json.loads(stop(run_hook, env, stop_hook_active=True,
                          background_tasks=[{"status": "running"}]).stdout)["systemMessage"]
    assert msg.startswith("⚠ /clear: контекст 205 тыс. — передача обновлена в ") and env.handoff.name in msg
    assert "Работают фоновые задачи: 1" in msg
    flag = json.loads((env.sessions / "s1.clear.json").read_text(encoding="utf-8"))
    assert flag["tokens"] == 205_000 and flag["at"] >= before - 1
    assert env.ctx_state()["p2_pending"] is False
    silent(stop(run_hook, env, stop_hook_active=True))


def test_stop_p2_report_silent_when_not_written(env, run_hook):
    _recommended(env, run_hook)
    os.utime(env.handoff, (time.time() - 100, time.time() - 100))  # передача записана давно, до блока П2
    _human_after(env)
    _p2_block(stop(run_hook, env))
    (env.sessions / "s1.clear.json").unlink()
    silent(stop(run_hook, env, stop_hook_active=True))  # передача не менялась после блока
    assert not (env.sessions / "s1.clear.json").exists()
    assert env.ctx_state()["p2_pending"] is False


def test_stop_p2_absorbed_by_ctx_block(env, run_hook):
    _recommended(env, run_hook)
    now = time.time()
    env.set_journal(230_000, human_user(now + 100))  # рост на шаг: сработает ctx-блок, а не П2
    out = blocked(stop(run_hook, env))
    assert "контекст сессии 230 тыс." in out["reason"]
    st = env.ctx_state()
    assert st["p2_seen_ts"] == pytest.approx(now + 100, abs=0.01) and st["p2_pending"] is False
    assert st["ctx_pending"] is True
    # сообщение уже учтено блоком: после рекомендации П2 по нему не срабатывает
    env.write_handoff(mtime=time.time() + 10)
    stop(run_hook, env, stop_hook_active=True)
    os.utime(env.handoff, (now + 10, now + 10))
    silent(stop(run_hook, env))


@pytest.mark.skipif(sys.platform != "win32", reason="на POSIX os.replace заменяет файл только для чтения")
def test_stop_p2_state_write_failure_no_block(env, run_hook):
    _recommended(env, run_hook)
    _human_after(env)
    p = env.sessions / "s1.ctx.json"
    os.chmod(p, stat.S_IREAD)
    try:
        silent(stop(run_hook, env))
    finally:
        os.chmod(p, stat.S_IWRITE | stat.S_IREAD)
    assert env.ctx_state()["p2_pending"] is False


def test_handoff_instruction_heading_time_and_open_items():
    import datetime
    import cg_handoff
    now = datetime.datetime(2026, 10, 15, 17, 53)
    own = cg_handoff.Target(Path("h.md"), "t", False, False)
    ext = cg_handoff.Target(Path("p.md"), "t", True, True)
    text = cg_handoff.handoff_instruction(own, now)
    assert "Раздел `## 2026-10-15 17:53` (время сигнала подставлено плагином)" in text
    assert "Открытые пункты прошлого раздела переносятся в новый или явно закрываются" in text
    ext_text = cg_handoff.handoff_instruction(ext, now)
    assert "Время сигнала: 2026-10-15 17:53." in ext_text and "## " not in ext_text
    assert "Открытые пункты прошлого раздела" in ext_text


def test_p2_block_has_no_heading_placeholder():
    import cg_handoff
    import cg_stop
    reason = json.loads(cg_stop.build_p2_block(cg_handoff.Target(Path("p.md"), "t", True, True)))["reason"]
    assert "<дата время>" not in reason and "## " not in reason
