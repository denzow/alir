"""ラベル付き Issue の取り込みのテスト。gh の検索は差し替える。"""

from __future__ import annotations

from pathlib import Path

import pytest

from alir import db, importer, registry
from alir.importer import ImporterError

URL = "https://github.com/denzow/alir/issues/12"


@pytest.fixture
def conn(tmp_path: Path):  # type: ignore[no-untyped-def]
    return db.connect(tmp_path / "data")


@pytest.fixture
def workdir(tmp_path: Path) -> Path:
    path = tmp_path / "repo"
    path.mkdir()
    return path


def test_add_and_list_targets(conn, workdir: Path) -> None:  # type: ignore[no-untyped-def]
    importer.add_target(conn, workdir=str(workdir), label="alir")
    targets = importer.list_targets(conn)
    assert targets == [importer.ImportTarget(workdir=str(workdir.resolve()), label="alir")]


def test_add_target_rejects_missing_workdir(conn, tmp_path: Path) -> None:  # type: ignore[no-untyped-def]
    with pytest.raises(ImporterError):
        importer.add_target(conn, workdir=str(tmp_path / "missing"), label="alir")


def test_add_target_rejects_empty_label(conn, workdir: Path) -> None:  # type: ignore[no-untyped-def]
    with pytest.raises(ImporterError):
        importer.add_target(conn, workdir=str(workdir), label="  ")


def test_add_target_rejects_duplicate(conn, workdir: Path) -> None:  # type: ignore[no-untyped-def]
    importer.add_target(conn, workdir=str(workdir), label="alir")
    with pytest.raises(ImporterError):
        importer.add_target(conn, workdir=str(workdir), label="alir")


def test_remove_target(conn, workdir: Path) -> None:  # type: ignore[no-untyped-def]
    importer.add_target(conn, workdir=str(workdir), label="alir")
    importer.remove_target(conn, workdir=str(workdir), label="alir")
    assert importer.list_targets(conn) == []


def test_remove_unknown_target_raises(conn, workdir: Path) -> None:  # type: ignore[no-untyped-def]
    with pytest.raises(ImporterError):
        importer.remove_target(conn, workdir=str(workdir), label="alir")


def test_find_target(conn, workdir: Path) -> None:  # type: ignore[no-untyped-def]
    importer.add_target(conn, workdir=str(workdir), label="alir")
    target = importer.find_target(conn, workdir=str(workdir), label="alir")
    assert target == importer.ImportTarget(workdir=str(workdir.resolve()), label="alir")


def test_find_unknown_target_raises(conn, workdir: Path) -> None:  # type: ignore[no-untyped-def]
    with pytest.raises(ImporterError):
        importer.find_target(conn, workdir=str(workdir), label="alir")


def test_import_interval_defaults_to_disabled(conn) -> None:  # type: ignore[no-untyped-def]
    assert importer.import_interval(conn) == 0.0


def test_set_import_interval(conn) -> None:  # type: ignore[no-untyped-def]
    importer.set_import_interval(conn, 300)
    assert importer.import_interval(conn) == 300.0
    importer.set_import_interval(conn, 0)
    assert importer.import_interval(conn) == 0.0


def test_set_import_interval_rejects_too_short(conn) -> None:  # type: ignore[no-untyped-def]
    with pytest.raises(ImporterError):
        importer.set_import_interval(conn, importer.MIN_INTERVAL - 1)
    with pytest.raises(ImporterError):
        importer.set_import_interval(conn, -1)


def test_run_import_adds_issues_as_queued(conn, workdir: Path) -> None:  # type: ignore[no-untyped-def]
    importer.add_target(conn, workdir=str(workdir), label="alir")

    def fetch(wd: str, label: str) -> list[dict[str, str]]:
        assert wd == str(workdir.resolve())
        assert label == "alir"
        return [{"url": URL, "title": "自動取り込みされる Issue"}]

    outcome = importer.run_import(conn, fetch=fetch)
    assert [i.url for i in outcome.imported] == [URL]
    assert outcome.errors == []
    items = registry.list_issues(conn)
    assert [(i.url, i.status, i.title) for i in items] == [
        (URL, registry.STATUS_QUEUED, "自動取り込みされる Issue")
    ]


def test_run_import_appends_to_queue_tail(conn, workdir: Path) -> None:  # type: ignore[no-untyped-def]
    first = registry.add(conn, url="https://github.com/denzow/alir/issues/1", workdir=str(workdir))
    importer.add_target(conn, workdir=str(workdir), label="alir")
    importer.run_import(conn, fetch=lambda wd, label: [{"url": URL, "title": "t"}])
    assert registry.next_queued(conn).id == first.id


def test_run_import_skips_registered_urls_in_any_status(conn, workdir: Path) -> None:  # type: ignore[no-untyped-def]
    issue = registry.add(conn, url=URL, workdir=str(workdir))
    registry.set_status(conn, issue.id, registry.STATUS_DONE)
    importer.add_target(conn, workdir=str(workdir), label="alir")
    outcome = importer.run_import(conn, fetch=lambda wd, label: [{"url": URL, "title": "t"}])
    assert outcome.imported == []
    assert len(registry.list_issues(conn)) == 1
    # スキップしても「検索して見つけた」ことはサマリに残る
    assert outcome.checked == 1
    assert outcome.found == 1


def test_run_import_without_targets_does_not_fetch(conn) -> None:  # type: ignore[no-untyped-def]
    def fetch(wd: str, label: str) -> list[dict[str, str]]:
        raise AssertionError("fetch must not be called")

    outcome = importer.run_import(conn, fetch=fetch)
    assert outcome.imported == []
    assert outcome.errors == []
    assert outcome.checked == 0
    assert outcome.found == 0


def test_run_import_with_targets_searches_only_them(conn, tmp_path: Path) -> None:  # type: ignore[no-untyped-def]
    first = tmp_path / "first"
    first.mkdir()
    second = tmp_path / "second"
    second.mkdir()
    importer.add_target(conn, workdir=str(first), label="alir")
    target = importer.add_target(conn, workdir=str(second), label="alir")

    searched: list[str] = []

    def fetch(wd: str, label: str) -> list[dict[str, str]]:
        searched.append(wd)
        return [{"url": URL, "title": "t"}]

    outcome = importer.run_import(conn, fetch=fetch, targets=[target])
    assert searched == [str(second.resolve())]
    assert [i.workdir for i in outcome.imported] == [str(second.resolve())]
    assert outcome.checked == 1


def test_run_import_collects_errors_and_continues(conn, tmp_path: Path) -> None:  # type: ignore[no-untyped-def]
    broken = tmp_path / "broken"
    broken.mkdir()
    ok = tmp_path / "ok"
    ok.mkdir()
    importer.add_target(conn, workdir=str(broken), label="alir")
    importer.add_target(conn, workdir=str(ok), label="alir")

    def fetch(wd: str, label: str) -> list[dict[str, str]]:
        if wd == str(broken.resolve()):
            raise ImporterError("gh issue list failed")
        return [{"url": URL, "title": "t"}]

    outcome = importer.run_import(conn, fetch=fetch)
    assert [i.url for i in outcome.imported] == [URL]
    assert len(outcome.errors) == 1
    assert "gh issue list failed" in outcome.errors[0]
