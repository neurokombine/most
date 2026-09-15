"""Исполнитель: адаптер к нейросети в папке проекта.

Интерфейс один — `run(prompt, workdir, session_id) -> Result`. Реализаций две:
`ClaudeExecutor` (настоящий `claude -p`) и `FakeExecutor` (для тестов).

Списано с боевого `room_task.py`, четыре решения перенесены дословно:
  1. чистое окружение словарём — вложенный `claude` наследует CLAUDECODE/
     CLAUDE_CODE_ENTRYPOINT от родительской сессии и молча падает;
  2. `start_new_session=True` — работа переживает смерть родителя и перезапуск юнита;
  3. `--session-id` задаём сами, чтобы сессия была адресуема снаружи;
  4. журнал работы — папкой на диске: состояние читается с диска, не из памяти.

Ключей нейросетей здесь нет ни в каком виде: `claude` работает под сохранённым
входом по подписке. Всё, что похоже на токен, маскируется до записи в лог.
"""
from __future__ import annotations

import json
import os
import shutil
import signal
import subprocess
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path

from . import narrator
from .receivers.base import mask

DEFAULT_TIMEOUT = 900
ERR_TAIL = 800

# Что оставляем вложенному процессу. Всё прочее (включая CLAUDECODE*,
# CLAUDE_CODE_ENTRYPOINT и любые ключи API) отрезаем.
KEEP = ("HOME", "USER", "LOGNAME", "PATH", "LANG", "LC_ALL", "TERM", "TZ", "SHELL")
FALLBACK = {
    "PATH": "/usr/local/bin:/usr/bin:/bin",
    "LANG": "C.UTF-8",
    "TERM": "dumb",
}


@dataclass
class Result:
    ok: bool
    exit_code: int | None
    session_id: str
    job_id: str
    job_dir: Path
    events: list[dict] = field(default_factory=list)
    text: str = ""
    error: str = ""
    timed_out: bool = False


def clean_env(source: dict | None = None) -> dict:
    """Чистое окружение для вложенной нейросети."""
    source = dict(source if source is not None else os.environ)
    env = {key: source[key] for key in KEEP if source.get(key)}
    for key, value in FALLBACK.items():
        env.setdefault(key, value)
    env.setdefault("HOME", str(Path.home()))
    user = env.get("USER") or env.get("LOGNAME") or "most"
    env.setdefault("USER", user)
    env.setdefault("LOGNAME", user)
    return env


def resolve_claude_bin() -> str:
    local = Path.home() / ".local" / "bin" / "claude"
    if local.exists():
        return str(local)
    return shutil.which("claude") or "claude"


class Executor:
    """Общий интерфейс. Этап 2 добавит сюда продолжение сессии (--resume)."""

    def run(self, prompt: str, workdir: Path, session_id: str | None = None) -> Result:
        raise NotImplementedError


class FakeExecutor(Executor):
    """Подставной исполнитель: ничего не запускает, всё записывает."""

    def __init__(self, text: str = "Готово.", ok: bool = True, exit_code: int = 0):
        self.text = text
        self.ok = ok
        self.exit_code = exit_code
        self.calls: list[dict] = []

    def run(self, prompt: str, workdir: Path, session_id: str | None = None) -> Result:
        self.calls.append({"prompt": prompt, "workdir": Path(workdir), "session_id": session_id})
        session_id = session_id or str(uuid.uuid4())
        events = [{"type": "result", "subtype": "success", "is_error": not self.ok,
                   "result": self.text, "session_id": session_id}]
        return Result(ok=self.ok, exit_code=self.exit_code, session_id=session_id,
                      job_id="fake-" + uuid.uuid4().hex[:8], job_dir=Path(workdir),
                      events=events, text=self.text)


class ClaudeExecutor(Executor):
    """Настоящий запуск: `claude -p … --output-format stream-json --verbose`."""

    def __init__(self, jobs_dir: Path, claude_bin=None, timeout: int = DEFAULT_TIMEOUT,
                 secrets=None, skip_permissions: bool = True):
        self.jobs_dir = Path(jobs_dir)
        self.claude_bin = claude_bin or resolve_claude_bin()
        self.timeout = timeout
        self.secrets = list(secrets or [])
        self.skip_permissions = skip_permissions

    # --- служебное ----------------------------------------------------------

    def _bin(self) -> list[str]:
        return list(self.claude_bin) if isinstance(self.claude_bin, (list, tuple)) \
            else [str(self.claude_bin)]

    def _mask(self, text) -> str:
        return mask(text, self.secrets)

    def _new_job_dir(self) -> tuple[str, Path]:
        job_id = time.strftime("%Y%m%d-%H%M%S") + "-" + uuid.uuid4().hex[:6]
        job_dir = self.jobs_dir / job_id
        job_dir.mkdir(parents=True, exist_ok=True)
        return job_id, job_dir

    # --- запуск -------------------------------------------------------------

    def run(self, prompt: str, workdir: Path, session_id: str | None = None) -> Result:
        workdir = Path(workdir)
        session_id = session_id or str(uuid.uuid4())
        job_id, job_dir = self._new_job_dir()

        (job_dir / "prompt.md").write_text(self._mask(prompt), encoding="utf-8")
        meta = {"id": job_id, "session_id": session_id, "workdir": str(workdir),
                "started_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"), "pid": None}
        _write_json(job_dir / "meta.json", meta)

        cmd = self._bin() + [
            "-p", prompt,
            "--output-format", "stream-json",
            "--verbose",
            "--session-id", session_id,
        ]
        if self.skip_permissions:
            cmd.append("--dangerously-skip-permissions")

        out_path = job_dir / "out.jsonl"
        err_path = job_dir / "err.log"
        timed_out = False

        try:
            with open(out_path, "wb") as out, open(err_path, "wb") as err:
                proc = subprocess.Popen(
                    cmd, cwd=str(workdir), env=clean_env(),
                    stdin=subprocess.DEVNULL, stdout=out, stderr=err,
                    start_new_session=True, close_fds=True)
                (job_dir / "pid").write_text(str(proc.pid), encoding="utf-8")
                meta["pid"] = proc.pid
                _write_json(job_dir / "meta.json", meta)
                try:
                    exit_code = proc.wait(timeout=self.timeout)
                except subprocess.TimeoutExpired:
                    timed_out = True
                    _kill_group(proc)
                    exit_code = None
        except FileNotFoundError:
            # claude не найден — это чинится руками, и сказать надо по-человечески.
            (job_dir / "exit.code").write_text("no-binary", encoding="utf-8")
            err_path.touch()
            (job_dir / "pid").write_text("", encoding="utf-8")
            return Result(ok=False, exit_code=None, session_id=session_id, job_id=job_id,
                          job_dir=job_dir, events=[], text="",
                          error=f"claude не найден: {self._bin()[0]}")
        except OSError as exc:
            (job_dir / "exit.code").write_text("error", encoding="utf-8")
            return Result(ok=False, exit_code=None, session_id=session_id, job_id=job_id,
                          job_dir=job_dir, events=[], text="",
                          error=self._mask(f"не удалось запустить claude: {exc}"))

        (job_dir / "exit.code").write_text(
            "timeout" if timed_out else str(exit_code), encoding="utf-8")

        raw = out_path.read_text(encoding="utf-8", errors="replace")
        events = narrator.parse_stream(raw)
        err_tail = self._mask(err_path.read_text(encoding="utf-8", errors="replace").strip())[-ERR_TAIL:]

        meta["finished_at"] = time.strftime("%Y-%m-%dT%H:%M:%S%z")
        meta["exit_code"] = "timeout" if timed_out else exit_code
        _write_json(job_dir / "meta.json", meta)

        ok = (not timed_out) and exit_code == 0
        text = narrator.final_text(events) if events else ""
        error = ""
        if timed_out:
            error = f"работа шла дольше {self.timeout} с и была остановлена"
        elif not ok:
            error = err_tail or f"claude завершился с кодом {exit_code}"

        return Result(ok=ok, exit_code=exit_code, session_id=narrator.session_id_of(events) or session_id,
                      job_id=job_id, job_dir=job_dir, events=events, text=text,
                      error=error, timed_out=timed_out)


def _write_json(path: Path, data: dict) -> None:
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


def _kill_group(proc) -> None:
    """Гасим всю группу процессов: claude поднимает детей, одинокий kill их оставит."""
    for sig, wait in ((signal.SIGTERM, 10), (signal.SIGKILL, 5)):
        try:
            os.killpg(os.getpgid(proc.pid), sig)
        except (ProcessLookupError, PermissionError, OSError):
            return
        try:
            proc.wait(timeout=wait)
            return
        except subprocess.TimeoutExpired:
            continue
