"""Тесты cg_transcript.context_tokens: формула, отбор записей, хвост, битые строки, кириллица, сжатие."""
from __future__ import annotations

import json
import sys

import pytest

import time

from cg_fixtures import SCRIPTS, assistant, human_user, iso_z, make_transcript, queued_human

sys.path.insert(0, str(SCRIPTS))
from cg_transcript import context_tokens, last_assistant, last_human_ts  # noqa: E402


@pytest.fixture
def transcript(tmp_path):
    """Журнал с последним ответом на 160K токенов."""
    return make_transcript(tmp_path / "t.jsonl", [assistant(1, 0, 1000, 10), assistant(2, 100, 159_000, 898)])


def test_normal_sum_of_last(transcript):
    assert context_tokens(transcript) == 2 + 100 + 159_000 + 898


def test_sidechain_ignored(tmp_path):
    p = make_transcript(tmp_path / "a.jsonl", [assistant(5, 0, 1000, 5), assistant(9, 0, 900_000, 1, sidechain=True)])
    assert context_tokens(p) == 1010


def test_api_error_ignored(tmp_path):
    p = make_transcript(tmp_path / "a.jsonl", [assistant(5, 0, 1000, 5), assistant(0, 0, 0, 0, api_error=True)])
    assert context_tokens(p) == 1010


def test_zero_usage_skipped(tmp_path):
    p = make_transcript(tmp_path / "a.jsonl", [assistant(5, 0, 1000, 5), assistant()])
    assert context_tokens(p) == 1010


def test_broken_lines_and_truncated_tail(tmp_path):
    p = make_transcript(tmp_path / "a.jsonl", [assistant(1, 0, 500, 1)], tail='мусор\n{"type": "assistant", "mess')
    assert context_tokens(p) == 502


def test_no_assistant(tmp_path):
    p = make_transcript(tmp_path / "a.jsonl", [{"type": "user", "message": {}}])
    assert context_tokens(p) is None


def test_empty_and_missing(tmp_path):
    (tmp_path / "e.jsonl").write_bytes(b"")
    assert context_tokens(tmp_path / "e.jsonl") is None
    assert context_tokens(tmp_path / "nope.jsonl") is None
    assert context_tokens(None) is None


def test_crlf(tmp_path):
    p = make_transcript(tmp_path / "a.jsonl", [assistant(1, 0, 500, 1)], newline="\r\n")
    assert context_tokens(p) == 502


def test_big_file_tail_read(tmp_path):
    filler = [{"type": "user", "message": {"content": "x" * 1000}}] * 6000  # > 4 МБ
    p = make_transcript(tmp_path / "big.jsonl", [assistant(1, 0, 10, 1)] + filler + [assistant(1, 0, 777, 1)])
    assert p.stat().st_size > 4 * 1024 * 1024
    assert context_tokens(p) == 779


def test_big_file_full_pass_when_tail_has_none(tmp_path):
    filler = [{"type": "user", "message": {"content": "x" * 1000}}] * 6000
    p = make_transcript(tmp_path / "big.jsonl", [assistant(1, 0, 10, 1)] + filler)
    assert context_tokens(p) == 12


def test_cyrillic_path(tmp_path):
    d = tmp_path / "проект-Ж" / "сессия"
    d.mkdir(parents=True)
    rec = assistant(1, 0, 500, 1)
    rec["cwd"] = "C:/Проекты/пример"
    p = make_transcript(d / "журнал.jsonl", [rec])
    assert context_tokens(str(p)) == 502


def test_compact_boundary_then_lower_usage(tmp_path):
    boundary = {"type": "system", "subtype": "compact_boundary",
                "compactMetadata": {"trigger": "auto", "preTokens": 269091, "postTokens": 12000}}
    p = make_transcript(tmp_path / "a.jsonl", [assistant(1, 0, 269_000, 90), boundary])
    assert context_tokens(p) == 269_091
    with open(p, "ab") as f:
        f.write((json.dumps(assistant(3, 0, 12_000, 40)) + "\n").encode("utf-8"))
    assert context_tokens(p) == 12_043


def test_last_assistant_parses_z_timestamp(tmp_path):
    ts = time.time() - 10
    p = make_transcript(tmp_path / "a.jsonl", [assistant(0, 0, 1000, 5, timestamp=iso_z(ts))])
    tokens, got = last_assistant(p)
    assert tokens == 1005 and abs(got - ts) < 0.01
    assert context_tokens(p) == 1005


def test_last_assistant_naive_or_missing_timestamp_gives_none(tmp_path):
    naive = time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime())
    p = make_transcript(tmp_path / "a.jsonl", [assistant(0, 0, 1000, 5, timestamp=naive)])
    assert last_assistant(p) == (1005, None)
    p = make_transcript(tmp_path / "b.jsonl", [assistant(0, 0, 1000, 5)])
    assert last_assistant(p) == (1005, None)
    old = make_transcript(tmp_path / "c.jsonl", [assistant(0, 0, 1000, 5, timestamp=iso_z(time.time() - 2 * 86400))])
    assert last_assistant(old) == (1005, None)  # вне окна правдоподобия
    future = make_transcript(tmp_path / "d.jsonl", [assistant(0, 0, 1000, 5, timestamp=iso_z(time.time() + 3600))])
    assert last_assistant(future) == (1005, None)


def test_last_assistant_garbage_timestamp_gives_none(tmp_path):
    for i, bad in enumerate(["не-время", "", 12345, None, ["x"]]):
        rec = assistant(0, 0, 1000, 5)
        rec["timestamp"] = bad
        p = make_transcript(tmp_path / f"g{i}.jsonl", [rec])
        assert last_assistant(p) == (1005, None)
    assert last_assistant(tmp_path / "нет.jsonl") is None


def test_last_human_ts_user_prompt(tmp_path):
    now = time.time()
    p = make_transcript(tmp_path / "t.jsonl", [human_user(now - 100), assistant(0, 0, 5, 0), human_user(now - 50, "позже"),
                                               assistant(0, 0, 6, 0)])
    assert last_human_ts(p) == pytest.approx(now - 50, abs=0.01)


def test_last_human_ts_queued_command(tmp_path):
    now = time.time()
    p = make_transcript(tmp_path / "t.jsonl", [human_user(now - 100), assistant(0, 0, 5, 0), queued_human(now - 30)])
    assert last_human_ts(p) == pytest.approx(now - 30, abs=0.01)
    # queued_command без origin не подходит
    bad = queued_human(now - 10)
    del bad["attachment"]["origin"]
    p = make_transcript(tmp_path / "u.jsonl", [human_user(now - 100), bad])
    assert last_human_ts(p) == pytest.approx(now - 100, abs=0.01)


def test_last_human_ts_ignores_peer_task_notification_meta(tmp_path):
    now = time.time()
    peer = {"type": "user", "isSidechain": False, "timestamp": iso_z(now - 10), "origin": {"kind": "peer"},
            "message": {"role": "user", "content": "Another Claude session sent a message"}}
    notif = {"type": "user", "isSidechain": False, "timestamp": iso_z(now - 9), "origin": {"kind": "task-notification"},
             "message": {"role": "user", "content": "<task-notification>x</task-notification>"}}
    meta = human_user(now - 8)
    meta["isMeta"] = True
    side = human_user(now - 7)
    side["isSidechain"] = True
    result = human_user(now - 6)
    result["message"]["content"] = [{"type": "tool_result", "tool_use_id": "t", "content": "human"}]
    p = make_transcript(tmp_path / "t.jsonl", [human_user(now - 100), peer, notif, meta, side, result])
    assert last_human_ts(p) == pytest.approx(now - 100, abs=0.01)


def test_last_human_ts_no_origin_gives_none(tmp_path):
    now = time.time()
    rec = human_user(now - 10)
    del rec["origin"]
    p = make_transcript(tmp_path / "t.jsonl", [rec, assistant(0, 0, 5, 0)], tail='{"broken": "human"')
    assert last_human_ts(p) is None
    assert last_human_ts(tmp_path / "нет.jsonl") is None
    assert last_human_ts(None) is None
    p = make_transcript(tmp_path / "ts.jsonl", [human_user(now + 3600)])  # время вне окна правдоподобия
    assert last_human_ts(p) is None
