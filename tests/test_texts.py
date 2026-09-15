"""Как мост разговаривает. Проверяем не смысл, а тон: он тут такая же часть дела.

Читать это будет человек сорока с лишним лет, который не технарь и который уже
встревожен («бот молчит»). Значит: никаких номеров ошибок и английских слов из
консоли, никаких восклицаний и «упс», и на каждую беду — что случилось и что
сделать. Латиница допускается только там, где это имя файла или команда,
которую человек должен увидеть в точности.
"""
import re

from bridge import texts

ALL = {name: value for name, value in vars(texts).items()
       if name.isupper() and isinstance(value, str)}

# Латиница, которую человек и правда видит на экране: имена файлов, команды,
# названия мессенджеров и программ. Всё остальное в русском тексте — жаргон.
ALLOWED_LATIN = {
    "config", "yaml", "most", "bridge", "python", "m", "name", "claude", "bash",
    "scripts", "setup", "sh", "voice", "chmod", "systemctl", "user", "start",
    "restart", "telegram", "max", "botfather", "masterbot", "allow", "last",
    "deny", "knock", "who", "status", "doctor", "say", "allowlist", "enabled",
    "false", "true", "parallel", "timezone", "json", "kill", "timedatectl",
    "set", "ntp", "jobs", "models", "faster", "whisper", "piper", "ru", "irina",
    "medium", "sonnet", "opus", "id", "at", "path", "cmd", "pid", "channel",
    "user_id", "number", "next", "what", "hint", "n", "text", "prompt", "name",
    "when", "how_long", "outcome", "seconds", "budget", "size", "limit", "files",
    "folder", "project", "result", "error", "count", "times", "who", "spent",
    "cost", "known", "head", "found", "partial", "problem", "used", "day",
    "time", "number", "off", "where", "all", "bad", "strangers", "allowed",
    "parallel", "e", "g", "md", "tzdata", "token", "live",
}

ERROR_NUMBERS = re.compile(r"\b(4\d\d|5\d\d|13[0-9])\b")
LATIN_WORD = re.compile(r"[A-Za-z][A-Za-z_]{0,30}")
PLACEHOLDER = re.compile(r"\{[a-z_]+\}")


def words_of(value: str) -> list[str]:
    return [w.lower() for w in LATIN_WORD.findall(PLACEHOLDER.sub(" ", value))]


def test_the_bridge_never_shouts():
    for name, value in ALL.items():
        assert "!" not in value, name
        assert "😊" not in value and "🙂" not in value, name


def test_there_are_no_oops_and_no_baby_talk():
    for name, value in ALL.items():
        low = value.lower()
        for bad in (r"упс", r"\bой\b", r"\bура\b", r"к сожалению", r"извините",
                    r"\bдруз[ья]", r"котик"):
            assert not re.search(bad, low), f"{name}: {bad}"


def test_no_numbers_of_errors_are_shown_to_the_person():
    """«Ошибка 409» человеку не говорит ничего. Говорит «бота слушает кто-то ещё»."""
    for name, value in ALL.items():
        found = ERROR_NUMBERS.search(value)
        assert not found, f"{name}: {found.group(0) if found else ''}"


def test_no_words_from_the_console():
    for name, value in ALL.items():
        low = value.lower()
        for bad in ("exit code", "traceback", "stacktrace", "stderr", "stdout",
                    "timeout", "webhook", "вебхук", "long polling", "лонг-поллинг",
                    "инстанс", "демон", "таймаут", "апдейт", "эксепшн"):
            assert bad not in low, f"{name}: {bad}"


def test_latin_words_are_only_names_of_files_and_commands():
    for name, value in ALL.items():
        for word in words_of(value):
            assert word in ALLOWED_LATIN, f"{name}: {word}"


def test_every_trouble_says_what_to_do():
    """На беду мало сказать «не вышло»: рядом должно стоять действие."""
    doing = ("скажите", "напишите", "проверьте", "впишите", "поставьте", "уберите",
             "остановите", "запустите", "войдите", "возьмите", "вернитесь",
             "попробуйте", "создайте", "заведите", "почините", "перезапустите",
             "поправьте", "закройте", "пришлите", "покажите", "повторите",
             "наговорите", "положите", "дождитесь", "делать", "держать", "смотрите",
             "выключить", "включить", "забрать", "заберите", "разбить",
             "подождите", "жду", "вернитесь")
    troubles = [name for name in ALL
                if any(mark in name for mark in ("REJECTED", "CONFLICT", "FAILED",
                                                 "MISSING", "BROKEN", "TOO_BIG",
                                                 "LOOSE", "NOT_SET_UP", "NOT_FOUND"))]
    assert troubles
    for name in troubles:
        low = ALL[name].lower()
        assert any(word in low for word in doing), name
