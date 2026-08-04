"""ラベル付き Issue の取り込み。

取り込み対象(workdir + ラベル)を control テーブルの KV として持ち、
GitHub を検索して未登録の Issue をキュー末尾に登録する。
実行の起点は Web UI の取り込みボタンで、間隔(KEY_IMPORT_INTERVAL)を
設定したときだけドライバのサイクルからも定期実行する。
plan.md の原則(ドライバは GitHub を直接検索しない)は維持する。
検索するのはこの取り込み処理であり、ドライバ本体はレジストリだけを見る。
"""

from __future__ import annotations

import json
import subprocess
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

import iceql

from alir import control, registry
from alir.registry import Issue, RegistryError

KEY_IMPORT_TARGETS = "import_targets"

# ドライバが定期取り込みを実行する間隔(秒)。未設定・0 なら定期取り込みをしない。
KEY_IMPORT_INTERVAL = "import_interval"

# 定期取り込みを有効にするときの目安の間隔(秒)。Web UI の入力欄の初期値に使う。
DEFAULT_INTERVAL = 300.0

# 定期取り込みで許す最短の間隔(秒)。gh の呼びすぎを防ぐための下限。
MIN_INTERVAL = 30.0

# 1 対象あたりの gh issue list を待つ上限(秒)。
FETCH_TIMEOUT = 60.0

Fetch = Callable[[str, str], list[dict[str, str]]]


class ImporterError(Exception):
    """取り込み対象の登録・検索に関する失敗。"""


@dataclass(frozen=True)
class ImportTarget:
    """取り込み対象。workdir の git remote が指すリポジトリからラベルで検索する。"""

    workdir: str
    label: str


@dataclass(frozen=True)
class ImportOutcome:
    imported: list[Issue]
    errors: list[str]
    # 稼働ログ用の実行サマリ。checked は検索を試みた対象数(fetch が失敗した
    # 対象も含む)、found は fetch が返した Issue の延べ数(登録済みなどで
    # スキップしたものも含む)。
    checked: int = 0
    found: int = 0


def list_targets(conn: iceql.Connection) -> list[ImportTarget]:
    """取り込み対象を登録順に返す。未設定なら空リスト。"""
    raw = control.get_value(conn, KEY_IMPORT_TARGETS)
    if not raw:
        return []
    return [ImportTarget(workdir=item["workdir"], label=item["label"]) for item in json.loads(raw)]


def _save_targets(conn: iceql.Connection, targets: list[ImportTarget]) -> None:
    control.set_value(
        conn,
        KEY_IMPORT_TARGETS,
        json.dumps([{"workdir": t.workdir, "label": t.label} for t in targets], ensure_ascii=False),
    )


def add_target(conn: iceql.Connection, *, workdir: str, label: str) -> ImportTarget:
    """取り込み対象を追加する。workdir は存在するディレクトリでなければならない。"""
    label = label.strip()
    if not label:
        raise ImporterError("label is empty")
    path = Path(workdir).expanduser()
    if not path.is_dir():
        raise ImporterError(f"workdir not found: {workdir}")
    target = ImportTarget(workdir=str(path.resolve()), label=label)
    targets = list_targets(conn)
    if target in targets:
        raise ImporterError(f"target already registered: {label} ({target.workdir})")
    _save_targets(conn, [*targets, target])
    return target


def find_target(conn: iceql.Connection, *, workdir: str, label: str) -> ImportTarget:
    """登録済みの取り込み対象を 1 件返す。登録がなければ ImporterError。"""
    resolved = str(Path(workdir).expanduser().resolve())
    for target in list_targets(conn):
        if target.label == label and target.workdir == resolved:
            return target
    raise ImporterError(f"target not registered: {label} ({workdir})")


def import_interval(conn: iceql.Connection) -> float:
    """ドライバの定期取り込みの間隔(秒)。0 なら定期取り込みをしない(既定)。"""
    raw = control.get_value(conn, KEY_IMPORT_INTERVAL)
    if not raw:
        return 0.0
    try:
        return float(raw)
    except ValueError:
        # 手で書き換えられて壊れていても、勝手に走らせない側に倒す
        return 0.0


def set_import_interval(conn: iceql.Connection, seconds: float) -> None:
    """定期取り込みの間隔を設定する。0 で定期取り込みを無効にする。"""
    if seconds < 0:
        raise ImporterError("interval must be 0 or greater")
    if 0 < seconds < MIN_INTERVAL:
        raise ImporterError(f"interval must be 0 or {MIN_INTERVAL:g} seconds or more")
    control.set_value(conn, KEY_IMPORT_INTERVAL, "" if seconds == 0 else f"{seconds:g}")


def remove_target(conn: iceql.Connection, *, workdir: str, label: str) -> None:
    """取り込み対象を削除する。登録がなければ ImporterError。"""
    resolved = str(Path(workdir).expanduser().resolve())
    targets = list_targets(conn)
    remaining = [t for t in targets if not (t.label == label and t.workdir == resolved)]
    if len(remaining) == len(targets):
        raise ImporterError(f"target not registered: {label} ({workdir})")
    _save_targets(conn, remaining)


def fetch_labeled_issues(workdir: str, label: str) -> list[dict[str, str]]:
    """gh CLI でラベル付きのオープンな Issue を検索する。

    Web UI の取り込みボタンからも呼ぶため、gh が応答しないときに
    リクエストが返らなくならないよう時間で打ち切る。
    """
    try:
        proc = subprocess.run(
            ["gh", "issue", "list", "--label", label, "--json", "url,title", "--limit", "100"],
            cwd=workdir,
            capture_output=True,
            text=True,
            check=False,
            timeout=FETCH_TIMEOUT,
        )
    except subprocess.TimeoutExpired as exc:
        raise ImporterError(f"gh issue list timed out after {FETCH_TIMEOUT:g}s") from exc
    if proc.returncode != 0:
        raise ImporterError(f"gh issue list failed: {proc.stderr.strip()}")
    return [
        {"url": str(item["url"]), "title": str(item.get("title", ""))}
        for item in json.loads(proc.stdout)
    ]


def run_import(
    conn: iceql.Connection,
    *,
    fetch: Fetch = fetch_labeled_issues,
    targets: list[ImportTarget] | None = None,
) -> ImportOutcome:
    """対象を検索し、未登録の Issue をキュー末尾に登録する。

    targets を渡すとその対象だけを検索する(Web UI の対象ごとの取り込み)。
    省略時は登録済みの全対象を検索する。
    対象が空なら何もしない(GitHub API も叩かない)。
    レジストリに同じ URL がある Issue は状態を問わずスキップする。
    done / failed でも再登録しないのは、ラベルが付いたままの消化済み Issue を
    サイクルのたびに取り込み直すループを防ぐためである(再実行は人が登録する)。
    1 対象の検索失敗は errors に集めて他の対象の取り込みを続ける。
    """
    targets = list_targets(conn) if targets is None else targets
    if not targets:
        return ImportOutcome(imported=[], errors=[])
    known = {issue.url for issue in registry.list_issues(conn)}
    imported: list[Issue] = []
    errors: list[str] = []
    found = 0
    for target in targets:
        try:
            items = fetch(target.workdir, target.label)
        except Exception as exc:  # noqa: BLE001
            errors.append(f"{target.label} ({target.workdir}): {exc}")
            continue
        found += len(items)
        for item in items:
            url = item.get("url")
            if not url or url in known:
                continue
            try:
                issue = registry.add(
                    conn, url=url, workdir=target.workdir, title=item.get("title") or None
                )
            except RegistryError:
                continue
            known.add(url)
            imported.append(issue)
    return ImportOutcome(imported=imported, errors=errors, checked=len(targets), found=found)
