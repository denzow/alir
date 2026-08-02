"""運用者が変更できる設定値。

ブランチ名のテンプレートなどを control テーブルの KV として保存する。
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import iceql

from alir import control
from alir.registry import Issue

KEY_BRANCH_TEMPLATE = "branch_template"
DEFAULT_BRANCH_TEMPLATE = "alir/issue-{number}"

# 直接 push 運用(PR を作らず push 先ブランチで開発を続ける)の workdir -> ブランチ名
KEY_PUSH_BRANCHES = "push_branches"

# park からの再開で claude -p --resume を使うかどうか("0" で無効)
KEY_RESUME_ENABLED = "resume_enabled"

# failed の Issue を自動で queued に戻す回数の上限
KEY_RETRY_LIMIT = "retry_limit"
DEFAULT_RETRY_LIMIT = 2

# ドライバが埋める変数
_DRIVER_PLACEHOLDERS = {"number", "id", "repo"}
# セッション(実装する Claude)が決めて git branch -m で反映する変数と、その仮の値
SESSION_PLACEHOLDERS = {"type": "work", "summary": "wip"}

_PLACEHOLDERS = _DRIVER_PLACEHOLDERS | set(SESSION_PLACEHOLDERS)

_VALID_BRANCH = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._/\-]*$")


class SettingsError(Exception):
    """設定値の検証エラー。"""


def branch_template(conn: iceql.Connection) -> str:
    """ブランチ名のテンプレートを返す。未設定なら既定値。"""
    return control.get_value(conn, KEY_BRANCH_TEMPLATE) or DEFAULT_BRANCH_TEMPLATE


def set_branch_template(conn: iceql.Connection, template: str) -> None:
    """ブランチ名のテンプレートを検証して保存する。"""
    validate_branch_template(template)
    control.set_value(conn, KEY_BRANCH_TEMPLATE, template)


def validate_branch_template(template: str) -> None:
    """テンプレートを検証し、問題があれば SettingsError を送出する。"""
    fields = set(re.findall(r"{([^{}]*)}", template))
    unknown = fields - _PLACEHOLDERS
    if unknown:
        allowed = ", ".join(sorted("{" + p + "}" for p in _PLACEHOLDERS))
        raise SettingsError(
            f"unknown placeholder: {', '.join(sorted(unknown))} (allowed: {allowed})"
        )
    if not fields & {"number", "id"}:
        raise SettingsError("template must contain {number} or {id} to keep branches unique")
    sample = template.format(number=12, id=1, repo="repo", **SESSION_PLACEHOLDERS)
    if (
        not _VALID_BRANCH.match(sample)
        or ".." in sample
        or sample.endswith("/")
        or sample.endswith(".lock")
    ):
        raise SettingsError(f"template renders an invalid branch name: {sample}")


def has_session_placeholders(template: str) -> bool:
    """セッションが決める変数({type} / {summary})を含むかどうか。"""
    fields = set(re.findall(r"{([^{}]*)}", template))
    return bool(fields & set(SESSION_PLACEHOLDERS))


def validate_branch_name(branch: str) -> None:
    """ブランチ名そのものを検証し、問題があれば SettingsError を送出する。"""
    if (
        not _VALID_BRANCH.match(branch)
        or ".." in branch
        or branch.endswith("/")
        or branch.endswith(".lock")
    ):
        raise SettingsError(f"invalid branch name: {branch}")


def resume_enabled(conn: iceql.Connection) -> bool:
    """park からの再開で --resume を使うかどうか。未設定なら有効。"""
    return control.get_value(conn, KEY_RESUME_ENABLED) != "0"


def set_resume_enabled(conn: iceql.Connection, enabled: bool) -> None:
    """park からの再開で --resume を使うかどうかを設定する(挙動の切り分け用)。"""
    control.set_value(conn, KEY_RESUME_ENABLED, "1" if enabled else "0")


def retry_limit(conn: iceql.Connection) -> int:
    """failed の自動リトライ回数の上限を返す。未設定なら既定値。"""
    raw = control.get_value(conn, KEY_RETRY_LIMIT)
    return DEFAULT_RETRY_LIMIT if raw is None else int(raw)


def set_retry_limit(conn: iceql.Connection, limit: int) -> None:
    """failed の自動リトライ回数の上限を設定する。0 で自動リトライを無効にする。"""
    if limit < 0:
        raise SettingsError("retry limit must be 0 or greater")
    control.set_value(conn, KEY_RETRY_LIMIT, str(limit))


def push_branches(conn: iceql.Connection) -> dict[str, str]:
    """直接 push 運用の workdir -> push 先ブランチの対応を返す。未設定なら空。"""
    raw = control.get_value(conn, KEY_PUSH_BRANCHES)
    if not raw:
        return {}
    return {str(k): str(v) for k, v in json.loads(raw).items()}


def push_branch(conn: iceql.Connection, workdir: str) -> str | None:
    """workdir の push 先ブランチを返す。直接 push 運用でなければ None。"""
    return push_branches(conn).get(workdir)


def _save_push_branches(conn: iceql.Connection, mapping: dict[str, str]) -> None:
    control.set_value(conn, KEY_PUSH_BRANCHES, json.dumps(mapping, ensure_ascii=False))


def set_push_branch(conn: iceql.Connection, *, workdir: str, branch: str) -> None:
    """workdir を直接 push 運用にする(PR を作らず branch へ push して開発を続ける)。"""
    branch = branch.strip()
    validate_branch_name(branch)
    path = Path(workdir).expanduser()
    if not path.is_dir():
        raise SettingsError(f"workdir not found: {workdir}")
    mapping = push_branches(conn)
    mapping[str(path.resolve())] = branch
    _save_push_branches(conn, mapping)


def clear_push_branch(conn: iceql.Connection, *, workdir: str) -> None:
    """workdir の直接 push 運用をやめる。設定がなければ SettingsError。"""
    resolved = str(Path(workdir).expanduser().resolve())
    mapping = push_branches(conn)
    if resolved not in mapping:
        raise SettingsError(f"push branch not set: {workdir}")
    del mapping[resolved]
    _save_push_branches(conn, mapping)


def render_branch(template: str, issue: Issue) -> str:
    """テンプレートから Issue のブランチ名を作る。

    セッションが決める変数は仮の値で埋める。
    実際の名前はセッションが git branch -m で付け、終了後にドライバが取り込む。
    """
    return template.format(
        number=issue.number,
        id=issue.id,
        repo=issue.repo.split("/")[-1],
        **SESSION_PLACEHOLDERS,
    )
