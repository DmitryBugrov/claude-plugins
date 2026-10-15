"""Общий каркас тестов context-guard: запуск хуков подпроцессом, как это делает Claude Code.

Фикстуры:
- env — окружение сессии (Env из cg_fixtures): каталог данных плагина, каталог журнала «проекта Claude»;
- run_hook — run_hook(event, payload | None, extra_env=None, raw=None, data=True, python=None) -> HookResult;
  запуск `python scripts/run.py <событие>` с CLAUDE_PLUGIN_DATA во временном каталоге.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

import pytest

TESTS_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(TESTS_DIR))
from cg_fixtures import Env, RUN_PY  # noqa: E402


@dataclass
class HookResult:
    """Результат запуска хука: код выхода и тексты потоков."""
    code: int
    stdout: str
    stderr: str

    def json(self) -> dict:
        """Разбирает stdout как JSON."""
        return json.loads(self.stdout)


@pytest.fixture
def env(tmp_path) -> Env:
    """Окружение сессии во временном каталоге."""
    return Env(tmp_path)


@pytest.fixture
def run_hook(env):
    """Запуск хука подпроцессом: run_hook(event, payload, extra_env=None, raw=None, data=True, python=None)."""
    def _run(event: str, payload, extra_env: dict | None = None, raw: bytes | None = None,
             data: bool = True, python: str | None = None) -> HookResult:
        full_env = {k: v for k, v in os.environ.items()
                    if not k.startswith("CLAUDE_PLUGIN_") and k != "CLAUDE_PROJECT_DIR"}
        if data:
            full_env["CLAUDE_PLUGIN_DATA"] = str(env.data)
        full_env.update(extra_env or {})
        body = raw if raw is not None else json.dumps(payload, ensure_ascii=False).encode("utf-8")
        proc = subprocess.run([python or sys.executable, str(RUN_PY), event], input=body,
                              capture_output=True, env=full_env, timeout=60)
        return HookResult(proc.returncode, proc.stdout.decode("utf-8", "replace"),
                          proc.stderr.decode("utf-8", "replace"))
    return _run
