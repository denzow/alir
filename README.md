# alir

Claude Code の自律ループを支える非同期判断サービス。

Claude Code が作業中に人間の判断が必要になったとき、質問を登録してループを止めずに先へ進むための仕組みを提供する。
質問は [iceql](https://github.com/denzow/iceql) のデータベース（CSV + YAML）に蓄積され、人間は CLI や Web UI から非同期に回答する。

設計の全体像は [plan.md](plan.md) を参照。

## 使い方

```console
# 未回答の質問を一覧する
$ alir questions

# 質問に回答する（選択肢番号または自由記述）
$ alir answer 1 2 "B案で。ただしテストを先に"

# MCP サーバーとして起動する（Claude Code から ask_human を呼ぶ）
$ alir mcp
```

データディレクトリは環境変数 `ALIR_DATA_DIR` で指定する。
未指定なら `$XDG_DATA_HOME/alir`（既定は `~/.local/share/alir`）を使う。
