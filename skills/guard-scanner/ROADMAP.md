# guard-scanner ROADMAP

## 方針
guard-scannerはファネル②（Trust）の中核。OSSで実力を証明し、論文で引用する。
旧guava-guard-v5のTier 1/2、privacy-guardのPII検出を吸収。
**2026-02-18 Grokレビュー反映: regex→AST/ML移行、OWASP GenAI完全カバーを軸とする。**

## v1.0.0 ✅
- 静的スキャン 17カテゴリ/170+パターン（リリース時。現v1.1.1は20カテゴリ/186パターン）
- Runtime Guard hook 12パターン/3モード
- Plugin API / SARIF・HTML・JSON出力
- npm + ClawHub + GitHub公開

## v1.1.0 ✅ — sandbox検証 + 複雑度 + 設定インパクト
吸収元: Issue #18677フィードバック + OpenClawエコシステム調査
- [x] SKILL.md manifest validation（sandbox-validation）
  - 危険バイナリ要求検出（sudo/rm/curl等23種）
  - 広すぎるfileスコープ検出（**/*）
  - 機密env要求検出（SECRET/PASSWORD/PRIVATE_KEY等）
- [x] Code complexity metrics
  - 1000行超ファイル / 5段ネスト / eval密度2%超
- [x] Config impact analysis
  - openclaw.json直接書き込み検出
  - exec.approvals無効化 / exec.hostゲートウェイ化
  - hooks.internal変更 / network.allowedDomainsワイルドカード
- [x] config-impactパターン6種追加
- [x] calculateRisk倍率 + レコメンデーション追加
- カテゴリ数: 17→20 / パターン数: 170+→186

## v1.2 — PII検出 + Shadow AI検出
吸収元: `privacy-guard` + Grokレビュー指摘
- [ ] PII検出をスキャンカテゴリ化（クレカ、住所、電話番号等）
- [ ] Credential-in-context検出
- [ ] Memory poisoning vector検出
- [ ] **Shadow AI検出**: 無許可の外部LLM API呼び出し検出
  - OpenAI/Anthropic/Google/Cohere等のAPI endpoint + SDKパターン
  - 環境変数からのAPIキー読み取り検出
- [ ] **Insecure Output Handling (LLM02)**: 出力の無検証実行パターン検出

## v1.3 — OWASP Gen AI Top 10 完全カバー
目標: Grokが指摘した10項目を全てカバー可能な範囲で対応
- [ ] **LLM04 Model DoS**: 過大入力/無限ループパターン検出
- [ ] **LLM07 Insecure Plugin Design**: プラグイン入力未検証パターン
- [ ] **LLM08 Excessive Agency**: 過剰権限要求の検出（sudo, admin, root scope）
- [ ] **LLM09 Overreliance**: 出力を検証なしに信頼するパターン
- [ ] **LLM10 Model Theft**: モデルファイルの外部転送パターン
- [ ] OWASP GenAI対応マッピング表をREADMEに追加

## v2.0 — AST解析 + MLハイブリッド（脱regex）
吸収元: `guava-guard-v4` Tier 1 + Grokレビュー
- [ ] JavaScript/TypeScript AST解析（acorn/esprimaベース or ゼロ依存パーサー）
- [ ] データフロー追跡（変数→require/import→exec/fetch）
- [ ] Taint analysis
- [ ] **MLベース検出**: 難読化/AI生成コードの意図判定
  - TF.js or ONNX.jsでローカル推論（軽量モデル）
  - regexパターンをフォールバックとして保持
- [ ] **SBOM生成**: 依存関係の深層偽装対策
- [ ] **PQC(量子耐性暗号)チェック**: 脆弱アルゴリズム(RSA/EC)使用検出
- [ ] 実世界悪意スキルでの偽陽性/偽陰性率評価

## v2.1 — コミュニティ駆動化
- [ ] CONTRIBUTING.md整備（パターン貢献ガイド）
- [ ] パターン定義をYAML化（非プログラマーでも貢献可能に）
- [ ] GitHub Actions CI（PR時に自動テスト）
- [ ] 脅威情報の自動更新パイプライン

## 宣伝ロードマップ（2026-02-18 Grok指摘）
- [ ] Reddit r/AI, r/MachineLearning に英語ポスト
- [ ] Hacker News Show HN ポスト
- [ ] X英語圏向けポスト
- [ ] スター/フォーク → コミュニティ形成 → パターン貢献加速
