# alir voice 実装計画

同一LAN内(Tailnet内)のAndroidスマホをクライアントとした常時通話インターフェースの実装計画。
スマホは薄いマイク/スピーカー端末とし、VAD・STT・意図解釈・TTSはすべて alir serve 側に集約する。

## ゴール

- 机に置いたAndroidスマホ(PWA通話画面)に話しかけると、alirがIssue登録・質問回答などの操作を実行する
- driverのイベント(セッション完了・質問登録・リトライ上限)を、スマホ側から音声で通知してくる
- 追加ハードなし。母艦(alir serve稼働マシン)+ Androidスマホのみで完結

## 全体構成

```
[Android PWA 通話画面]
  getUserMedia → AudioWorklet(16kHz PCM) ─┐
  再生キュー(TTS音声) ←──────────────────┤ WebSocket (wss)
                                          │
[alir serve 単一プロセス]                 │
  /voice/ws ←─────────────────────────────┘
    ├─ silero-VAD: 発話区間の切り出し
    ├─ faster-whisper: STT(日本語)
    ├─ 意図解釈: 常駐Claudeエージェント + alir内部ツール
    ├─ イベントバス: driverイベントの購読 → 読み上げ文生成
    └─ VOICEVOX(ローカルHTTP): TTS → 音声フレームをWSで返送
```

- 経路は Tailscale。`tailscale cert` でLet's Encrypt証明書を取得し、PWA要件(secure context)とWeb UI無認証問題を同時に解決する
- WSプロトコルは「JSONテキストフレーム(制御) + バイナリフレーム(音声)」の混在方式

## フェーズ分け

### Phase 0: 基盤整備(半日)

- [ ] Tailscale導入、`tailscale cert` によるHTTPS化。uvicornに証明書を渡すか、`tailscale serve` でリバースプロキシ
- [ ] alir serve に `/voice/ws` WebSocketエンドポイントの骨組みを追加
- [ ] driver内の通知ポイント(Pushover送信箇所)にイベントバス(`asyncio.Queue` ベースのpub/sub)を注入。既存Pushover通知はバス購読者の一つとして再配置
- [ ] `alir voice` サブコマンドの雛形(設定の置き場所を確保)

完了条件: スマホのChromeから `wss://<host>.ts.net:8710/voice/ws` に接続でき、pingが返る。

### Phase 1: 音声パイプ疎通(1〜2日)

まず「話した内容がテキスト化され、そのまま合成音声でオウム返しされる」ループだけを通す。意図解釈はまだ載せない。

- [ ] PWA通話画面: 「通話開始」ボタン → `getUserMedia`(echoCancellation: true)→ AudioWorkletで16kHz/mono/16bit PCMに変換しWS送信
- [ ] Screen Wake Lock API で画面常時点灯。`visibilitychange` での再接続処理
- [ ] サーバ: silero-VADで発話区間を検出し、区間確定ごとにfaster-whisper(まずは `small`、品質不足なら `medium`)でSTT
- [ ] VOICEVOX engineをローカルで起動(Docker可)。認識テキストをそのまま合成してWSでクライアントへ返す
- [ ] クライアント: 受信音声フレームを再生キューで順次再生
- [ ] barge-inの土台: サーバがVADで発話開始を検出したら `{"type": "interrupt"}` を送り、クライアントは再生キューを破棄

完了条件: スマホに話しかけると1〜2秒でオウム返しが返る。往復レイテンシを計測してログに残す。

### Phase 2: イベント読み上げ(1日)

「向こうから話しかけてくる」側。

- [ ] イベントバスの購読者としてvoiceハンドラを追加。WS接続中のクライアントに通知を配送
- [ ] イベント種別ごとの読み上げポリシー設定(`speak` / `chime` / `silent`)。CLI: `alir voice notify set question=speak session_done=chime`
- [ ] 質問イベントは本文が長いので、TTS前にLLMで口頭向けに要約(「Issue 45で質問です。DB変更の方式について、選択肢が3つあります」程度)
- [ ] クライアント側: 通知読み上げの前にチャイム音を鳴らす(発話とイベント読み上げの区別)
- [ ] 未接続時のフォールバック: WSクライアントがいなければ従来通りPushoverのみ

完了条件: セッション完了・質問登録時にスマホが自発的に喋る。うるさければポリシーで黙らせられる。

### Phase 3: 音声コマンド / 意図解釈(2〜3日)

- [ ] 常駐エージェントの実装。`claude -p` の都度起動は起動オーバーヘッドが大きいため、Claude APIのtool use(またはAgent SDKの常駐セッション)で、alirの内部関数を直接ツールとして公開する
  - ツール候補: `list_questions` / `answer_question` / `add_issue` / `list_issues` / `session_status`
- [ ] 文脈保持: 直前に読み上げた質問IDをセッション状態に持ち、「それ、Bで進めて」の指示語を解決できるようにする
- [ ] 確認フロー: `add_issue` や `answer_question` など状態を変える操作は、実行前に復唱(「Issue 45にBで回答します。いいですか?」)→「はい/OK」で確定。誤認識対策として必須
- [ ] STT誤認識に備え、認識テキストをWS経由で画面にも表示(字幕)。目視で誤爆に気づける
- [ ] 応答生成: エージェントの返答テキストをVOICEVOXで合成して返す(Phase 1の経路を流用)

完了条件: 「Issue 45を積んで。変更はAPI層に限定で」→ `issues add --note` 相当が実行され、口頭で結果が返る。

### Phase 4: 運用改善(随時)

- [ ] 通話モードのオン/オフをスマホ画面のトグルで切り替え(常時リスニングが不要な時間帯用)
- [ ] 必要ならウェイクワード(openWakeWord)。ただし通話タブ方式なら優先度低
- [ ] Whisperのhallucination対策: 無音区間での幻聴テキスト(「ご視聴ありがとうございました」等)をフィルタ
- [ ] 認証: Tailnet内前提なら急がないが、WS接続時のトークン検証は入れておくと安心
- [ ] 複数クライアント対応(PC + スマホ同時接続時の配送ルール)

## WSプロトコル案

テキストフレーム(JSON):

```
クライアント → サーバ
  {"type": "start", "sample_rate": 16000}
  {"type": "stop"}

サーバ → クライアント
  {"type": "stt_partial", "text": "..."}        # 字幕用
  {"type": "stt_final", "text": "..."}
  {"type": "agent_reply", "text": "..."}         # 読み上げ本文(字幕にも出す)
  {"type": "event", "kind": "question", ...}
  {"type": "interrupt"}                          # 再生キュー破棄指示
  {"type": "audio_start"} / {"type": "audio_end"}
```

バイナリフレーム: PCM音声(上り: マイク入力 / 下り: `audio_start`〜`audio_end` 間のTTS音声)。

## 技術スタック

| 役割 | 採用 | 備考 |
|---|---|---|
| トランスポート | WebSocket(FastAPI/uvicorn) | serve既存プロセスに同居 |
| HTTPS | Tailscale + `tailscale cert` | LAN縛りも同時に外れる |
| VAD | silero-vad | ONNXでCPU軽量 |
| STT | faster-whisper small→medium | GPUなしでもsmallは実用圏 |
| TTS | VOICEVOX engine | ローカルHTTP、日本語品質 |
| 意図解釈 | Claude API tool use(常駐) | alir内部関数を直接ツール化 |
| クライアント | PWA(素のJS + AudioWorklet) | フレームワーク不要の規模 |

## リスクと対策

- **Android Chromeのバックグラウンド制限**: wake lock + 充電スタンド運用で回避。画面OFF対応が欲しくなったらネイティブForeground Serviceを別途検討(スコープ外)
- **エコー(TTS再生をマイクが拾う)**: `getUserMedia` のAECで大半は解決。残る場合はTTS再生中のVAD感度を下げる、または再生中はSTT区間を破棄
- **STT誤認識による誤操作**: 状態変更系は復唱確認を必須にする(Phase 3)。字幕表示で気づけるようにする
- **レイテンシ**: 目標は発話終了→応答開始2秒以内。whisperモデルサイズとVADの区間確定待ち時間(末尾無音判定、300〜500ms)がチューニングポイント
- **VOICEVOX起動忘れ**: serve起動時にヘルスチェックし、落ちていればWeb Speech API TTSへフォールバック(クライアント側合成)

## 実装順のポイント

Phase 1の音声パイプ疎通が全体のリスクの大半を占める(HTTPS・AudioWorklet・VAD・レイテンシ)。ここが通れば残りは既存のalirの延長線上の作業なので、Phase 1完了時点で一度体験を評価し、レイテンシや認識品質に問題があればPhase 3の前にモデル・構成を見直す。
