"""Issue レジストリのテスト。"""

from __future__ import annotations

from pathlib import Path

import pytest

from alir import db, registry
from alir.registry import RegistryError


@pytest.fixture
def conn(tmp_path: Path):  # type: ignore[no-untyped-def]
    return db.connect(tmp_path / "data")


URL = "https://github.com/denzow/alir/issues/12"


def test_add_registers_queued_issue(conn) -> None:  # type: ignore[no-untyped-def]
    issue = registry.add(conn, url=URL, workdir="/tmp/alir", priority=5)
    assert issue.id == 1
    assert issue.repo == "denzow/alir"
    assert issue.number == 12
    assert issue.ref == "denzow/alir#12"
    assert issue.status == registry.STATUS_QUEUED
    assert issue.priority == 5


def test_add_defaults_to_auto_mode_without_note(conn) -> None:  # type: ignore[no-untyped-def]
    issue = registry.add(conn, url=URL, workdir="/tmp/alir")
    assert issue.mode == registry.MODE_AUTO
    assert issue.note is None


def test_add_stores_mode_and_note(conn) -> None:  # type: ignore[no-untyped-def]
    issue = registry.add(
        conn, url=URL, workdir="/tmp/alir", mode=registry.MODE_REFINE, note="仕様だけ詰めたい"
    )
    assert issue.mode == registry.MODE_REFINE
    assert issue.note == "仕様だけ詰めたい"
    # 再取得でも保持される
    assert registry.get(conn, issue.id).note == "仕様だけ詰めたい"


def test_add_rejects_unknown_mode(conn) -> None:  # type: ignore[no-untyped-def]
    with pytest.raises(RegistryError):
        registry.add(conn, url=URL, workdir="/tmp", mode="review")


def test_add_treats_blank_note_as_none(conn) -> None:  # type: ignore[no-untyped-def]
    issue = registry.add(conn, url=URL, workdir="/tmp", note="   ")
    assert issue.note is None


def test_add_rejects_non_issue_url(conn) -> None:  # type: ignore[no-untyped-def]
    with pytest.raises(RegistryError):
        registry.add(conn, url="https://github.com/denzow/alir/pull/12", workdir="/tmp")


def test_add_rejects_duplicate_unfinished(conn) -> None:  # type: ignore[no-untyped-def]
    registry.add(conn, url=URL, workdir="/tmp")
    with pytest.raises(RegistryError):
        registry.add(conn, url=URL, workdir="/tmp")


def test_add_allows_reregister_after_done(conn) -> None:  # type: ignore[no-untyped-def]
    issue = registry.add(conn, url=URL, workdir="/tmp")
    registry.set_status(conn, issue.id, registry.STATUS_DONE)
    assert registry.add(conn, url=URL, workdir="/tmp").id == 2


def test_next_queued_respects_priority(conn) -> None:  # type: ignore[no-untyped-def]
    registry.add(conn, url=URL, workdir="/tmp", priority=0)
    high = registry.add(
        conn, url="https://github.com/denzow/alir/issues/13", workdir="/tmp", priority=10
    )
    nxt = registry.next_queued(conn)
    assert nxt is not None
    assert nxt.id == high.id


def test_next_queued_returns_none_when_empty(conn) -> None:  # type: ignore[no-untyped-def]
    assert registry.next_queued(conn) is None


def test_set_status_updates_and_keeps_fields(conn) -> None:  # type: ignore[no-untyped-def]
    issue = registry.add(conn, url=URL, workdir="/tmp")
    updated = registry.set_status(
        conn, issue.id, registry.STATUS_PARKED, session_id="sess-1", branch="alir/issue-12"
    )
    assert updated.status == registry.STATUS_PARKED
    assert updated.session_id == "sess-1"

    updated = registry.set_status(conn, issue.id, registry.STATUS_QUEUED)
    assert updated.session_id == "sess-1"
    assert updated.branch == "alir/issue-12"


def test_set_status_rejects_unknown_status(conn) -> None:  # type: ignore[no-untyped-def]
    issue = registry.add(conn, url=URL, workdir="/tmp")
    with pytest.raises(RegistryError):
        registry.set_status(conn, issue.id, "sleeping")


def test_reorder_sets_new_order(conn) -> None:  # type: ignore[no-untyped-def]
    a = registry.add(conn, url=URL, workdir="/tmp")
    b = registry.add(conn, url="https://github.com/denzow/alir/issues/13", workdir="/tmp")
    c = registry.add(conn, url="https://github.com/denzow/alir/issues/14", workdir="/tmp")

    registry.reorder(conn, [c.id, a.id, b.id])
    assert [i.id for i in registry.list_issues(conn)] == [c.id, a.id, b.id]


def test_reorder_rejects_mismatched_ids(conn) -> None:  # type: ignore[no-untyped-def]
    a = registry.add(conn, url=URL, workdir="/tmp")
    registry.add(conn, url="https://github.com/denzow/alir/issues/13", workdir="/tmp")
    with pytest.raises(RegistryError):
        registry.reorder(conn, [a.id])


def test_reorder_rejects_non_reorderable_id(conn) -> None:  # type: ignore[no-untyped-def]
    a = registry.add(conn, url=URL, workdir="/tmp")
    done = registry.add(conn, url="https://github.com/denzow/alir/issues/13", workdir="/tmp")
    registry.set_status(conn, done.id, registry.STATUS_DONE)
    with pytest.raises(RegistryError):
        registry.reorder(conn, [done.id, a.id])


def test_reorder_does_not_touch_finished_priority(conn) -> None:  # type: ignore[no-untyped-def]
    done = registry.add(conn, url=URL, workdir="/tmp", priority=99)
    registry.set_status(conn, done.id, registry.STATUS_DONE)
    a = registry.add(conn, url="https://github.com/denzow/alir/issues/13", workdir="/tmp")
    b = registry.add(conn, url="https://github.com/denzow/alir/issues/14", workdir="/tmp")
    registry.reorder(conn, [b.id, a.id])
    assert registry.get(conn, done.id).priority == 99
    assert [i.id for i in registry.list_issues(conn, status=registry.STATUS_QUEUED)] == [b.id, a.id]


def test_list_workdirs_dedupes_latest_first(conn) -> None:  # type: ignore[no-untyped-def]
    registry.add(conn, url=URL, workdir="/tmp/a")
    registry.add(conn, url="https://github.com/denzow/alir/issues/13", workdir="/tmp/b")
    registry.add(conn, url="https://github.com/denzow/alir/issues/14", workdir="/tmp/a")
    assert registry.list_workdirs(conn) == ["/tmp/a", "/tmp/b"]


def test_add_stores_title(conn) -> None:  # type: ignore[no-untyped-def]
    issue = registry.add(conn, url=URL, workdir="/tmp", title="タイトル")
    assert issue.title == "タイトル"
