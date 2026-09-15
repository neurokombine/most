"""Цикл демона: два канала в одном процессе, три класса ошибок по поведению."""
import pytest
import requests

from bridge.daemon import Bridge, EXIT_STALE
from bridge.executor import FakeExecutor
from bridge.receivers.base import BridgeConflict, Incoming, RateLimited, TokenRejected


class FakeReceiver:
    def __init__(self, channel, batches=None, limit=4096):
        self.channel = channel
        self.limit = limit
        self.batches = list(batches or [])
        self.sent = []
        self.polls = 0

    def poll_once(self):
        self.polls += 1
        if not self.batches:
            return []
        nxt = self.batches.pop(0)
        if isinstance(nxt, Exception):
            raise nxt
        return nxt

    def send(self, chat_id, text):
        self.sent.append((chat_id, text))


def msg(channel, text="посчитай", user_id=111, chat_id=500):
    return Incoming(channel=channel, chat_id=chat_id, user_id=user_id, text=text,
                    thread_id=0, raw={})


@pytest.fixture()
def bridge(config, store):
    store.sync_allowlist("telegram", [111])
    store.sync_allowlist("max", [222])
    config.parallel = 2          # два чата за раз: иначе второму честно скажут «занята»
    tg = FakeReceiver("telegram", [[msg("telegram")]])
    mx = FakeReceiver("max", [[msg("max", user_id=222, chat_id=900)]], limit=4000)
    b = Bridge(config=config, store=store, executor=FakeExecutor(text="Готово."),
               receivers={"telegram": tg, "max": mx}, sleeper=lambda s: None)
    return b


def drain(bridge):
    """Работа идёт своим потоком; ответ уходит в чат следующим заходом цикла."""
    bridge.pool.wait_idle()
    bridge.deliver()


def test_tick_answers_into_the_channel_the_question_came_from(bridge):
    bridge.tick()
    drain(bridge)
    tg = bridge.receivers["telegram"]
    mx = bridge.receivers["max"]
    assert tg.sent[-1] == (500, "Готово.")
    assert mx.sent[-1] == (900, "Готово.")
    assert "работу" in tg.sent[0][1]          # сначала «взяла в работу»


def test_stranger_gets_no_answer_at_all(config, store):
    store.sync_allowlist("telegram", [111])
    tg = FakeReceiver("telegram", [[msg("telegram", user_id=999)]])
    b = Bridge(config=config, store=store, executor=FakeExecutor(),
               receivers={"telegram": tg}, sleeper=lambda s: None)
    b.tick()
    assert tg.sent == []
    assert store.recent_strangers()[0]["user_id"] == 999


def test_conflict_stops_only_its_own_channel(config, store):
    store.sync_allowlist("telegram", [111])
    store.sync_allowlist("max", [222])
    tg = FakeReceiver("telegram", [BridgeConflict("вебхук")])
    mx = FakeReceiver("max", [[msg("max", user_id=222, chat_id=900)]])
    b = Bridge(config=config, store=store, executor=FakeExecutor(text="Готово."),
               receivers={"telegram": tg, "max": mx}, sleeper=lambda s: None)
    b.tick()
    b.pool.wait_idle()
    b.deliver()
    assert "telegram" not in b.receivers
    assert mx.sent[-1] == (900, "Готово.")
    assert b.alive() is True


def test_token_rejected_stops_its_channel_and_is_written_down(config, store):
    tg = FakeReceiver("telegram", [TokenRejected("401")])
    b = Bridge(config=config, store=store, executor=FakeExecutor(),
               receivers={"telegram": tg}, sleeper=lambda s: None)
    b.tick()
    assert b.alive() is False
    kinds = [row["kind"] for row in store.recent_journal()]
    assert "stopped" in kinds
    assert any("токен" in (row["text"] or "").lower() for row in store.recent_journal())


def test_rate_limit_waits_exactly_as_asked(config, store):
    slept = []
    tg = FakeReceiver("telegram", [RateLimited("подожди", retry_after=17), []])
    b = Bridge(config=config, store=store, executor=FakeExecutor(),
               receivers={"telegram": tg}, sleeper=slept.append)
    b.tick()
    assert slept == [17]
    assert "telegram" in b.receivers


def test_network_trouble_is_survived(config, store):
    slept = []
    tg = FakeReceiver("telegram", [requests.exceptions.ConnectionError("нет сети"), []])
    b = Bridge(config=config, store=store, executor=FakeExecutor(),
               receivers={"telegram": tg}, sleeper=slept.append)
    b.tick()
    assert slept and slept[0] >= 5
    assert "telegram" in b.receivers


def test_one_broken_message_does_not_stop_the_tick(config, store):
    store.sync_allowlist("telegram", [111])

    class Boom(FakeExecutor):
        def run(self, prompt, workdir, session_id=None, resume=False, handle=None):
            raise RuntimeError("внутри всё сломалось")

    tg = FakeReceiver("telegram", [[msg("telegram"), msg("telegram", text="вторая")]])
    b = Bridge(config=config, store=store, executor=Boom(),
               receivers={"telegram": tg}, sleeper=lambda s: None)
    b.tick()
    b.pool.wait_idle()
    b.deliver()
    assert tg.polls == 1
    assert len(tg.sent) >= 2                       # ответили по-человечески
    assert all("Traceback" not in text for _, text in tg.sent)


def test_code_change_asks_for_a_restart(config, store, monkeypatch):
    tg = FakeReceiver("telegram", [[], []])
    b = Bridge(config=config, store=store, executor=FakeExecutor(),
               receivers={"telegram": tg}, sleeper=lambda s: None)
    monkeypatch.setattr(b, "code_changed", lambda: True)
    assert b.run(max_ticks=3) == EXIT_STALE


def test_loop_ends_quietly_when_no_channel_is_left(config, store):
    tg = FakeReceiver("telegram", [TokenRejected("401")])
    b = Bridge(config=config, store=store, executor=FakeExecutor(),
               receivers={"telegram": tg}, sleeper=lambda s: None)
    assert b.run(max_ticks=5) == 0
    assert tg.polls == 1


# --- этап 2: работа идёт своим потоком, перезагрузка не проходит молча --------

def test_the_loop_keeps_listening_while_the_work_is_running(config, store):
    store.sync_allowlist("telegram", [111])
    tg = FakeReceiver("telegram", [[msg("telegram", text="долгая задача")],
                                   [msg("telegram", text="стоп")]])
    b = Bridge(config=config, store=store,
               executor=FakeExecutor(delay=5, partial="Успела немного."),
               receivers={"telegram": tg}, sleeper=lambda s: None)
    b.tick()                                   # задачу взяли в работу
    assert b.pool.running() == 1
    b.tick()                                   # и услышали «стоп», пока она шла
    b.pool.wait_idle()
    b.deliver()
    said = " ".join(text for _, text in tg.sent)
    assert "Остановила по вашей просьбе" in said
    assert "Успела немного" in said


def test_after_a_restart_the_unfinished_work_is_confessed(config, store):
    store.sync_allowlist("telegram", [111])
    link = store.upsert_link("telegram", 500, 0, project="buhgalter")
    store.start_job(link["id"], "telegram", 500, "s-1",
                    "собери отчёт по августу и положи его в папку результаты", "")

    tg = FakeReceiver("telegram")
    b = Bridge(config=config, store=store, executor=FakeExecutor(),
               receivers={"telegram": tg}, sleeper=lambda s: None)
    assert b.recover() == 1

    said = " ".join(text for _, text in tg.sent)
    assert "прервали" in said
    assert "собери отчёт по августу" in said
    assert store.list_jobs()[0]["state"] == "interrupted"
    assert store.get_link("telegram", 500, 0)["session_id"]      # связка на месте
    assert b.recover() == 0                                      # второй раз не повторяем


def test_recovery_happens_on_start_of_the_loop(config, store):
    link = store.upsert_link("telegram", 500, 0, project="buhgalter")
    store.start_job(link["id"], "telegram", 500, "s-1", "недоделанное", "")
    tg = FakeReceiver("telegram", [[], []])
    b = Bridge(config=config, store=store, executor=FakeExecutor(),
               receivers={"telegram": tg}, sleeper=lambda s: None)
    b.run(max_ticks=1)
    assert any("прервали" in text for _, text in tg.sent)


# --- этап 3: во все настроенные каналы разом --------------------------------

def test_broadcast_reaches_both_messengers(bridge):
    bridge.tick()                       # оба чата знакомы мосту
    drain(bridge)
    delivered = bridge.broadcast("сводка за сегодня")
    assert delivered == 2
    assert bridge.receivers["telegram"].sent[-1] == (500, "сводка за сегодня")
    assert bridge.receivers["max"].sent[-1] == (900, "сводка за сегодня")


def test_broadcast_with_a_file_sends_the_file(bridge, tmp_path):
    path = tmp_path / "сводка.txt"
    path.write_text("итоги", encoding="utf-8")
    bridge.tick()
    drain(bridge)
    for receiver in bridge.receivers.values():
        receiver.files = []
        receiver.send_file = (lambda r: lambda chat_id, p, caption="":
                              r.files.append((chat_id, p, caption)))(receiver)

    assert bridge.broadcast("вот она", file=path) == 2
    assert bridge.receivers["telegram"].files[0][0] == 500
    assert bridge.receivers["max"].files[0][2] == "вот она"


def test_broadcast_without_a_single_chat_still_finds_telegram_by_allowlist(config, store):
    store.sync_allowlist("telegram", [111])
    tg = FakeReceiver("telegram")
    b = Bridge(config=config, store=store, executor=FakeExecutor(),
               receivers={"telegram": tg}, sleeper=lambda s: None)
    assert b.broadcast("сводка") == 1
    assert tg.sent[0] == (111, "сводка")        # в личке chat_id и есть ваш id


def test_broadcast_takes_the_freshest_chat_of_a_channel(bridge):
    bridge.tick()
    drain(bridge)
    bridge.router.handle(msg("telegram", chat_id=777))      # свежая связка
    bridge.pool.wait_idle()
    bridge.deliver()
    bridge.receivers["telegram"].sent = []
    bridge.broadcast("сводка")
    assert [chat for chat, _ in bridge.receivers["telegram"].sent] == [777]


def test_a_dead_channel_does_not_stop_the_others(bridge):
    bridge.tick()
    drain(bridge)

    def refuse(chat_id, text):
        raise RuntimeError("канал лёг")

    bridge.receivers["telegram"].send = refuse
    assert bridge.broadcast("сводка") == 1              # Max получил
    assert store_has_error(bridge.store)


def store_has_error(store):
    return any(row["kind"] == "error" for row in store.recent_journal())


def test_incoming_file_reaches_the_router_with_its_receiver(bridge, tmp_path):
    from bridge.receivers.base import Attachment

    project = next(p for p in bridge.config.projects_dir.iterdir() if p.is_dir())
    tg = bridge.receivers["telegram"]
    tg.batches = [[Incoming(channel="telegram", chat_id=500, user_id=111, text="",
                            thread_id=0, raw={},
                            attachments=[Attachment(kind="file", file_id="f",
                                                    file_name="акт.pdf", size=4)])]]
    tg.fetch = lambda attachment: b"body"
    bridge.tick()
    drain(bridge)
    assert (project / "входящие" / "акт.pdf").exists()
    assert "входящие" in "\n".join(text for _, text in tg.sent)


def test_the_whole_way_of_a_file_through_the_real_telegram_receiver(config, store, tmp_path):
    """Сквозь настоящий разбор апдейта и настоящую сборку запроса, но без сети."""
    from bridge.receivers.telegram import TelegramReceiver
    from tests.fakes import FakeResponse, FakeSession

    projects = tmp_path / "папки проектов"
    (projects / "бухгалтерия за сентябрь").mkdir(parents=True)
    config.projects_dir = projects
    store.sync_allowlist("telegram", [111])

    session = FakeSession([
        # 1 · пришёл документ
        FakeResponse(200, {"ok": True, "result": [{"update_id": 1, "message": {
            "message_id": 1, "from": {"id": 111}, "chat": {"id": 500, "type": "private"},
            "document": {"file_id": "f1", "file_name": "акт сверки.pdf",
                         "file_size": 5}}}]}),
        FakeResponse(200, {"ok": True, "result": {"file_path": "documents/f1.pdf"}}),
        FakeResponse(200, content=b"12345"),
        FakeResponse(200, {"ok": True}),                     # ответ «положила»
        # 2 · «пришли мне акт»
        FakeResponse(200, {"ok": True, "result": [{"update_id": 2, "message": {
            "message_id": 2, "from": {"id": 111}, "chat": {"id": 500, "type": "private"},
            "text": "пришли мне акт"}}]}),
        FakeResponse(200, {"ok": True, "result": {"message_id": 9}}),
    ])
    receiver = TelegramReceiver(token="123:abc", store=store, session=session)
    bridge = Bridge(config=config, store=store, executor=FakeExecutor(),
                    receivers={"telegram": receiver}, sleeper=lambda s: None)

    bridge.tick()
    saved = projects / "бухгалтерия за сентябрь" / "входящие" / "акт сверки.pdf"
    assert saved.read_bytes() == b"12345"

    bridge.tick()
    out = [c for c in session.calls if "sendDocument" in c["url"]]
    assert len(out) == 1
    assert out[0]["files"]["document"][0] == "акт сверки.pdf"


# --- этап 4: голос в главном цикле ------------------------------------------

def voiced(bridge):
    """Подставной голос вместо piper: сам мост его не отличает."""
    from bridge import voice

    bridge.router.mouth = voice.FakeSpeaker()
    for receiver in bridge.receivers.values():
        receiver.voices = []
        receiver.send_voice = (lambda r: lambda chat_id, p, caption="":
                               r.voices.append((chat_id, p, caption)))(receiver)
    return bridge


def test_answer_asked_aloud_goes_out_as_a_voice_message(bridge):
    voiced(bridge)
    bridge.receivers["telegram"].batches = [[msg("telegram", "ответь голосом: сколько осталось")]]
    bridge.receivers["max"].batches = []
    bridge.tick()
    drain(bridge)
    assert bridge.receivers["telegram"].voices               # ответ прочитан вслух
    assert bridge.receivers["telegram"].sent[-1] == (500, "Готово.")   # и текст тоже


def test_ordinary_answer_stays_text_only(bridge):
    voiced(bridge)
    bridge.tick()
    drain(bridge)
    assert bridge.receivers["telegram"].voices == []


def test_summary_is_read_aloud_when_the_setting_says_so(bridge):
    voiced(bridge)
    bridge.config.voice.reply = True
    bridge.tick()
    drain(bridge)
    assert bridge.broadcast("сводка за сегодня", aloud=True) == 2
    assert bridge.receivers["telegram"].voices
    assert bridge.receivers["max"].voices
    assert bridge.receivers["max"].sent[-1] == (900, "сводка за сегодня")


def test_summary_stays_silent_until_asked_in_the_settings(bridge):
    voiced(bridge)
    bridge.config.voice.reply = False
    bridge.tick()
    drain(bridge)
    bridge.broadcast("сводка за сегодня", aloud=True)
    assert bridge.receivers["telegram"].voices == []


# --- этап 6: осиротевшая нейросеть и второй мост -----------------------------

def test_an_orphan_neural_net_is_put_out_on_start(config, store, monkeypatch):
    """Мост упал, а `claude` в папке остался жить: в следующий запуск гасим его."""
    link = store.upsert_link("telegram", 500, 0, project="buhgalter")
    job_id = store.start_job(link["id"], "telegram", 500, "s-1", "считай долго", "")
    store.set_job_pid(job_id, 4242)

    killed = []
    monkeypatch.setattr("bridge.lock.looks_like_claude", lambda pid, command=None: True)
    monkeypatch.setattr("bridge.lock.kill_tree", lambda pid: killed.append(int(pid)) or True)

    tg = FakeReceiver("telegram")
    b = Bridge(config=config, store=store, executor=FakeExecutor(),
               receivers={"telegram": tg}, sleeper=lambda s: None)
    b.recover()

    assert killed == [4242]
    assert store.list_jobs()[0]["state"] == "interrupted"
    assert any("4242" in (row["text"] or "") for row in store.recent_journal())


def test_a_stranger_process_with_the_same_number_is_left_alone(config, store, monkeypatch):
    """Номера процессов переиспользуются: чужую программу не гасим."""
    link = store.upsert_link("telegram", 500, 0, project="buhgalter")
    job_id = store.start_job(link["id"], "telegram", 500, "s-1", "считай долго", "")
    store.set_job_pid(job_id, 4242)

    killed = []
    monkeypatch.setattr("bridge.lock.looks_like_claude", lambda pid, command=None: False)
    monkeypatch.setattr("bridge.lock.kill_tree", lambda pid: killed.append(pid) or True)

    b = Bridge(config=config, store=store, executor=FakeExecutor(),
               receivers={"telegram": FakeReceiver("telegram")}, sleeper=lambda s: None)
    b.recover()
    assert killed == []
    assert store.list_jobs()[0]["state"] == "interrupted"


def test_the_bridge_remembers_when_it_last_heard_the_messenger(bridge, store):
    """«Мост жив, а мессенджер молчит» — про это отвечает `status`."""
    bridge.tick()
    assert store.get_setting("heard:telegram")
    assert store.get_setting("heard:max")


def test_a_channel_that_answers_with_a_error_is_not_counted_as_heard(config, store):
    tg = FakeReceiver("telegram", [requests.exceptions.ConnectionError("нет сети")])
    b = Bridge(config=config, store=store, executor=FakeExecutor(),
               receivers={"telegram": tg}, sleeper=lambda s: None)
    b.tick()
    assert store.get_setting("heard:telegram") is None
