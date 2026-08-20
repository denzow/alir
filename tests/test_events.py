"""イベントバス(pub/sub)のテスト。"""

from __future__ import annotations

from alir import events


def test_publish_reaches_all_subscribers() -> None:
    bus = events.EventBus()
    received: list[tuple[str, events.Event]] = []
    bus.subscribe(lambda e: received.append(("a", e)))
    bus.subscribe(lambda e: received.append(("b", e)))
    event = events.Event(kind=events.KIND_QUESTION, message="hello")
    bus.publish(event)
    assert received == [("a", event), ("b", event)]


def test_subscriber_failure_does_not_block_others() -> None:
    bus = events.EventBus()
    received: list[events.Event] = []

    def broken(event: events.Event) -> None:
        raise RuntimeError("boom")

    bus.subscribe(broken)
    bus.subscribe(received.append)
    bus.publish(events.Event(kind=events.KIND_ISSUE_FAILED, message="x"))
    assert len(received) == 1


def test_unsubscribe_stops_delivery() -> None:
    bus = events.EventBus()
    received: list[events.Event] = []
    unsubscribe = bus.subscribe(received.append)
    bus.publish(events.Event(kind=events.KIND_QUESTION, message="1"))
    unsubscribe()
    bus.publish(events.Event(kind=events.KIND_QUESTION, message="2"))
    assert [e.message for e in received] == ["1"]


def test_unsubscribe_twice_is_noop() -> None:
    bus = events.EventBus()
    unsubscribe = bus.subscribe(lambda e: None)
    unsubscribe()
    unsubscribe()  # 2 回目も例外にならない


def test_publish_without_subscribers_is_noop() -> None:
    events.EventBus().publish(events.Event(kind=events.KIND_RETRY_EXHAUSTED, message="x"))
