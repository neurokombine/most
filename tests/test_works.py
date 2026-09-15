"""Очередь работ: одна связка — одна работа, разные связки — параллельно."""
import time

import pytest

from bridge.executor import FakeExecutor
from bridge.works import WorkPool


@pytest.fixture()
def link(store):
    return store.upsert_link("telegram", 500, 0, project="buhgalter", session_id="s-1")


@pytest.fixture()
def other_link(store):
    return store.upsert_link("max", 900, 0, project="analitika", session_id="s-2")


def submit(pool, store, link, prompt="посчитай", workdir=None, resume=None):
    row = store.get_link_by_id(link["id"])
    return pool.submit(link=row, channel=row["channel"], chat_id=row["chat_id"],
                       prompt=prompt, workdir=workdir or "/tmp/project",
                       resume=row["session_started"] if resume is None else resume)


def test_finished_work_comes_back_through_collect(store, link):
    pool = WorkPool(executor=FakeExecutor(text="Остатки: 17"), store=store)
    submit(pool, store, link)
    pool.wait_idle()
    done = pool.collect()
    assert len(done) == 1
    assert done[0].result.text == "Остатки: 17"
    assert pool.collect() == []            # второй раз ту же работу не отдаёт
    assert store.list_jobs()[0]["state"] == "done"
    assert store.list_jobs()[0]["result_head"] == "Остатки: 17"


def test_one_link_one_work_at_a_time(store, link):
    pool = WorkPool(executor=FakeExecutor(delay=5), store=store)
    submit(pool, store, link)
    assert pool.busy(("telegram", 500, 0)) is True
    assert submit(pool, store, link) is None          # вторую не берём
    pool.stop(("telegram", 500, 0))
    pool.wait_idle()


def test_different_links_work_in_parallel(store, link, other_link):
    pool = WorkPool(executor=FakeExecutor(delay=1.5), store=store, max_parallel=2)
    assert submit(pool, store, link) is not None
    assert submit(pool, store, other_link) is not None
    assert pool.running() == 2
    pool.stop(("telegram", 500, 0))
    pool.stop(("max", 900, 0))
    pool.wait_idle()


def test_parallel_limit_holds_the_second_link_back(store, link, other_link):
    pool = WorkPool(executor=FakeExecutor(delay=5), store=store, max_parallel=1)
    assert submit(pool, store, link) is not None
    assert pool.has_free_slot() is False
    assert submit(pool, store, other_link) is None
    pool.stop(("telegram", 500, 0))
    pool.wait_idle()


def test_stop_ends_the_work_and_keeps_what_it_managed(store, link):
    pool = WorkPool(executor=FakeExecutor(delay=5, partial="Успела посчитать август."),
                    store=store)
    submit(pool, store, link)
    time.sleep(0.1)
    stopped = pool.stop(("telegram", 500, 0))
    assert stopped is not None
    pool.wait_idle()
    done = pool.collect()
    assert done[0].result.stopped is True
    assert "август" in done[0].result.partial
    assert store.list_jobs()[0]["state"] == "stopped"


def test_stop_on_an_idle_link_is_not_a_crash(store, link):
    pool = WorkPool(executor=FakeExecutor(), store=store)
    assert pool.stop(("telegram", 500, 0)) is None


def test_time_budget_is_marked_as_timeout_in_the_journal(store, link):
    pool = WorkPool(executor=FakeExecutor(delay=5, timeout=0.3, partial="Начала считать."),
                    store=store)
    submit(pool, store, link)
    pool.wait_idle(timeout=5)
    done = pool.collect()
    assert done[0].result.timed_out is True
    assert store.list_jobs()[0]["state"] == "timeout"


def test_first_work_starts_the_session_and_the_second_resumes_it(store, link):
    executor = FakeExecutor(text="Готово.")
    pool = WorkPool(executor=executor, store=store)
    submit(pool, store, link)
    pool.wait_idle()
    pool.collect()
    assert store.get_link_by_id(link["id"])["session_started"] == 1

    submit(pool, store, link)
    pool.wait_idle()
    pool.collect()
    assert executor.calls[0]["resume"] is False
    assert executor.calls[1]["resume"] is True
    assert executor.calls[1]["session_id"] == "s-1"


def test_lost_session_starts_a_new_one_and_says_so(store, link):
    executor = FakeExecutor(text="Готово.", lose_session=True)
    pool = WorkPool(executor=executor, store=store)
    submit(pool, store, link, resume=True)
    pool.wait_idle()
    done = pool.collect()

    assert done[0].session_restarted is True
    assert done[0].result.ok is True                  # вторая попытка отработала
    assert executor.calls[0]["resume"] is True
    assert executor.calls[1]["resume"] is False
    assert executor.calls[1]["session_id"] != "s-1"   # новый ключ разговора
    assert store.get_link_by_id(link["id"])["session_id"] == executor.calls[1]["session_id"]


def test_failed_work_is_written_as_failed(store, link):
    pool = WorkPool(executor=FakeExecutor(text="claude не найден", ok=False, exit_code=1),
                    store=store)
    submit(pool, store, link)
    pool.wait_idle()
    done = pool.collect()
    assert done[0].result.ok is False
    assert store.list_jobs()[0]["state"] == "failed"


def test_broken_executor_does_not_leave_the_link_busy_forever(store, link):
    class Broken(FakeExecutor):
        def run(self, *a, **kw):
            raise RuntimeError("исполнитель сломался")

    pool = WorkPool(executor=Broken(), store=store)
    submit(pool, store, link)
    pool.wait_idle()
    done = pool.collect()
    assert pool.busy(("telegram", 500, 0)) is False
    assert done[0].result.ok is False
    assert store.list_jobs()[0]["state"] == "failed"
