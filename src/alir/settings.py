"""運用者が変更できる設定値。

ブランチ名のテンプレートなどを control テーブルの KV として保存する。
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import iceql

from alir import control, usage
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

# セッション(claude -p)に --model で渡すモデル名。未設定なら claude の既定に従う
KEY_MODEL = "model"

# 公式使用率(claude -p /usage)がこの割合(0〜1)を超えたら新規セッションを開始しない。
# ドライバ(サイクルごと)と MCP(report_progress の中断判定)が同じ値を参照する
KEY_USAGE_THRESHOLD = "usage_threshold"

# Pushover 通知の認証情報(JSON: {"token": ..., "user": ...})。
# 未設定なら環境変数(ALIR_PUSHOVER_TOKEN / ALIR_PUSHOVER_USER)にフォールバックする
KEY_PUSHOVER = "pushover"

# 通知に含める Web UI の URL(LAN 内からアクセスできるもの)。
# 未設定なら環境変数(ALIR_WEB_URL)にフォールバックする
KEY_WEB_URL = "web_url"

# voice の TTS に使う VOICEVOX engine の URL
KEY_VOICEVOX_URL = "voicevox_url"
DEFAULT_VOICEVOX_URL = "http://127.0.0.1:50021"

# VOICEVOX の話者(speaker) ID。既定はずんだもん(ノーマル)
KEY_VOICE_SPEAKER = "voice_speaker"
DEFAULT_VOICE_SPEAKER = 3

# voice の STT に使う faster-whisper のモデル名(small / medium など)
KEY_VOICE_WHISPER_MODEL = "voice_whisper_model"
DEFAULT_VOICE_WHISPER_MODEL = "small"

# STT の beam search の幅。大きいほど正確だが認識時間はトークン数に比例して伸びる。
# CPU 実行のレイテンシを優先して既定は 2(greedy より長い発話に強く、5 より大幅に速い)
KEY_VOICE_BEAM_SIZE = "voice_beam_size"
DEFAULT_VOICE_BEAM_SIZE = 2

# イベント種別ごとの voice の読み上げポリシー(JSON: {"question": "speak", ...})。
# speak は要約を合成音声で読み上げ、chime はチャイムと字幕だけ、silent は何もしない
KEY_VOICE_NOTIFY = "voice_notify_policies"
VOICE_NOTIFY_POLICIES = ("speak", "chime", "silent")
DEFAULT_VOICE_NOTIFY = {
    "question": "speak",
    "session_done": "chime",
    "issue_failed": "speak",
    "retry_exhausted": "speak",
}

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


def model(conn: iceql.Connection) -> str | None:
    """セッションに渡すモデル名を返す。未設定なら None(claude の既定に従う)。

    モデル名の妥当性は検証しない。エイリアス(sonnet / opus など)と
    完全なモデル ID のどちらも claude 側がそのまま解釈する。
    """
    return control.get_value(conn, KEY_MODEL) or None


def set_model(conn: iceql.Connection, model: str) -> None:
    """セッションに渡すモデル名を設定する。次に開始するセッションから使われる。"""
    model = model.strip()
    if not model:
        raise SettingsError("model is empty")
    control.set_value(conn, KEY_MODEL, model)


def clear_model(conn: iceql.Connection) -> None:
    """モデル名の設定を消し、claude の既定に戻す。"""
    control.set_value(conn, KEY_MODEL, "")


def voicevox_url(conn: iceql.Connection) -> str:
    """VOICEVOX engine の URL を返す。未設定なら既定のローカル URL。"""
    return control.get_value(conn, KEY_VOICEVOX_URL) or DEFAULT_VOICEVOX_URL


def set_voicevox_url(conn: iceql.Connection, url: str) -> None:
    """VOICEVOX engine の URL を設定する。空文字で既定に戻す。"""
    url = url.strip()
    if url and not url.startswith(("http://", "https://")):
        raise SettingsError("voicevox url must start with http:// or https://")
    control.set_value(conn, KEY_VOICEVOX_URL, url.rstrip("/"))


def voice_speaker(conn: iceql.Connection) -> int:
    """VOICEVOX の話者 ID を返す。未設定なら既定値。"""
    raw = control.get_value(conn, KEY_VOICE_SPEAKER)
    return DEFAULT_VOICE_SPEAKER if not raw else int(raw)


def set_voice_speaker(conn: iceql.Connection, speaker: int) -> None:
    """VOICEVOX の話者 ID を設定する。"""
    if speaker < 0:
        raise SettingsError("speaker id must be >= 0")
    control.set_value(conn, KEY_VOICE_SPEAKER, str(speaker))


def voice_whisper_model(conn: iceql.Connection) -> str:
    """STT に使う faster-whisper のモデル名を返す。未設定なら既定値。"""
    return control.get_value(conn, KEY_VOICE_WHISPER_MODEL) or DEFAULT_VOICE_WHISPER_MODEL


def set_voice_whisper_model(conn: iceql.Connection, model: str) -> None:
    """STT に使う faster-whisper のモデル名を設定する。空文字で既定に戻す。

    モデル名の妥当性は検証しない(faster-whisper 側が解釈する)。
    次に serve を起動したときから使われる。
    """
    control.set_value(conn, KEY_VOICE_WHISPER_MODEL, model.strip())


def voice_beam_size(conn: iceql.Connection) -> int:
    """STT の beam search の幅を返す。未設定なら既定値。"""
    raw = control.get_value(conn, KEY_VOICE_BEAM_SIZE)
    return DEFAULT_VOICE_BEAM_SIZE if not raw else int(raw)


def set_voice_beam_size(conn: iceql.Connection, beam_size: int) -> None:
    """STT の beam search の幅を設定する。次に serve を起動したときから使われる。"""
    if beam_size < 1:
        raise SettingsError("beam size must be >= 1")
    control.set_value(conn, KEY_VOICE_BEAM_SIZE, str(beam_size))


def voice_notify(conn: iceql.Connection) -> dict[str, str]:
    """イベント種別ごとの読み上げポリシーを返す。未設定の種別は既定値で埋める。"""
    policies = dict(DEFAULT_VOICE_NOTIFY)
    raw = control.get_value(conn, KEY_VOICE_NOTIFY)
    if raw:
        stored = json.loads(raw)
        policies.update({k: v for k, v in stored.items() if k in DEFAULT_VOICE_NOTIFY})
    return policies


def set_voice_notify(conn: iceql.Connection, updates: dict[str, str]) -> None:
    """読み上げポリシーを検証して部分更新する。指定しなかった種別は変えない。"""
    for kind, policy in updates.items():
        if kind not in DEFAULT_VOICE_NOTIFY:
            allowed = ", ".join(sorted(DEFAULT_VOICE_NOTIFY))
            raise SettingsError(f"unknown event kind: {kind} (allowed: {allowed})")
        if policy not in VOICE_NOTIFY_POLICIES:
            raise SettingsError(
                f"unknown policy: {policy} (allowed: {', '.join(VOICE_NOTIFY_POLICIES)})"
            )
    merged = voice_notify(conn)
    merged.update(updates)
    control.set_value(conn, KEY_VOICE_NOTIFY, json.dumps(merged, ensure_ascii=False))


def usage_threshold(conn: iceql.Connection) -> float:
    """公式使用率の停止閾値(0〜1)を返す。未設定なら既定値。"""
    raw = control.get_value(conn, KEY_USAGE_THRESHOLD)
    return usage.DEFAULT_THRESHOLD if not raw else float(raw)


def set_usage_threshold(conn: iceql.Connection, threshold: float) -> None:
    """公式使用率の停止閾値を設定する。ドライバは次のサイクルから参照する。"""
    if not 0 < threshold <= 1:
        raise SettingsError("usage threshold must be greater than 0 and at most 1")
    control.set_value(conn, KEY_USAGE_THRESHOLD, str(threshold))


def pushover(conn: iceql.Connection) -> tuple[str, str] | None:
    """Pushover の認証情報 (token, user) を返す。未設定なら None。"""
    raw = control.get_value(conn, KEY_PUSHOVER)
    if not raw:
        return None
    data = json.loads(raw)
    return data["token"], data["user"]


def set_pushover(conn: iceql.Connection, *, token: str, user: str) -> None:
    """Pushover の認証情報を設定する。次の通知から使われる。"""
    token = token.strip()
    user = user.strip()
    if not token or not user:
        raise SettingsError("token and user are required")
    control.set_value(conn, KEY_PUSHOVER, json.dumps({"token": token, "user": user}))


def clear_pushover(conn: iceql.Connection) -> None:
    """Pushover の認証情報を消す(環境変数があればそちらに戻る)。"""
    control.set_value(conn, KEY_PUSHOVER, "")


def web_url(conn: iceql.Connection) -> str | None:
    """通知に含める Web UI の URL を返す。未設定なら None。"""
    return control.get_value(conn, KEY_WEB_URL) or None


def set_web_url(conn: iceql.Connection, url: str) -> None:
    """通知に含める Web UI の URL を設定する。次の通知から使われる。"""
    url = url.strip()
    if not url.startswith(("http://", "https://")):
        raise SettingsError("url must start with http:// or https://")
    control.set_value(conn, KEY_WEB_URL, url)


def clear_web_url(conn: iceql.Connection) -> None:
    """Web UI の URL の設定を消す(環境変数、なければ自動検出の URL に戻る)。"""
    control.set_value(conn, KEY_WEB_URL, "")


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
