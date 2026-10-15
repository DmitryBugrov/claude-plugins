"""Тесты PostToolUse (cg_posttool.py через run.py): мягкий сигнал о пороге контекста внутри хода."""
from __future__ import annotations

import json
import os
import stat
import subprocess
import sys
import time

import pytest

from cg_fixtures import RUN_PY, SCRIPTS, iso_z


def emitted(res) -> str:
    """Ожидает additionalContext PostToolUse с кодом 0 и возвращает его текст."""
    assert res.code == 0, res.stderr
    out = json.loads(res.stdout)
    assert out["hookSpecificOutput"]["hookEventName"] == "PostToolUse"
    assert set(out) == {"hookSpecificOutput"}
    return out["hookSpecificOutput"]["additionalContext"]


def silent(res) -> None:
    """Ожидает код 0 и пустой stdout."""
    assert res.code == 0, res.stderr
    assert res.stdout.strip() == ""


def tool(run_hook, env, **extra):
    """Запуск PostToolUse для сессии s1."""
    return run_hook("PostToolUse", env.tool_payload(**extra))


def test_below_threshold_silent(env, run_hook):
    env.set_tokens(149_999)
    silent(tool(run_hook, env))
    assert env.mid_state() is None


def test_crossing_emits_context_once(env, run_hook):
    env.set_tokens(180_000)
    text = emitted(tool(run_hook, env))
    assert "180 тыс." in text and "порог 170 тыс." in text and "ничего не изменилось" in text
    assert env.handoff.as_posix() in text and "/clear" in text and "достаточно одного раздела" in text
    st = env.mid_state()
    assert st["mid_last_trigger"] == 180_000 and st["mid_trigger_at"] > 0
    silent(tool(run_hook, env))  # тот же размер: повторного сигнала нет
    assert not list(env.sessions.glob("*.lock"))


def test_repeat_needs_step(env, run_hook):
    env.set_tokens(180_000)
    emitted(tool(run_hook, env))
    env.set_tokens(209_999)
    silent(tool(run_hook, env))
    env.set_tokens(230_000)
    emitted(tool(run_hook, env))
    assert env.mid_state()["mid_last_trigger"] == 230_000


def test_small_drop_no_reset(env, run_hook):
    env.set_tokens(220_000)
    emitted(tool(run_hook, env))
    env.set_tokens(196_789)  # падение < шага и выше порога: цикл не сбрасывается
    silent(tool(run_hook, env))
    assert env.mid_state()["mid_last_trigger"] == 220_000


def test_drop_resets_cycle(env, run_hook):
    env.set_tokens(220_000)
    emitted(tool(run_hook, env))
    env.set_tokens(30_000)  # ниже порога: сброс без вывода
    silent(tool(run_hook, env))
    assert env.mid_state()["mid_last_trigger"] == 0
    env.set_tokens(171_000)  # меньше last+step, но цикл обнулён
    emitted(tool(run_hook, env))


def test_no_midturn_signal_after_stop_block_within_step(env, run_hook):
    env.set_tokens(180_000)
    run_hook("Stop", env.payload())  # Stop заблокировал: ctx_last_trigger = 160K
    assert env.ctx_state()["ctx_last_trigger"] == 180_000
    env.set_tokens(200_000)
    silent(tool(run_hook, env))
    env.set_tokens(230_000)
    emitted(tool(run_hook, env))


def test_subagent_call_ignored(env, run_hook):
    """subagent_ignored: вызов с agent_id (саб-агент) не обрабатывается."""
    env.set_tokens(400_000)
    silent(tool(run_hook, env, agent_id="a1", agent_type="Explore"))
    assert env.mid_state() is None


def test_agent_type_without_agent_id_processed(env, run_hook):
    env.set_tokens(400_000)
    emitted(tool(run_hook, env, agent_type="Explore"))


def test_missing_or_bad_session_id_silent(env, run_hook):
    env.set_tokens(400_000)
    silent(run_hook("PostToolUse", env.tool_payload(sid=None)))
    silent(run_hook("PostToolUse", env.tool_payload(sid="../x")))
    assert not env.sessions.exists() or not list(env.sessions.glob("*.json"))


def test_tokens_unknown_silent(env, run_hook):
    silent(tool(run_hook, env))  # журнала нет
    env.transcript.write_bytes(b"")
    silent(tool(run_hook, env))
    assert env.mid_state() is None


def test_exception_silent_exit0(env, run_hook):
    env.set_tokens(400_000)
    silent(run_hook("PostToolUse", None, raw=b"[]"))
    silent(run_hook("PostToolUse", None, raw=b"\xff\xfe not json"))
    silent(tool(run_hook, env, transcript_path=["x"]))


@pytest.mark.skipif(sys.platform != "win32", reason="на POSIX os.replace заменяет файл только для чтения")
def test_save_failure_silent(env, run_hook):
    """Сбой именно ЗАПИСИ состояния (К7): файл mid.json валиден, но только для чтения — os.replace падает.

    Сначала сохранение, потом вывод: при сбое записи сигнала нет, состояние не изменено, lock снят.
    """
    env.set_tokens(400_000)
    env.write_mid_state(0, 0)
    p = env.sessions / "s1.mid.json"
    os.chmod(p, stat.S_IREAD)
    try:
        silent(tool(run_hook, env))
        assert not list(env.sessions.glob("*.lock"))  # lock снят
        assert not list(env.sessions.glob("*.tmp"))  # временный файл убран
    finally:
        os.chmod(p, stat.S_IWRITE | stat.S_IREAD)
    assert env.mid_state()["mid_last_trigger"] == 0


def test_surrogate_in_payload_ascii_output(env, run_hook):
    env.set_tokens(400_000)
    text = json.dumps(env.tool_payload(tool_input={"x": "XSUR"})).replace("XSUR", "\\ud800")
    res = run_hook("PostToolUse", None, raw=text.encode("utf-8"))
    emitted(res)
    res.stdout.encode("ascii")


def test_unreadable_ctx_state_silent(env, run_hook):
    """Ошибка чтения (не «нет файла») ctx.json — тишина, а не нули с ложным сигналом."""
    env.set_tokens(400_000)
    (env.sessions / "s1.ctx.json").mkdir(parents=True)
    silent(tool(run_hook, env))
    assert env.mid_state() is None


def test_no_git_called(env, run_hook, tmp_path):
    """Ни один путь хука не запускает git: GIT_TRACE=<файл> — любой запуск git оставил бы файл-след."""
    env.set_tokens(400_000)
    trace = tmp_path / "git_trace.txt"
    emitted(run_hook("PostToolUse", env.tool_payload(cwd=str(tmp_path)), extra_env={"GIT_TRACE": str(trace)}))
    run_hook("Stop", env.payload(), extra_env={"GIT_TRACE": str(trace)})
    run_hook("SessionStart", env.payload("SessionStart", source="startup"), extra_env={"GIT_TRACE": str(trace)})
    assert not trace.exists(), "хук вызвал git"


def test_git_trace_detects_git_call(tmp_path):
    """Контроль самого приёма: настоящий вызов git при GIT_TRACE оставляет файл-след."""
    trace = tmp_path / "t.txt"
    subprocess.run(["git", "--version"], check=True, capture_output=True, env={**os.environ, "GIT_TRACE": str(trace)})
    subprocess.run(["git", "rev-parse", "--git-dir"], cwd=str(tmp_path), capture_output=True,
                   env={**os.environ, "GIT_TRACE": str(trace)})
    assert trace.exists()


def test_two_processes_exactly_one_output(env):
    """Два процесса с одинаковым payload: ровно один вывод. Повторы с новой сессией; без lock тест падает."""
    env.set_tokens(400_000)
    e = {k: v for k, v in os.environ.items() if not k.startswith("CLAUDE_PLUGIN_")}
    e["CLAUDE_PLUGIN_DATA"] = str(env.data)
    for i in range(8):
        data = json.dumps(env.tool_payload(sid=f"race{i}")).encode("utf-8")
        procs = [subprocess.Popen([sys.executable, str(RUN_PY), "PostToolUse"], stdin=subprocess.PIPE,
                                  stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=e) for _ in range(2)]
        for p in procs:  # stdin обоим до чтения stdout: процессы стартуют максимально одновременно
            p.stdin.write(data)
            p.stdin.close()
        outs = [p.stdout.read() for p in procs]
        for p in procs:
            p.wait(timeout=60)
            p.stdout.close()
            p.stderr.close()
        assert [p.returncode for p in procs] == [0, 0]
        assert sum(1 for o in outs if o.strip()) == 1, f"повтор {i}: выводов не один"


def test_stale_lock_removed_and_fresh_lock_blocks(env, run_hook):
    env.set_tokens(400_000)
    env.sessions.mkdir(parents=True)
    lock = env.sessions / "s1.mid.lock"
    lock.write_text("", encoding="utf-8")
    silent(tool(run_hook, env))  # свежий lock: занято
    assert env.mid_state() is None
    old = time.time() - 120
    os.utime(lock, (old, old))
    emitted(tool(run_hook, env))  # устаревший lock снят
    assert not lock.exists()


def test_stop_skips_block_after_midturn_handoff(env, run_hook):
    env.set_tokens(180_000)
    emitted(tool(run_hook, env))
    env.write_handoff(mtime=time.time() + 100)  # передача записана после сигнала
    env.set_tokens(190_000)
    res = run_hook("Stop", env.payload())
    assert res.code == 0, res.stderr
    out = json.loads(res.stdout)
    assert "decision" not in out and out["systemMessage"].startswith("⚠ /clear: контекст 190 тыс.")
    st = env.ctx_state()
    assert st["ctx_last_trigger"] == 190_000 and st["ctx_pending"] is False
    silent(run_hook("Stop", env.payload(stop_hook_active=True)))  # проверки результата нет


def test_stop_blocks_if_no_handoff_after_midturn(env, run_hook):
    env.set_tokens(180_000)
    emitted(tool(run_hook, env))
    env.write_handoff(mtime=time.time() - 1000)  # файл передачи старше сигнала
    out = json.loads(run_hook("Stop", env.payload()).stdout)
    assert out["decision"] == "block" and "180 тыс." in out["reason"]
    assert env.ctx_state()["ctx_pending"] is True


def test_stop_does_not_write_mid_file(env, run_hook):
    env.set_tokens(180_000)
    env.write_mid_state(180_000, time.time())
    before = (env.sessions / "s1.mid.json").read_bytes()
    run_hook("Stop", env.payload())
    assert (env.sessions / "s1.mid.json").read_bytes() == before


def test_new_cycle_after_compaction_within_turn(env, run_hook):
    """Stop заблокировал на 170K; сжатие внутри хода, затем рост выше порога — сигнал нового цикла приходит."""
    env.set_tokens(190_000)
    run_hook("Stop", env.payload())  # блок Stop: ctx_last = 170K
    assert env.ctx_state()["ctx_last_trigger"] == 190_000
    env.set_tokens(40_000)  # сжатие внутри следующего хода
    silent(tool(run_hook, env))
    assert env.mid_state()["ctx_ignore"] == 190_000
    env.set_tokens(175_000)  # новый цикл: рост выше порога в том же ходе
    emitted(tool(run_hook, env))
    assert env.mid_state()["mid_last_trigger"] == 175_000


def test_handoff_check_error_keeps_block(env):
    """Сбой проверки передачи (midturn_handoff_done бросает исключение) не снимает блок по контексту."""
    env.set_tokens(180_000)
    env.write_mid_state(180_000, time.time())
    env.write_handoff(mtime=time.time() + 100)
    code = ("import sys; sys.path.insert(0, sys.argv[1]); import cg_stop as g\n"
            "def boom(*a, **k): raise OSError('stat failed')\n"
            "g.midturn_handoff_done = boom\n"
            "g.main()")
    e = {k: v for k, v in os.environ.items() if not k.startswith("CLAUDE_PLUGIN_")}
    e["CLAUDE_PLUGIN_DATA"] = str(env.data)
    proc = subprocess.run([sys.executable, "-c", code, str(SCRIPTS)], input=json.dumps(env.payload()).encode("utf-8"),
                          capture_output=True, env=e, timeout=60)
    assert proc.returncode == 0, proc.stderr
    out = json.loads(proc.stdout.decode("utf-8"))
    assert out["decision"] == "block" and "180 тыс." in out["reason"]


def _step_with_ts(env, tokens=180_000, age=2.0):
    """Журнал, где последний ответ записан `age` секунд назад; возвращает его epoch-время."""
    ts = time.time() - age
    env.set_tokens(tokens, timestamp=iso_z(ts))
    return ts


def test_posttool_silent_when_handoff_written_by_same_step(env, run_hook):
    ts = _step_with_ts(env)
    env.write_handoff(mtime=ts + 1)  # передача записана тем же шагом (mtime >= ts)
    silent(tool(run_hook, env))
    mid = env.mid_state()
    assert mid["mid_last_trigger"] == 180_000 and mid["mid_trigger_at"] < ts + 1
    res = run_hook("Stop", env.payload())  # Stop засчитывает передачу: без блока, но с рекомендацией
    assert res.code == 0, res.stderr
    out = json.loads(res.stdout)
    assert "decision" not in out and out["systemMessage"].startswith("⚠ /clear")


def test_posttool_signals_when_handoff_older_than_step(env, run_hook):
    ts = _step_with_ts(env)
    env.write_handoff(mtime=ts - 100)
    emitted(tool(run_hook, env))
    assert env.mid_state()["mid_trigger_at"] > ts


def test_posttool_suppression_keeps_ctx_ignore(env, run_hook):
    """Подавление сигнала сохраняет сброс цикла Stop (ctx_ignore), записанный решением _decide."""
    env.write_state("ctx", {"ctx_last_trigger": 270_000, "ctx_trigger_at": 1.0, "ctx_pending": False})
    env.write_mid_state(0, 0)
    ts = _step_with_ts(env)
    env.write_handoff(mtime=ts + 1)
    silent(tool(run_hook, env))
    mid = env.mid_state()
    assert mid["ctx_ignore"] == 270_000 and mid["mid_last_trigger"] == 180_000
