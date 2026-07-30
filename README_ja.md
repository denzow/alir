# alir

[English README](README.md)

Claude Code の自律ループを支える非同期判断サービス。

Claude Code が作業中に人間の判断が必要になったとき、質問を登録してループを止めずに先へ進むための仕組みを提供する。
質問は [iceql](https://github.com/denzow/iceql) のデータベース（CSV + YAML）に蓄積され、人間は CLI や Web UI から非同期に回答する。

設計の全体像は [plan.md](plan.md) を参照。

## 使い方

```console
# 消化対象の Issue を登録する
$ alir issues add https://github.com/owner/repo/issues/12 --workdir ~/work/repo --priority 5
$ alir issues                       # 一覧
$ alir issues import --label agent-ready --workdir ~/work/repo   # ラベルで一括取り込み(補助)

# ループドライバを起動する
$ alir run --parallel 1 --session-budget 4000000 --weekly-budget 20000000

# 未回答の質問を一覧する
$ alir questions

# 質問に回答する（選択肢番号または自由記述）
$ alir answer 1 2 "B案で。ただしテストを先に"

# 回答用の Web UI を起動する（LAN 内のスマホから回答できる）
$ alir web --port 8710

# MCP サーバーとして起動する（Claude Code から ask_human を呼ぶ）
$ alir mcp
```

データディレクトリは環境変数 `ALIR_DATA_DIR` で指定する。
未指定なら `$XDG_DATA_HOME/alir`（既定は `~/.local/share/alir`）を使う。

## セキュリティ上の注意

Web UI には認証がない。LAN 内に限って使い、インターネット側に公開しないこと。
質問には作業対象リポジトリの内部情報が含まれ得る。
