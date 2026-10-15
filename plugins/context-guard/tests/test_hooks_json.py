"""Тесты hooks.json: форма команд трёх событий и сквозной прогон команды SessionStart через оболочку."""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys

import pytest

from cg_fixtures import PLUGIN

EVENTS = ("Stop", "PostToolUse", "SessionStart")


def _hooks() -> dict:
    return json.loads((PLUGIN / "hooks" / "hooks.json").read_text(encoding="utf-8"))["hooks"]


def _command(event: str) -> dict:
    return _hooks()[event][0]["hooks"][0]


@pytest.mark.parametrize("event", EVENTS)
def test_command_is_shell_string_without_args(event):
    """Команда — shell-строка с запуском run.py; поле args не используется."""
    hook = _command(event)
    assert "args" not in hook
    # путь к run.py взят в кавычки, поэтому после него идёт закрывающая кавычка и имя события
    assert '${CLAUDE_PLUGIN_ROOT}/scripts/run.py" ' + event in hook["command"]


def _find_sh():
    """Оболочка для прогона: на Windows только bash из Git for Windows (bash.exe из System32 — это WSL)."""
    if os.name == "nt":
        roots = [os.environ.get("ProgramFiles"), os.environ.get("ProgramFiles(x86)")]
        for cand in [shutil.which("bash")] + [os.path.join(r, "Git", "bin", "bash.exe") for r in roots if r]:
            if cand and "Git" in cand and os.path.exists(cand):
                return cand
        return None
    return shutil.which("sh")


def test_session_start_command_runs_through_shell(env):
    """Сквозной прогон: строка команды SessionStart исполняется `sh -c`, код выхода 0."""
    sh = _find_sh()
    if sh is None:
        pytest.skip("нет подходящей оболочки (на Windows нужен Git for Windows)")
    e = {k: v for k, v in os.environ.items() if not k.startswith("CLAUDE_PLUGIN_")}
    if os.name == "nt":
        e["OS"] = "Windows_NT"
        e["PATH"] = os.path.dirname(sys.executable) + os.pathsep + e.get("PATH", "")
        if shutil.which("python", path=e["PATH"]) is None:
            pytest.skip("нет python в PATH")
    else:
        py3 = shutil.which("python3")
        if py3 is None:
            pytest.skip("нет python3 в PATH")
    e["CLAUDE_PLUGIN_ROOT"] = PLUGIN.as_posix()
    e["CLAUDE_PLUGIN_DATA"] = str(env.data)
    body = json.dumps(env.payload("SessionStart", source="startup"), ensure_ascii=False).encode("utf-8")
    proc = subprocess.run([sh, "-c", _command("SessionStart")["command"]], input=body,
                          capture_output=True, env=e, timeout=60)
    assert proc.returncode == 0, proc.stderr.decode("utf-8", "replace")
