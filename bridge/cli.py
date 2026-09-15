"""Командная строка моста — то, чем мост управляют из серверного окна.

Ученик разговаривает не с мостом, а со своей нейросетью в окне редактора:
«покажи, кто стучался, и пускай только меня». Нейросеть читает README, зовёт
вот эти команды и пересказывает ответ. Значит, вывод должен читаться обоими:
человеку — русский текст без жаргона, ей — тот же текст или `--json`.

    python -m bridge --name anna knock
    python -m bridge --name anna allow last
    python -m bridge --name anna allow telegram:12345
    python -m bridge --name anna deny max:12345
    python -m bridge --name anna who
    python -m bridge --name anna status
    python -m bridge --name anna doctor [--live]
    python -m bridge --name anna say "проверка связи"

Команды не берут замок экземпляра: они читают ту же базу, что и живой мост,
и им незачем его останавливать. Белый список правится сразу в двух местах —
в базе (её демон читает на каждом сообщении, перезапуск не нужен) и в
config.yaml (иначе человек снова станет чужим после перезагрузки).
"""
from __future__ import annotations

import json
from datetime import timedelta

from . import alarm, config as config_module, doctor, lock, narrator, texts
from .store import Store

COMMANDS = ("knock", "allow", "deny", "who", "status", "doctor", "say")
KNOCKS_SHOWN = 20
JOBS_SHOWN = 5
TROUBLES_SHOWN = 3
DAY = 24 * 3600


class Answer:
    """Что команда сказала: строки человеку, разбор машине и код возврата."""

    def __init__(self, lines, data: dict | None = None, code: int = 0):
        self.lines = [lines] if isinstance(lines, str) else list(lines)
        self.data = dict(data or {})
        self.code = code

    def text(self) -> str:
        return "\n".join(self.lines)


# --- общее ------------------------------------------------------------------

def channel_name(channel: str) -> str:
    return texts.CHANNEL_NAMES.get(channel, channel)


def how_to_call(config) -> str:
    """Как позвать мост той же командой, что и сейчас: нейросеть повторит её слово в слово."""
    return f"python -m bridge --name {config.name}"


def at_text(raw) -> str:
    """Время из базы человеку — московское и с пометкой."""
    moment = alarm.from_iso(raw)
    return alarm.when_text(moment) if moment is not None else str(raw or "")


def _since_day() -> str:
    return alarm.to_iso(alarm.now() - timedelta(seconds=DAY))


def _named(name) -> str:
    name = (name or "").strip()
    return f" · {name}" if name else ""


def _parse_target(args, config) -> tuple[str, int] | str:
    """«telegram:12345», «max:12345» или просто «12345», если мессенджер один.

    Возвращает пару (канал, номер) или слово-беду: which · bad.
    """
    raw = " ".join(str(a) for a in args).strip().lower().replace(" ", "")
    if not raw:
        return "bad"
    channel, _, number = raw.partition(":")
    if not number:
        number, channel = channel, ""
    if not number.lstrip("-").isdigit():
        return "bad"
    user_id = int(number)
    if channel in ("telegram", "max"):
        return channel, user_id
    if channel:
        return "bad"
    known = config.enabled_channels()
    if len(known) == 1:
        return known[0], user_id
    return "which"


# --- кто стучался -----------------------------------------------------------

def knock(config, store) -> Answer:
    """Кто писал мосту и не был своим — и что мост ответил (ничего)."""
    rows = store.recent_strangers(limit=KNOCKS_SHOWN)
    cmd = how_to_call(config)
    if not rows:
        return Answer(texts.CLI_NO_KNOCKS.format(cmd=cmd), {"knocks": []})

    lines = [texts.CLI_KNOCKS_HEADER]
    knocks = []
    for row in rows:
        allowed = store.is_allowed(row["channel"], row["user_id"])
        answer = texts.CLI_KNOCK_ALLOWED if allowed else texts.CLI_KNOCK_SILENT
        lines.append(texts.CLI_KNOCK_LINE.format(
            at=at_text(row["at"]), channel=channel_name(row["channel"]),
            user_id=row["user_id"], name=_named(row["name"]),
            text=row["text"] or "", answer=answer))
        knocks.append({"at": row["at"], "at_msk": at_text(row["at"]),
                       "channel": row["channel"], "user_id": int(row["user_id"]),
                       "name": row["name"] or "", "text": row["text"] or "",
                       "answer": "промолчала" if not allowed else "ответила",
                       "allowed": bool(allowed)})
    example = f"{rows[0]['channel']}:{rows[0]['user_id']}"
    lines += ["", texts.CLI_KNOCKS_FOOTER.format(cmd=cmd, example=example)]
    return Answer(lines, {"knocks": knocks})


# --- пустить и не пускать ---------------------------------------------------

def _change(config, store, args, add: bool) -> Answer:
    cmd = how_to_call(config)
    target = _parse_target(args, config)
    if target == "which":
        number = "".join(ch for ch in " ".join(args) if ch.isdigit())
        return Answer(texts.CLI_WHICH_CHANNEL.format(cmd=cmd, user_id=number), code=1)
    if target == "bad":
        return Answer(texts.CLI_BAD_ARGUMENT.format(cmd=cmd), code=1)

    channel, user_id = target
    return _apply(config, store, [(channel, user_id, "")], add=add)


def _apply(config, store, people, add: bool) -> Answer:
    """Общая часть «пусти» и «не пускай»: сначала база, потом файл настроек."""
    lines: list[str] = []
    data = {"changed": [], "channel": None}
    for channel, user_id, name in people:
        changed = store.allow(channel, user_id, note=name or None) if add \
            else store.deny(channel, user_id)
        if not changed:
            lines.append((texts.CLI_ALREADY_ALLOWED if add else texts.CLI_NOT_ALLOWED).format(
                channel=channel_name(channel), user_id=user_id))
            continue

        lines.append((texts.CLI_ALLOWED if add else texts.CLI_DENIED).format(
            channel=channel_name(channel), user_id=user_id, name=_named(name)))
        written = (config_module.add_to_allowlist if add else
                   config_module.remove_from_allowlist)(config.config_path, channel, user_id)
        if written in ("added", "removed", "already"):
            lines.append(texts.CLI_ALLOWED_CONFIG.format(path=config.config_path))
        elif written == "no_channel":
            lines.append(texts.CLI_NO_CHANNEL_IN_CONFIG.format(
                channel=channel, path=config.config_path))
        else:
            lines.append(texts.CLI_ALLOWED_NOT_IN_CONFIG.format(
                path=config.config_path, user_id=user_id, channel=channel))
        data["changed"].append({"channel": channel, "user_id": user_id, "name": name,
                                "in_config": written})
    return Answer(lines, data)


def allow(config, store, args) -> Answer:
    """«allow last» — последний, кто стучался, во всех мессенджерах, где стучался."""
    if [str(a).strip().lower() for a in args] == ["last"]:
        last = store.last_knocks()
        if not last:
            return Answer(texts.CLI_NOBODY_KNOCKED_YET, code=1)
        people = []
        for channel, user_id in last.items():
            rows = [r for r in store.recent_strangers(limit=KNOCKS_SHOWN)
                    if r["channel"] == channel and r["user_id"] == user_id]
            people.append((channel, user_id, (rows[0]["name"] if rows else "") or ""))
        return _apply(config, store, people, add=True)
    return _change(config, store, args, add=True)


def deny(config, store, args) -> Answer:
    return _change(config, store, args, add=False)


# --- кто свои ---------------------------------------------------------------

def who(config, store) -> Answer:
    rows = store.list_allowed()
    if not rows:
        return Answer(texts.CLI_WHO_EMPTY.format(cmd=how_to_call(config)), {"allowed": []})
    lines = [texts.CLI_WHO_HEADER.format(name=config.name)]
    people = []
    for row in rows:
        lines.append(texts.CLI_WHO_LINE.format(
            channel=channel_name(row["channel"]), user_id=row["user_id"],
            name=_named(row["note"]), at=at_text(row["added_at"])))
        people.append({"channel": row["channel"], "user_id": int(row["user_id"]),
                       "name": row["note"] or ""})
    return Answer(lines, {"allowed": people})


# --- жив ли мост ------------------------------------------------------------

def status(config, store) -> Answer:
    """Первый вопрос при «бот молчит»: а мост-то вообще запущен."""
    cmd = how_to_call(config)
    pid = lock.InstanceLock(config.home / "most.lock").holder_pid()
    running = pid is not None
    lines = [texts.CLI_STATUS_RUNNING.format(name=config.name, pid=pid) if running
             else texts.CLI_STATUS_NOT_RUNNING.format(name=config.name, cmd=cmd)]

    twins = lock.other_bridges(config.name, mine=pid or None)
    for twin_pid, _ in twins:
        lines.append(texts.CLI_STATUS_TWINS.format(pid=twin_pid))

    heard = {}
    lines += ["", texts.CLI_STATUS_HEARD_HEADER]
    for channel in config.enabled_channels():
        raw = store.get_setting(f"heard:{channel}")
        heard[channel] = raw
        lines.append(texts.CLI_STATUS_HEARD.format(channel=channel_name(channel),
                                                   at=at_text(raw)) if raw
                     else texts.CLI_STATUS_NOT_HEARD.format(
                         channel=channel_name(channel), cmd=cmd))

    since = _since_day()
    strangers = len(store.strangers_since(since))
    allowed = len(store.list_allowed())
    lines += ["", texts.CLI_STATUS_PEOPLE.format(allowed=allowed, strangers=strangers)]

    # Последние беды: канал мог погаснуть час назад, и тогда молчание бота
    # объясняется не «мост не запущен», а вот этой строкой.
    troubles = [row for row in store.journal_since("stopped", since)][-TROUBLES_SHOWN:]
    if troubles:
        lines += ["", texts.CLI_STATUS_TROUBLES_HEADER]
        for row in troubles:
            lines.append(texts.CLI_STATUS_TROUBLE_LINE.format(
                at=at_text(row["at"]),
                channel=channel_name(row["channel"]) if row["channel"] else "мост",
                text=(row["text"] or "").strip()))

    jobs = store.list_jobs(limit=JOBS_SHOWN)
    lines += ["", texts.CLI_STATUS_JOBS_HEADER]
    if not jobs:
        lines.append(texts.CLI_STATUS_JOBS_EMPTY)
    for row in jobs:
        lines.append(texts.CLI_STATUS_JOB_LINE.format(
            at=at_text(row["started_at"]),
            outcome=texts.JOB_OUTCOME.get(row["state"], row["state"]),
            how_long=alarm.how_long(row["duration_sec"] or 0),
            prompt=(row["prompt_head"] or "").strip().replace("\n", " ")[:60]))

    schedule = store.list_schedule()
    lines += ["", texts.CLI_STATUS_SCHEDULE_HEADER]
    if not schedule:
        lines.append(texts.CLI_STATUS_SCHEDULE_EMPTY)
    for row in schedule:
        spec = alarm.Spec.stored(row["spec"])
        lines.append(texts.CLI_STATUS_SCHEDULE_LINE.format(
            number=row["id"], when=spec.human() if spec else row["spec"],
            next=at_text(row["next_run_at"]) if row["next_run_at"] else "ещё не считала",
            off="" if row["enabled"] else " · выключена"))

    data = {"running": running, "pid": pid, "heard": heard, "allowed": allowed,
            "troubles": [{"at": r["at"], "channel": r["channel"], "text": r["text"]}
                         for r in troubles],
            "strangers_24h": strangers, "twins": [p for p, _ in twins],
            "jobs": [{"at": r["started_at"], "state": r["state"],
                      "prompt": (r["prompt_head"] or "")[:60]} for r in jobs],
            "schedule": [{"id": r["id"], "spec": r["spec"], "enabled": bool(r["enabled"]),
                          "next_run_at": r["next_run_at"]} for r in schedule]}
    return Answer(lines, data, code=0 if running else 1)


# --- самопроверка -----------------------------------------------------------

def run_doctor(config, store, live: bool = False, session=None,
               claude_bin: str | None = None) -> Answer:
    checks = doctor.checkup(config, session=session, claude_bin=claude_bin, live=live,
                            store=store)
    lines = doctor.table(checks)
    bad = [c for c in checks if not c.ok]
    lines += ["", doctor.verdict(checks, config)]
    data = {"checks": [{"ok": c.ok, "what": c.what, "hint": c.hint} for c in checks],
            "bad": len(bad)}
    return Answer(lines, data, code=1 if bad else 0)


# --- сказать во все чаты ----------------------------------------------------

def say(config, store, args, receivers=None) -> Answer:
    """Проверка доставки: то же, что делает мост сам, когда шлёт сводку."""
    text = " ".join(str(a) for a in args).strip()
    if not text:
        return Answer(texts.CLI_SAY_WHAT.format(cmd=how_to_call(config)), code=1)

    receivers = receivers if receivers is not None else _receivers(config, store)
    said, lines = [], []
    for channel, receiver in receivers.items():
        link = store.newest_link_of(channel)
        if link is None:
            continue
        try:
            for piece in narrator.chunk(text, getattr(receiver, "limit",
                                                      narrator.TELEGRAM_LIMIT)):
                receiver.send(link["chat_id"], piece)
            said.append(channel)
        except Exception as exc:                            # noqa: BLE001
            from .receivers.base import mask
            lines.append(texts.CLI_SAY_FAILED.format(
                channel=channel_name(channel), name=config.name,
                problem=mask(exc, config.secrets())))
    if not said:
        lines.append(texts.CLI_SAY_NOWHERE)
        return Answer(lines, {"said": []}, code=1)
    lines.insert(0, texts.CLI_SAID.format(
        where=", ".join(channel_name(c) for c in said)))
    return Answer(lines, {"said": said})


def _receivers(config, store) -> dict:
    """Приёмники только на отправку: обновления мы не спрашиваем, бота не отбираем."""
    from .receivers.max import MaxReceiver
    from .receivers.telegram import TelegramReceiver

    out = {}
    if config.telegram is not None:
        out["telegram"] = TelegramReceiver(token=config.telegram.token, store=store)
    if config.max is not None:
        out["max"] = MaxReceiver(token=config.max.token, store=store)
    return out


# --- разбор команды ---------------------------------------------------------

def answer(command: str, args, config, store, live: bool = False, session=None,
           receivers=None, claude_bin: str | None = None) -> Answer:
    if command == "knock":
        return knock(config, store)
    if command == "allow":
        return allow(config, store, args)
    if command == "deny":
        return deny(config, store, args)
    if command == "who":
        return who(config, store)
    if command == "status":
        return status(config, store)
    if command == "doctor":
        return run_doctor(config, store, live=live, session=session, claude_bin=claude_bin)
    if command == "say":
        return say(config, store, args, receivers=receivers)
    return Answer(texts.CLI_UNKNOWN_COMMAND.format(command=command), code=1)


def run(command: str, args=None, *, config, store=None, as_json: bool = False,
        live: bool = False, session=None, receivers=None,
        claude_bin: str | None = None) -> int:
    """Выполняет команду и печатает ответ. Своё соединение с базой закрывает само."""
    own = store is None
    store = store or Store(config.db_path).init()
    try:
        # Список своих в настройках — источник правды, а база — рабочая копия.
        # Мост мог ещё ни разу не запускаться, и тогда в базе пусто: доберём
        # оттуда, но ничего не выбросим — её читает живой демон.
        for channel in config.enabled_channels():
            store.merge_allowlist(channel, getattr(config.channel(channel),
                                                   "allowlist", None) or [])
        out = answer(command, list(args or []), config, store, live=live, session=session,
                     receivers=receivers, claude_bin=claude_bin)
    finally:
        if own:
            store.close()
    if as_json:
        print(json.dumps({"ok": out.code == 0, "text": out.text(), **out.data},
                         ensure_ascii=False, indent=2), flush=True)
    else:
        print(out.text(), flush=True)
    return out.code
