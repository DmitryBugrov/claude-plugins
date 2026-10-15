"""Тесты контракта плагина: поставщик передачи, каталог данных, настройки userConfig, запускающий файл."""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys

import pytest

from cg_fixtures import RUN_PY, SCRIPTS, Env


def block(res) -> dict:
    """Ожидает блокировку Stop JSON-ом с кодом 0."""
    assert res.code == 0, res.stderr
    out = json.loads(res.stdout)
    assert out["decision"] == "block"
    return out


def silent(res) -> None:
    """Ожидает код 0 и пустой stdout."""
    assert res.code == 0, res.stderr
    assert res.stdout.strip() == ""


def test_descriptor_provider_target(env, run_hook):
    """Дескриптор: передача в handoff_path, шаблон из дескриптора; и Stop, и PostToolUse."""
    art = env.root / "art" / "handoff.md"
    env.write_descriptor(art, template="ШАБЛОН-ПОСТАВЩИКА")
    env.set_tokens(180_000)
    tool = run_hook("PostToolUse", env.tool_payload())
    text = json.loads(tool.stdout)["hookSpecificOutput"]["additionalContext"]
    assert art.as_posix() in text and "ШАБЛОН-ПОСТАВЩИКА" in text and "добавляется в конец файла" not in text
    stop = block(run_hook("Stop", env.payload()))
    assert art.as_posix() in stop["reason"] and "ШАБЛОН-ПОСТАВЩИКА" in stop["reason"]
    assert not env.handoff.exists()


def test_descriptor_provider_handoff_done_by_provider_file(env, run_hook):
    """Проверка «передача записана» идёт по файлу из дескриптора."""
    import time
    art = env.root / "art" / "handoff.md"
    env.write_descriptor(art)
    env.set_tokens(180_000)
    block(run_hook("Stop", env.payload()))
    env.write_handoff(art, mtime=time.time() + 100)
    out = json.loads(run_hook("Stop", env.payload(stop_hook_active=True)).stdout)
    assert "передача записана в handoff.md" in out["systemMessage"]


def test_no_descriptor_own_handoff_and_load(env, run_hook):
    """Без дескриптора: встроенный шаблон и собственный файл в каталоге данных плагина."""
    env.set_tokens(180_000)
    reason = block(run_hook("Stop", env.payload()))["reason"]
    assert env.handoff.as_posix() in reason and "открытые вопросы, следующие шаги" in reason
    assert "добавляется в конец файла" in reason and "40 строк" in reason
    assert env.data in env.handoff.parents


def test_unknown_schema_silent(env, run_hook):
    env.set_tokens(400_000)
    for fields in ({"schema": 2}, {"schema": "1"}, {"schema": True}, {"handoff_path": ""}):
        env.write_descriptor(**fields)
        silent(run_hook("Stop", env.payload()))
        silent(run_hook("PostToolUse", env.tool_payload()))
    (env.logdir / "handoff-provider.json").write_text("{битый", encoding="utf-8")
    silent(run_hook("Stop", env.payload()))
    silent(run_hook("PostToolUse", env.tool_payload()))
    assert not env.sessions.exists() or not list(env.sessions.glob("*.json"))


def test_no_plugin_data_silent(env, run_hook):
    env.set_tokens(400_000)
    silent(run_hook("Stop", env.payload(), data=False))
    silent(run_hook("PostToolUse", env.tool_payload(), data=False))
    assert not env.data.exists()


def test_options_absent_defaults(env, run_hook):
    """Переменных CLAUDE_PLUGIN_OPTION_* нет: порог 170 000, шаг 50 000."""
    env.set_tokens(149_999)
    silent(run_hook("Stop", env.payload()))
    env.set_tokens(170_000)
    assert "порог 170 тыс." in block(run_hook("Stop", env.payload()))["reason"]
    env.set_tokens(199_999)
    silent(run_hook("Stop", env.payload()))
    env.set_tokens(220_000)
    block(run_hook("Stop", env.payload()))


def test_options_applied(env, run_hook):
    opts = {"CLAUDE_PLUGIN_OPTION_THRESHOLD_TOKENS": "20000", "CLAUDE_PLUGIN_OPTION_REPEAT_STEP_TOKENS": "5000"}
    env.set_tokens(19_999)
    silent(run_hook("Stop", env.payload(), extra_env=opts))
    env.set_tokens(20_000)
    assert "порог 20 тыс." in block(run_hook("Stop", env.payload(), extra_env=opts))["reason"]
    env.set_tokens(24_999)
    silent(run_hook("Stop", env.payload(), extra_env=opts))
    env.set_tokens(25_000)
    block(run_hook("Stop", env.payload(), extra_env=opts))
    env.set_tokens(30_000)
    text = run_hook("PostToolUse", env.tool_payload(), extra_env=opts).stdout
    assert "порог 20 тыс." in json.loads(text)["hookSpecificOutput"]["additionalContext"]


def test_options_invalid_defaults(env, run_hook):
    """Некорректные значения (текст, ноль, минус, nan, inf, дробь, пусто) — умолчания молча."""
    for n, bad in enumerate(("abc", "0", "-5", "nan", "inf", "1.5", "", "true")):
        e = Env(env.root / f"v{n}")
        opts = {"CLAUDE_PLUGIN_OPTION_THRESHOLD_TOKENS": bad, "CLAUDE_PLUGIN_OPTION_REPEAT_STEP_TOKENS": bad}
        env.data = e.data  # весь цикл вызовов — в одном каталоге данных
        e.set_tokens(149_999)
        silent(run_hook("Stop", e.payload(), extra_env=opts))
        e.set_tokens(170_000)
        assert "порог 170 тыс." in block(run_hook("Stop", e.payload(), extra_env=opts))["reason"], bad


def test_python_version_gate(env):
    """На Python старше 3.9 запускающий файл молча выходит; проверка на подмене sys.version_info."""
    code = ("import sys, runpy\n"
            "sys.version_info = (3, 8, 0)\n"
            "sys.argv = [sys.argv[1], 'Stop']\n"
            "runpy.run_path(sys.argv[0], run_name='__main__')")
    env.set_tokens(400_000)
    e = {k: v for k, v in os.environ.items() if not k.startswith("CLAUDE_PLUGIN_")}
    e["CLAUDE_PLUGIN_DATA"] = str(env.data)
    proc = subprocess.run([sys.executable, "-c", code, str(RUN_PY)], input=json.dumps(env.payload()).encode("utf-8"),
                          capture_output=True, env=e, timeout=60)
    assert proc.returncode == 0 and proc.stdout.strip() == b"", proc.stderr
    assert not env.sessions.exists()
    # контроль: без подмены тот же запуск блокирует
    ok = subprocess.run([sys.executable, str(RUN_PY), "Stop"], input=json.dumps(env.payload()).encode("utf-8"),
                        capture_output=True, env=e, timeout=60)
    assert json.loads(ok.stdout)["decision"] == "block"


def test_run_py_unknown_or_missing_event_silent(env, run_hook):
    env.set_tokens(400_000)
    silent(run_hook("Bogus", env.payload()))
    proc = subprocess.run([sys.executable, str(RUN_PY)], input=b"{}", capture_output=True, timeout=60)
    assert proc.returncode == 0 and proc.stdout == b""


def test_run_py_has_no_new_syntax():
    """run.py компилируется как Python 3.8: без match, `X | Y` в аннотациях, скобочных менеджеров контекста."""
    import ast
    src = RUN_PY.read_text(encoding="utf-8")
    ast.parse(src, feature_version=(3, 8))


def test_parallel_sessions_own_handoff(env, run_hook):
    """Две сессии без дескриптора одного проекта: общий файл передачи, раздельное состояние."""
    env.set_tokens(180_000)
    r1 = block(run_hook("Stop", env.payload(sid="sA")))
    r2 = block(run_hook("Stop", env.payload(sid="sB")))
    assert env.handoff.as_posix() in r1["reason"] and env.handoff.as_posix() in r2["reason"]
    assert env.ctx_state("sA")["ctx_pending"] and env.ctx_state("sB")["ctx_pending"]
    env.write_handoff(text="## A\nтекст-A\n")
    env.handoff.write_text(env.handoff.read_text(encoding="utf-8") + "\n## B\nтекст-B\n", encoding="utf-8")
    res = run_hook("SessionStart", env.payload("SessionStart", sid="sC", source="startup"))
    assert "текст-B" in res.stdout and "текст-A" not in res.stdout


def test_descriptor_without_provider_silent(env, run_hook):
    """Дескриптор без provider (или с пустым/нестроковым provider) — неизвестный: тишина."""
    env.set_tokens(400_000)
    for provider in (None, "", "  ", 5):
        env.write_descriptor(provider=provider)
        if provider is None:
            d = json.loads((env.logdir / "handoff-provider.json").read_text(encoding="utf-8"))
            del d["provider"]
            (env.logdir / "handoff-provider.json").write_text(json.dumps(d), encoding="utf-8")
        silent(run_hook("Stop", env.payload()))
        silent(run_hook("PostToolUse", env.tool_payload()))
    assert not env.sessions.exists() or not list(env.sessions.glob("*.json"))


def test_descriptor_relative_handoff_path_silent(env, run_hook):
    """Относительный handoff_path не принимается (разрешался бы от cwd хука): тишина."""
    env.set_tokens(400_000)
    for hp in ("handoff.md", "art/handoff.md", "./handoff.md"):
        env.write_descriptor(handoff_path=hp)
        silent(run_hook("Stop", env.payload()))
        silent(run_hook("PostToolUse", env.tool_payload()))
    assert not env.sessions.exists() or not list(env.sessions.glob("*.json"))


@pytest.mark.skipif(sys.platform != "win32", reason="обратные слэши — абсолютный путь только на Windows")
def test_descriptor_backslash_handoff_path(env, run_hook):
    """handoff_path с обратными слэшами в дескрипторе работает: адресат — этот файл."""
    env.set_tokens(400_000)
    hp = str(env.root / "art" / "handoff.md")
    assert "\\" in hp
    d = {"schema": 1, "provider": "example", "handoff_path": hp, "template": "шаблон-поставщика", "loads_on_start": True}
    (env.logdir / "handoff-provider.json").write_text(json.dumps(d), encoding="utf-8")
    reason = block(run_hook("Stop", env.payload()))["reason"]
    assert (env.root / "art" / "handoff.md").as_posix() in reason and "шаблон-поставщика" in reason


def test_run_py_is_ascii():
    """run.py декодируется как ascii: шлюз версии должен компилироваться под любым Python."""
    RUN_PY.read_bytes().decode("ascii")


@pytest.mark.parametrize("missing", ["cg_transcript.py", "cg_common.py"])
@pytest.mark.parametrize("event", ["Stop", "PostToolUse", "SessionStart"])
def test_missing_module_silent(env, tmp_path, event, missing):
    """Копия scripts/ без одного модуля: сбой импорта в run.py -> код 0 и пустой stdout."""
    copy = tmp_path / "scripts_copy"
    shutil.copytree(SCRIPTS, copy, ignore=shutil.ignore_patterns("__pycache__"))
    (copy / missing).unlink()
    env.set_tokens(400_000)
    e = {k: v for k, v in os.environ.items() if not k.startswith("CLAUDE_PLUGIN_")}
    e["CLAUDE_PLUGIN_DATA"] = str(env.data)
    proc = subprocess.run([sys.executable, str(copy / "run.py"), event],
                          input=json.dumps(env.payload(event)).encode("utf-8"),
                          capture_output=True, env=e, timeout=60)
    assert proc.returncode == 0 and proc.stdout == b"", proc.stderr
