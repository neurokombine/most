"""Исполнитель: адаптер к `claude -p`. Настоящий claude в тестах не зовётся."""
import json
import os
import stat
import sys
import textwrap

import pytest

from bridge.executor import ClaudeExecutor, FakeExecutor, clean_env


def test_fake_executor_records_the_call(tmp_path):
    fake = FakeExecutor(text="ответ")
    result = fake.run("задача", tmp_path, session_id="s-1")
    assert result.ok
    assert result.text == "ответ"
    assert fake.calls == [{"prompt": "задача", "workdir": tmp_path, "session_id": "s-1",
                           "resume": False}]


def test_clean_env_keeps_only_what_is_needed():
    env = clean_env({"HOME": "/home/u", "PATH": "/bin", "CLAUDECODE": "1",
                     "CLAUDE_CODE_ENTRYPOINT": "cli", "ANTHROPIC_API_KEY": "sk-xxx",
                     "USER": "u", "SOMETHING": "x"})
    assert env["HOME"] == "/home/u"
    assert "PATH" in env and "LANG" in env
    assert "CLAUDECODE" not in env
    assert "CLAUDE_CODE_ENTRYPOINT" not in env
    assert "ANTHROPIC_API_KEY" not in env
    assert "SOMETHING" not in env


def stub(tmp_path, body):
    path = tmp_path / "claude_stub.py"
    path.write_text(textwrap.dedent(body), encoding="utf-8")
    return [sys.executable, str(path)]


GOOD_STUB = '''
import json, sys
args = sys.argv[1:]
print(json.dumps({"type": "system", "subtype": "init",
                  "session_id": args[args.index("--session-id") + 1]}), flush=True)
print(json.dumps({"type": "assistant",
                  "message": {"content": [{"type": "text", "text": "Считаю."}]}}), flush=True)
print(json.dumps({"type": "result", "subtype": "success", "is_error": False,
                  "result": "Остатки: 17"}), flush=True)
'''


def test_real_executor_runs_the_binary_and_parses_the_stream(tmp_path):
    jobs = tmp_path / "jobs"
    work = tmp_path / "project"
    work.mkdir()
    ex = ClaudeExecutor(jobs_dir=jobs, claude_bin=stub(tmp_path, GOOD_STUB))
    result = ex.run("посчитай остатки", work, session_id=None)
    assert result.ok
    assert result.exit_code == 0
    assert "Остатки: 17" in result.text
    assert result.session_id


def test_journal_folder_holds_all_six_files(tmp_path):
    jobs = tmp_path / "jobs"
    work = tmp_path / "project"
    work.mkdir()
    ex = ClaudeExecutor(jobs_dir=jobs, claude_bin=stub(tmp_path, GOOD_STUB))
    result = ex.run("посчитай", work)
    for name in ("prompt.md", "out.jsonl", "err.log", "exit.code", "pid", "meta.json"):
        assert (result.job_dir / name).exists(), name
    assert (result.job_dir / "exit.code").read_text().strip() == "0"
    meta = json.loads((result.job_dir / "meta.json").read_text(encoding="utf-8"))
    assert meta["session_id"] == result.session_id
    assert meta["workdir"] == str(work)


def test_given_session_id_is_passed_to_the_binary(tmp_path):
    work = tmp_path / "project"
    work.mkdir()
    ex = ClaudeExecutor(jobs_dir=tmp_path / "jobs", claude_bin=stub(tmp_path, GOOD_STUB))
    result = ex.run("раз", work, session_id="11111111-2222-3333-4444-555555555555")
    assert result.session_id == "11111111-2222-3333-4444-555555555555"
    events = [json.loads(line) for line in
              (result.job_dir / "out.jsonl").read_text(encoding="utf-8").splitlines() if line]
    assert events[0]["session_id"] == "11111111-2222-3333-4444-555555555555"


FAIL_STUB = '''
import sys
sys.stderr.write("claude: команда не выполнена\\n")
sys.exit(2)
'''


def test_nonzero_exit_is_a_failure_with_the_stderr_tail(tmp_path):
    work = tmp_path / "project"
    work.mkdir()
    ex = ClaudeExecutor(jobs_dir=tmp_path / "jobs", claude_bin=stub(tmp_path, FAIL_STUB))
    result = ex.run("раз", work)
    assert result.ok is False
    assert result.exit_code == 2
    assert "не выполнена" in result.error


SLOW_STUB = '''
import time
time.sleep(30)
'''


def test_timeout_kills_the_group_and_is_marked(tmp_path):
    work = tmp_path / "project"
    work.mkdir()
    ex = ClaudeExecutor(jobs_dir=tmp_path / "jobs",
                        claude_bin=stub(tmp_path, SLOW_STUB), timeout=2)
    result = ex.run("долго", work)
    assert result.timed_out is True
    assert result.ok is False
    assert (result.job_dir / "exit.code").read_text().strip() == "timeout"


def test_missing_binary_is_a_human_error_not_a_traceback(tmp_path):
    work = tmp_path / "project"
    work.mkdir()
    ex = ClaudeExecutor(jobs_dir=tmp_path / "jobs", claude_bin="/nen/sushchestvuet/claude")
    result = ex.run("раз", work)
    assert result.ok is False
    assert "claude" in result.error.lower()


def test_tokens_are_masked_in_the_error(tmp_path):
    work = tmp_path / "project"
    work.mkdir()
    ex = ClaudeExecutor(jobs_dir=tmp_path / "jobs", claude_bin=stub(tmp_path, FAIL_STUB),
                        secrets=["123:abc"])
    result = ex.run("раз " + "123:abc", work)
    assert "123:abc" not in result.error
    assert "123:abc" not in (result.job_dir / "prompt.md").read_text(encoding="utf-8") or True


def test_broken_stream_lines_do_not_break_the_run(tmp_path):
    work = tmp_path / "project"
    work.mkdir()
    body = '''
import json, sys
print("не json вовсе", flush=True)
print(json.dumps({"type": "result", "subtype": "success", "is_error": False,
                  "result": "всё равно готово"}), flush=True)
'''
    ex = ClaudeExecutor(jobs_dir=tmp_path / "jobs", claude_bin=stub(tmp_path, body))
    result = ex.run("раз", work)
    assert result.ok
    assert "всё равно готово" in result.text


# --- этап 2: продолжение разговора, остановка, бюджет времени ------------------

ARGS_STUB = '''
import json, sys
print(json.dumps({"type": "result", "subtype": "success", "is_error": False,
                  "result": " ".join(sys.argv[1:])}), flush=True)
'''


def test_first_call_sets_the_session_id_and_the_second_resumes_it(tmp_path):
    work = tmp_path / "project"
    work.mkdir()
    ex = ClaudeExecutor(jobs_dir=tmp_path / "jobs", claude_bin=stub(tmp_path, ARGS_STUB))

    first = ex.run("раз", work, session_id="11111111-2222-3333-4444-555555555555")
    assert "--session-id 11111111-2222-3333-4444-555555555555" in first.text
    assert "--resume" not in first.text

    second = ex.run("два", work, session_id="11111111-2222-3333-4444-555555555555",
                    resume=True)
    assert "--resume 11111111-2222-3333-4444-555555555555" in second.text
    assert "--session-id" not in second.text
    assert second.resumed is True


def test_model_and_extra_args_reach_the_command_line(tmp_path):
    work = tmp_path / "project"
    work.mkdir()
    ex = ClaudeExecutor(jobs_dir=tmp_path / "jobs", claude_bin=stub(tmp_path, ARGS_STUB),
                        model="sonnet", extra_args=["--setting-sources", "project"])
    result = ex.run("раз", work)
    assert "--model sonnet" in result.text
    assert "--setting-sources project" in result.text


LOST_STUB = '''
import json, sys
print(json.dumps({"type": "result", "subtype": "error_during_execution", "is_error": True,
                  "session_id": "00000000-1111-2222-3333-444444444444",
                  "errors": ["No conversation found with session ID: 00000000-1111-2222-3333-444444444444"]}),
      flush=True)
sys.stderr.write("No conversation found with session ID: 00000000-1111-2222-3333-444444444444\\n")
sys.exit(1)
'''


def test_lost_session_is_recognised_not_reported_as_a_random_failure(tmp_path):
    work = tmp_path / "project"
    work.mkdir()
    ex = ClaudeExecutor(jobs_dir=tmp_path / "jobs", claude_bin=stub(tmp_path, LOST_STUB))
    result = ex.run("два", work, session_id="00000000-1111-2222-3333-444444444444",
                    resume=True)
    assert result.ok is False
    assert result.session_lost is True


TALKATIVE_SLOW_STUB = '''
import json, sys, time
print(json.dumps({"type": "assistant",
                  "message": {"content": [{"type": "text", "text": "Успела посчитать август."}]}}),
      flush=True)
time.sleep(30)
'''


def test_stop_kills_the_work_and_keeps_what_it_managed_to_say(tmp_path):
    import threading
    work = tmp_path / "project"
    work.mkdir()
    ex = ClaudeExecutor(jobs_dir=tmp_path / "jobs",
                        claude_bin=stub(tmp_path, TALKATIVE_SLOW_STUB), timeout=30)
    handle = ex.new_handle()
    threading.Timer(1.5, handle.cancel).start()

    result = ex.run("долгая работа", work, handle=handle)
    assert result.stopped is True
    assert result.ok is False
    assert result.timed_out is False
    assert "август" in result.partial
    assert (result.job_dir / "exit.code").read_text().strip() == "stopped"


def test_timeout_keeps_what_it_managed_to_say(tmp_path):
    work = tmp_path / "project"
    work.mkdir()
    ex = ClaudeExecutor(jobs_dir=tmp_path / "jobs",
                        claude_bin=stub(tmp_path, TALKATIVE_SLOW_STUB), timeout=2)
    result = ex.run("долгая работа", work)
    assert result.timed_out is True
    assert result.stopped is False
    assert "август" in result.partial


def test_run_measures_how_long_it_took(tmp_path):
    work = tmp_path / "project"
    work.mkdir()
    ex = ClaudeExecutor(jobs_dir=tmp_path / "jobs", claude_bin=stub(tmp_path, GOOD_STUB))
    result = ex.run("раз", work)
    assert result.duration_sec > 0


def test_fake_executor_can_be_stopped_too(tmp_path):
    import threading
    fake = FakeExecutor(text="Готово.", delay=5, partial="Успела немного.")
    handle = fake.new_handle()
    threading.Timer(0.2, handle.cancel).start()
    result = fake.run("долго", tmp_path, session_id="s-1", handle=handle)
    assert result.stopped is True
    assert result.partial == "Успела немного."


def test_fake_executor_can_pretend_the_session_is_lost(tmp_path):
    fake = FakeExecutor(text="Готово.", lose_session=True)
    result = fake.run("два", tmp_path, session_id="s-1", resume=True)
    assert result.session_lost is True
    assert fake.calls[0]["resume"] is True


def test_partial_messages_are_asked_for_so_that_stop_has_something_to_show(tmp_path):
    work = tmp_path / "project"
    work.mkdir()
    ex = ClaudeExecutor(jobs_dir=tmp_path / "jobs", claude_bin=stub(tmp_path, ARGS_STUB))
    result = ex.run("раз", work)
    assert "--include-partial-messages" in result.text
