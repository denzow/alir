"""運用者が変更できる設定値。

ブランチ名のテンプレートなどを control テーブルの KV として保存する。
"""

from __future__ import annotations

import re

import iceql

from alir import control
from alir.registry import Issue

KEY_BRANCH_TEMPLATE = "branch_template"
DEFAULT_BRANCH_TEMPLATE = "alir/issue-{number}"

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
