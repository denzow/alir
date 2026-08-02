# alir

[English README](README.md)

Claude Code の自律ループを支える非同期判断サービス。
名前は **A**gent **L**oop for **I**ssue **R**esolution の略。

Claude Code が作業中に人間の判断が必要になったとき、質問を登録してループを止めずに先へ進むための仕組みを提供する。
質問は [iceql](https://github.com/denzow/iceql) のデータベース（CSV + YAML）に蓄積され、人間は CLI や Web UI から非同期に回答する。

設計の全体像は [plan.md](plan.md) を参照。

## 使い方

```console
# 消化対象の Issue を登録する（タイトルは gh で取得される）
$ alir issues add https://github.com/owner/repo/issues/12 --workdir ~/work/repo
# 作業種別（auto / implement / refine）と補足コメントも指定できる
$ alir issues add https://github.com/owner/repo/issues/13 --workdir ~/work/repo \
    --mode refine --note "対象は API 層だけにする"
$ alir issues                       # 一覧
$ alir issues import --label agent-ready --workdir ~/work/repo   # ラベルで一括取り込み(補助)
$ alir issues targets add --label agent-ready --workdir ~/work/repo  # ラベルの自動取り込み対象に登録

# 直接 push 運用: このリポジトリでは PR を作らず develop に push して開発を続ける
$ alir push-branch set --workdir ~/work/repo --branch develop

# ループドライバ・Web UI・MCP(HTTP) をワンプロセスで起動する
$ alir serve --port 8710 --session-budget 4000000 --weekly-budget 20000000

# 未回答の質問を一覧する
$ alir questions

# 質問に回答する（選択肢番号または自由記述）
$ alir answer 1 2 "B案で。ただしテストを先に"
```

`alir push-branch set` で登録したリポジトリは直接 push 運用になる。
セッションは PR を作らず、push 先ブランチをチェックアウトした共有 worktree で作業して直接 push する。
同じリポジトリのセッションは同時に 1 つしか動かない。
push 先には workdir でチェックアウトしていないブランチを指定すること（チェックアウト中のブランチは git が worktree の作成を拒否する）。

推奨は `alir serve` での起動。
ドライバが起動する claude セッションは、セッションごとにサブプロセスを立てる代わりに `http://127.0.0.1:PORT/mcp` の MCP エンドポイントへ接続する。
個別起動も可能で、`alir run`（ドライバのみ）、`alir web`（Web UI のみ）、`alir mcp`（手動セッション用の stdio MCP サーバー）が使える。

データディレクトリは環境変数 `ALIR_DATA_DIR` で指定する。
未指定なら `$XDG_DATA_HOME/alir`（既定は `~/.local/share/alir`）を使う。

## セキュリティ上の注意

Web UI には認証がない。LAN 内に限って使い、インターネット側に公開しないこと。
質問には作業対象リポジトリの内部情報が含まれ得る。
