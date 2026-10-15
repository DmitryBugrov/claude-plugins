"""Тесты SessionStart: строка после сжатия, загрузка передачи без дескриптора, чистка sessions/."""
from __future__ import annotations

import os
import time

COMPACT = "Контекст сжат. Решения после последнего раздела передачи могли не попасть в неё"


def start(run_hook, env, **extra):
    """Запуск SessionStart; возвращает stdout."""
    res = run_hook("SessionStart", env.payload("SessionStart", **extra))
    assert res.code == 0, res.stderr
    return res.stdout


def test_compact_line(env, run_hook):
    assert COMPACT in start(run_hook, env, source="compact")


def test_no_compact_line_on_other_sources(env, run_hook):
    for source in ("startup", "resume", "clear"):
        assert COMPACT not in start(run_hook, env, source=source)
    assert COMPACT not in start(run_hook, env)


def test_no_descriptor_own_handoff_and_load(env, run_hook):
    """Без дескриптора: последний раздел собственного файла загружается при старте, ≤ 40 строк."""
    text = ("## 2026-10-14 10:00\nстарый раздел\n\n## 2026-10-15 12:00\n"
            + "".join(f"строка-{i}\n" for i in range(1, 60)))
    env.write_handoff(text=text)
    for source in ("startup", "resume", "clear", "compact"):
        out = start(run_hook, env, source=source)
        assert "Последняя передача сессии" in out and env.handoff.as_posix() in out
        assert "старый раздел" not in out and "## 2026-10-15 12:00" in out
        assert "строка-39" in out and "строка-40" not in out  # заголовок + 39 строк = 40


def test_no_handoff_file_no_load(env, run_hook):
    assert start(run_hook, env, source="startup").strip() == ""


def test_descriptor_loads_on_start_true_not_loaded(env, run_hook):
    """Дескриптор внешнего поставщика с loads_on_start: true — плагин раздел не загружает, строку сжатия выводит."""
    art = env.root / "art" / "handoff.md"
    env.write_handoff(art, text="## Раздел поставщика\nсекрет-поставщика\n")
    env.write_descriptor(art)
    out = start(run_hook, env, source="compact")
    assert "секрет-поставщика" not in out and COMPACT in out
    assert start(run_hook, env, source="startup").strip() == ""


def test_descriptor_loads_on_start_false_loaded(env, run_hook):
    art = env.root / "art" / "handoff.md"
    env.write_handoff(art, text="## Раздел X\nтекст-поставщика\n")
    env.write_descriptor(art, loads_on_start=False)
    assert "текст-поставщика" in start(run_hook, env, source="startup")


def test_unknown_schema_silent(env, run_hook):
    env.write_handoff(text="## Раздел\nсекрет\n")
    env.write_descriptor(schema=2)
    assert start(run_hook, env, source="compact").strip() == ""


def test_cleanup_old_sessions_keeps_current_and_handoff(env, run_hook):
    env.write_handoff()
    env.sessions.mkdir(parents=True)
    old = time.time() - 8 * 24 * 3600
    names = ["old.ctx.json", "old.mid.json", "old.mid.lock", "cur.ctx.json", "cur.mid.json", "new.ctx.json"]
    for n in names:
        (env.sessions / n).write_text("{}", encoding="utf-8")
    for n in ("old.ctx.json", "old.mid.json", "old.mid.lock", "cur.ctx.json", "cur.mid.json"):
        os.utime(env.sessions / n, (old, old))
    os.utime(env.handoff, (old, old))
    run_hook("SessionStart", env.payload("SessionStart", sid="cur", source="startup"))
    left = sorted(p.name for p in env.sessions.iterdir())
    assert left == ["cur.ctx.json", "cur.mid.json", "new.ctx.json"]
    assert env.handoff.exists()


def test_no_plugin_data_silent(env, run_hook):
    env.write_handoff()
    res = run_hook("SessionStart", env.payload("SessionStart", source="compact"), data=False)
    assert res.code == 0 and res.stdout.strip() == ""


def test_cyrillic_handoff_text_roundtrip(env, run_hook):
    env.write_handoff(text="## Раздел\nрешение: выбрать вариант «Б» — кириллица ё\n")
    assert "вариант «Б» — кириллица ё" in start(run_hook, env, source="clear")


WARNING = "Внимание: порог контекста {n} выше 180000 — близко к автосжатию."


def test_warning_above_max(env, run_hook):
    """Порог выше 180000 — предупреждение при любом источнике и независимо от дескриптора."""
    n = 200_000
    for source in ("startup", "resume", "clear", "compact"):
        for descriptor in (None, {}, {"schema": 2}):
            if descriptor is not None:
                env.write_descriptor(**descriptor)
            res = run_hook("SessionStart", env.payload("SessionStart", source=source),
                           extra_env={"CLAUDE_PLUGIN_OPTION_THRESHOLD_TOKENS": str(n)})
            assert res.code == 0 and WARNING.format(n=n) in res.stdout, (source, descriptor)


def test_no_warning_at_default(env, run_hook):
    """Умолчание (170000) и ровно 180000 — без предупреждения."""
    assert "Внимание" not in start(run_hook, env, source="compact")
    res = run_hook("SessionStart", env.payload("SessionStart", source="startup"),
                   extra_env={"CLAUDE_PLUGIN_OPTION_THRESHOLD_TOKENS": "180000"})
    assert res.code == 0 and "Внимание" not in res.stdout


def test_session_start_compact_removes_clear_flag(env, run_hook):
    env.sessions.mkdir(parents=True, exist_ok=True)
    flag = env.sessions / "s1.clear.json"
    other = env.sessions / "s2.clear.json"
    flag.write_text("{}", encoding="utf-8")
    other.write_text("{}", encoding="utf-8")
    start(run_hook, env, source="resume")
    assert flag.exists()  # флаг остаётся при возобновлении
    start(run_hook, env, source="compact")
    assert not flag.exists() and other.exists()


def test_cleanup_removes_old_clear_flag(env, run_hook):
    env.sessions.mkdir(parents=True, exist_ok=True)
    old = env.sessions / "s9.clear.json"
    old.write_text("{}", encoding="utf-8")
    t = time.time() - 8 * 86400
    os.utime(old, (t, t))
    start(run_hook, env, source="startup")
    assert not old.exists()
