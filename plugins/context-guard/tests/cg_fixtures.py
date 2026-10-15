"""Помощники тестов context-guard: записи журнала, окружение сессии, файлы состояния и передачи."""
from __future__ import annotations

import json
import os
import time
from datetime import datetime, timezone
from pathlib import Path

PLUGIN = Path(__file__).resolve().parent.parent
SCRIPTS = PLUGIN / "scripts"
RUN_PY = SCRIPTS / "run.py"


def assistant(tokens_in=0, cache_c=0, cache_r=0, out=0, sidechain=False, api_error=False, timestamp=None) -> dict:
    """Запись assistant журнала с usage; timestamp (строка) добавляется в запись, если задан."""
    rec = {"type": "assistant", "isSidechain": sidechain,
           "message": {"usage": {"input_tokens": tokens_in, "cache_creation_input_tokens": cache_c,
                                 "cache_read_input_tokens": cache_r, "output_tokens": out}}}
    if api_error:
        rec["isApiErrorMessage"] = True
    if timestamp is not None:
        rec["timestamp"] = timestamp
    return rec


def human_user(epoch: float, text="сообщение человека") -> dict:
    """Запись user с origin.kind == human на верхнем уровне записи (форма реального журнала 2.1.300)."""
    return {"type": "user", "isSidechain": False, "message": {"role": "user", "content": text},
            "timestamp": iso_z(epoch), "origin": {"kind": "human"}}


def queued_human(epoch: float, text="набрано во время хода") -> dict:
    """Вложение queued_command с attachment.origin.kind == human (сообщение, набранное во время хода)."""
    return {"type": "attachment", "isSidechain": False, "timestamp": iso_z(epoch),
            "attachment": {"type": "queued_command", "prompt": text, "commandMode": "prompt",
                           "origin": {"kind": "human"}, "timestamp": iso_z(epoch), "humanTurn": True}}


def iso_z(epoch: float) -> str:
    """Epoch -> ISO-время в UTC с суффиксом Z, как в журнале Claude Code."""
    return datetime.fromtimestamp(epoch, timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"


def make_transcript(path: Path, records, newline="\n", tail: str = "") -> Path:
    """Пишет журнал jsonl; tail — произвольный хвост (мусор/обрезанная строка)."""
    text = "".join(json.dumps(r, ensure_ascii=False) + newline for r in records) + tail
    Path(path).write_bytes(text.encode("utf-8"))
    return Path(path)


def tokens_transcript(path: Path, tokens: int) -> Path:
    """Журнал, у которого последний ответ даёт ровно `tokens` токенов контекста."""
    return make_transcript(path, [assistant(0, 0, tokens, 0)])


class Env:
    """Окружение сессии: каталог данных плагина и каталог журнала «проекта Claude» (родитель transcript_path)."""

    def __init__(self, root: Path, project_name: str = "C--Projects-demo"):
        self.root = root
        self.data = root / "plugin_data"
        self.logdir = root / "claude_home" / "projects" / project_name
        self.logdir.mkdir(parents=True)
        self.transcript = self.logdir / "t.jsonl"
        self.sessions = self.data / "sessions"
        self.handoff = self.data / "handoff" / f"{project_name}.md"  # адресат без дескриптора

    def payload(self, event="Stop", sid="s1", **extra) -> dict:
        """Payload хука: session_id, transcript_path, cwd (cwd плагином не используется)."""
        p = {"hook_event_name": event, "cwd": str(self.root / "repo"), "session_id": sid,
             "transcript_path": str(self.transcript)}
        if sid is None:
            p.pop("session_id")
        p.update(extra)
        return p

    def tool_payload(self, sid="s1", **extra) -> dict:
        """Payload PostToolUse главного потока."""
        return self.payload("PostToolUse", sid, **{"tool_name": "Read", "tool_input": {}, **extra})

    def set_tokens(self, tokens: int, timestamp=None) -> None:
        """Перезаписывает журнал: последний ответ даёт `tokens` токенов контекста (timestamp - ISO-строка записи)."""
        make_transcript(self.transcript, [assistant(0, 0, tokens, 0, timestamp=timestamp)])

    def set_journal(self, tokens: int, *extra) -> None:
        """Журнал: ответ на `tokens` токенов контекста, затем записи extra (например, сообщения человека)."""
        make_transcript(self.transcript, [assistant(0, 0, tokens, 0), *extra])

    def write_descriptor(self, handoff_path=None, **fields) -> Path:
        """Дескриптор поставщика (схема 1, внешний поставщик) в каталоге журнала; fields перекрывают поля."""
        hp = handoff_path if handoff_path is not None else str(self.root / "art" / "handoff.md")
        data = {"schema": 1, "provider": "example", "handoff_path": str(hp).replace("\\", "/"),
                "template": "ШАБЛОН-ПОСТАВЩИКА",
                "loads_on_start": True, "written_at": time.time(), **fields}
        p = self.logdir / "handoff-provider.json"
        p.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
        return p

    def write_handoff(self, path: Path | None = None, mtime=None, text="## Раздел\nтекст\n") -> Path:
        """Создаёт файл передачи (по умолчанию собственный); mtime задаёт время правки."""
        p = path or self.handoff
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text, encoding="utf-8")
        if mtime is not None:
            os.utime(p, (mtime, mtime))
        return p

    def state(self, kind: str, sid="s1") -> dict | None:
        """Содержимое <sid>.<kind>.json (kind: ctx | mid) или None, если файла нет."""
        p = self.sessions / f"{sid}.{kind}.json"
        return json.loads(p.read_text(encoding="utf-8")) if p.exists() else None

    def write_state(self, kind: str, data: dict, sid="s1") -> None:
        """Записывает файл состояния сессии."""
        self.sessions.mkdir(parents=True, exist_ok=True)
        (self.sessions / f"{sid}.{kind}.json").write_text(json.dumps(data), encoding="utf-8")

    def ctx_state(self, sid="s1") -> dict | None:
        """Состояние контекстного триггера Stop."""
        return self.state("ctx", sid)

    def mid_state(self, sid="s1") -> dict | None:
        """Состояние сигнала внутри хода."""
        return self.state("mid", sid)

    def write_mid_state(self, last: int, at: float, sid="s1", **extra) -> None:
        """Состояние сигнала внутри хода; extra — например, ctx_ignore."""
        self.write_state("mid", {"mid_last_trigger": last, "mid_trigger_at": at, **extra}, sid)
