# 被験者ID解決ガイド（施設導入・ガバナンス）

データ自動収集では、データを引くシステム（院内DWH / FHIR / ファイル）ごとに患者を
識別するキーが**異なります**（`where patientid = '0000123456'` のように）。KAWASEMI は
これを「登録番号(Hub)」と「key_type(名前空間)」に分離し、実行時に外部 resolver で
解決します。本書は施設の DPO / IRB / 情報システム部門向けの説明です。

## 1. 不変条件（最重要）

> **KAWASEMI が永続保持する患者情報は「登録番号（試験内の仮名ID）」と「取得した臨床値
> （CRF 入力という業務成果物）」だけです。各システムの患者キー（MRN 等）・氏名・名寄せ表は
> 保持も受領もしません。KAWASEMI からの患者キー値の入力経路は構造的に存在しません。**

これにより「別患者の値を取り違える」事故が構造的に防止されます（手入力経路が無いため）。

## 2. アーキテクチャ（4つの分離面）

1. **接続レジストリ** `connections.yaml` — connection_id → 接続詳細/scope（secret は env 参照）。
2. **リソース索引** — db定義書/FHIR辞書/参照文書の「地図」。患者データは載せない。
3. **実行** — 患者値を扱う（本番DB行 / FHIR患者リソース / ファイル中身）。
4. **ID解決(resolver)** — 登録番号 → 各システムの患者キー。**解決そのものを外部（院内 FHIR）へ
   委ねる**（KAWASEMI は名寄せ表を持たない）。別プロセス化は将来案（§4）。

## 3. キーのモデル

- **Hub＝登録番号(`trial_subject_no`)**：実行時に入力する被験者の仮名ID。`(study, subject)` で一意。
- **スポーク＝key_type（名前空間）**：`mrn` / `dwh_serial` / `fhir_patient_id` / … を
  `key_types.yaml`（レベル1カタログ）で定義。
- **変換表(crosswalk)**：`登録番号 → {key_type: 値}`。**KAWASEMI は保持しません**。
  既定実装は FHIR `Patient.identifier`（`system|value`）を crosswalk として使います。
- **整形の2層**：resolver は canonical（正準形）を返し、物理整形（ゼロ詰め等）は
  DSL `step.physical_format` で実行時バインド直前に適用します。
- **step の `key_type` / `physical_format` はコンパイラが決める**。LLM には書かせません。
  - `key_type`: DB は索引の `payload.key_type`（列ごと）。file / csv / fhir は `connections.yaml` の
    接続ごとの既定 `key_type`
  - `physical_format`: `connections.yaml` の `key_formats.<key_type>`（「この接続ではこの名前空間の値を
    この形で持つ」）。**接続単位**で、列ごとには宣言できません（indexer に語彙が無いため）
  - LLM が別の値を書いていたら宣言の値に置き換えて `needs_input` にします
  - 付けない step（患者で絞らない表、自アプリDBの anchor）と、宣言もカタログも無い名前の扱いは
    [dsl_specification.md](dsl_specification.md) の §7「患者キーの解決」

  ```yaml
  connections:
    dwh:       {kind: db,   scope: external, secret_ref: env:DWH_DSN, key_formats: {mrn: {pad: 10}}}
    report_fs: {kind: file, scope: external, root_path: /mnt/reports, key_type: report_key}
    hosp_fhir: {kind: fhir, scope: external, base_url: https://..., key_type: fhir_patient_id}
  ```

## 4. resolver（ID解決系）

**実装は院内 FHIR の identifier 検索1つです。** `Patient?identifier={system}|{登録番号}` で患者を
特定し、その `identifier` から各 key_type を解決します（in-process）。接続設定は FHIR サーバの
ものを使います（`FHIR_SERVER_URL` / `FHIR_SERVER_TIMEOUT` / `FHIR_AUTH_MODE` ほか）。anchor と
なる key_type は `KEY_RESOLVER_ANCHOR`（既定 `trial_subject_no`）で指定します。

- 契約はバッチ（実行前に必要 key_type を一括解決）。`found / not_found / ambiguous` を返し、
  **ambiguous は自動選択しません**（一意化は resolver/外部系の責務）。
- 要求した `key_type` が応答から得られない場合は `not_found` で埋めます（要求 key_type を必ず埋める）。
- **可用性は SPoF**：resolver 不通時は自動収集を停止します（誤った患者の値を取るより、
  何も取らない）。KAWASEMI 側に名寄せキャッシュは置きません。可用性は resolver の
  HA 構成（施設責務）で担保してください。院内 NW を越える経路には mTLS / トークン認証を推奨します。

### 別プロセス化は将来案

以前は `key_resolver_backend=http` で名寄せ系を別プロセスへ物理隔離できる設計を持っていました。
**選ぶ利用者が現れなかったため実装を削除しました**（設定はどの運用構成・CI にも
無く、実装は自分のテストからのみ呼ばれていた）。切り替えの層（Protocol ＋ factory ＋ HTTP 実装）
を持たないので、**現時点で resolver を差し替える設定はありません。**

隔離の要求が実際に出た時点で、その利用者と一緒に実装し直します。施設へ提示するワイヤ契約の
草案は別紙 [`facility_resolver_requirements.md`](facility_resolver_requirements.md) §5 に
将来案として残しています。

### resolver 障害の波及範囲（重要）

resolver の失敗には**2つの粒度**があり、波及範囲が異なります。運用上この区別が要です。

| 事象 | 粒度 | KAWASEMI の挙動 | 実行結果 |
|---|---|---|---|
| resolver 到達不能・timeout・5xx・401/403・応答不正 | **実行全体** | 例外を送出し実行を停止（本番DB/FHIR/ファイルへ一切アクセスしない。FHIR 例外をそのまま伝播する） | `InputExecution.status=failed` |
| ある被験者の key_type が `not_found` / `ambiguous` | **その key_type を使う項目だけ** | その項目を `needs_input` としてスキップ（他の項目は継続） | 他が取れれば `status=partial` |

- 前者は「名寄せ基盤そのものの障害」で、誤った患者値を取るより**全停止**が安全。
- 後者は「この被験者はこのシステムに未登録」等の正常な部分欠落で、`needs_input` 項目は
  手入力（collector）へ縮退します（[manual 縮退フロー](manual_fallback_flow.md)）。

## 5. カタログ化（key_type の付与）

1. `key_types.yaml` に施設の名前空間を定義（id は将来 FHIR `identifier.system` URI と揃えると良い）。
2. 索引の患者キー列へ key_type を付与（indexer（このリポジトリの `indexer/`）のワークフロー）：
   - `kawasemi-indexer draft-knowledge` が知識層と一緒に key_type を下書き（名称類似による候補提示）。
   - 人が出力された knowledge YAML を確認・修正し、`kawasemi-indexer ingest` で取り込む。
3. 「列名だけでは mrn か dwh_serial か決まらない」ため、最終確定は人手です。将来 `--from-live`
   のフォーマット指紋（桁数・ゼロ詰め・値集合の重なり率）で候補を補強します。

### `--from-live` 指紋採取について（実装予定）

本番DBへサンプリング接続する場合、**読むのは患者キー列のフォーマット指紋のみ**で、
**患者値そのものは保存しません**（保存するのは桁数・ゼロ詰め有無・接頭辞・チェックディジット
有無・列間の値集合の重なり率といったメタ情報だけ）。採取前にドライランで対象と採取内容を
提示し、採取ログ（いつ・どの接続・何件・どの指紋）を監査に残します。

## 6. 証跡（GxP データインテグリティ）

実行ごとの「データ収集手続確認書（Computerized System Validation 文書）」に
**「ID解決経路」節**を出力します：
- 使用した key_type と解決状況（found/not_found/ambiguous）。
- 患者キーは**マスク値**（温存量は値長に比例。末尾 min(4, 値長の25%) 文字のみ。
  例: 10桁 `0000012345`→`********45`、長いUUID→末尾4文字）のみ。生値は残しません。
- 「KAWASEMI からの値入力経路は構造的に存在しない」旨の宣言。
- 「どの被験者か」は登録番号で一意に追え、名寄せ先の生IDは外部 resolver 側の監査で辿ります。

## 7. 施設導入チェックリスト

- [ ] `key_types.yaml` に名前空間を定義（mrn 等、FHIR system URI と整合）。
- [ ] `connections.yaml` に接続・secret_ref（env）を設定。file / csv / fhir の接続には既定の
      `key_type`、ゼロ詰め等で格納している接続には `key_formats` を書く（§3）。
- [ ] 院内 FHIR の `Patient.identifier` に登録番号と各患者キーが載っていることを確認
      （crosswalk として使えること。載っていなければ資源の所在を手入力へ縮退させる判断が要る）。
- [ ] resolver（院内 FHIR）の可用性（HA）と認証（mTLS）を整備。
- [ ] 索引を構築し、患者キー列へ key_type を確定（indexer の `draft-knowledge` 下書き
      → knowledge YAML を人手で確認・修正 → `ingest`）。
- [ ] `--from-live` 指紋採取を行う場合は DPO/IRB へ採取内容を説明し、ドライランで確認。

## 8. 運用（障害対応・復旧）

resolver は意図的に SPoF（§4）です。可用性は施設の HA 構成で担保する前提のため、
障害時に**運用者が何を確認し、復旧後どう再開するか**を定めます。障害時に運用者が見るのは
実行詳細画面の「ID解決経路」節（§6）と、`InputExecution.error_summary`（患者キー生値は
含まれずマスク値のみ。§6）です。波及範囲（実行全体の停止か、項目単位のスキップか）は
§4「resolver 障害の波及範囲」の表で切り分けます。

### シナリオ A: resolver 不通 / タイムアウト

- **症状**：自動収集（autofill 本番・trial run）が即 `failed`。error_summary に FHIR 接続エラー。
- **確認手順**：
  1. 院内 FHIR の稼働・疎通（`FHIR_SERVER_URL` へ `Patient?identifier=...` が応答するか）。
  2. `FHIR_SERVER_TIMEOUT`（既定 30 秒）に対し FHIR サーバの応答時間が収まっているか。
  3. ネットワーク経路（mTLS / ファイアウォール）。
- **復旧後の再開**：resolver 復旧を確認 → 対象1被験者で **trial run** を実行して疎通確認 →
  問題なければ本番 autofill を**再実行**（`failed` 実行は患者値を書いていないため、そのまま再実行で安全）。

### シナリオ B: 認証失敗（401 / 403）

- **症状**：即 `failed`。error_summary に FHIR サーバの 401 / 403。
  認証秘匿値はログ・例外に出力されません（§4）。
- **確認手順**：
  1. `FHIR_AUTH_MODE` と資格情報（Bearer トークン / Basic）の有効期限・失効。
  2. FHIR サーバ側でのトークン / アカウントの権限変更。
  - 資格情報が欠落したまま認証を有効化している場合は、**resolver 呼び出し時（autofill 実行時）に
    fail-fast** します（無認証送信しない）。この失敗はアプリ起動時ではなく当該実行が `failed` に
    なる形で表面化するため、起動ログではなく**該当実行の error_summary / 実行ログ**を確認。
- **復旧後の再開**：トークン/鍵をローテーションして `.env` を更新 → アプリ再起動（設定再読込） → trial run で疎通確認 → 本番再実行。

### シナリオ C: 不正応答 / 未登録被験者

- **症状（区別が重要）**：
  - **応答が壊れている**（スキーマ不正・5xx）→ 実行全体が `failed`（シナリオ A と同様に扱う）。
  - **応答は正常だが当該被験者が `not_found` / `ambiguous`** → その項目のみ `needs_input`、
    実行は `partial` で継続。
- **確認手順**（`not_found` / `ambiguous` の場合）：
  1. crosswalk（登録番号 → 各 key_type）にその被験者が登録済みか（外部 resolver 側で確認）。
  2. `ambiguous` は resolver が一意化しない契約（§4）。名寄せ元の重複を resolver / 外部系で解消。
- **復旧後の再開**：crosswalk へ登録・一意化 → 該当被験者だけ autofill を**個別再実行**。
  依然取得不能なら手入力（collector）へ縮退（[manual 縮退フロー](manual_fallback_flow.md)）。

### 設計判断：なぜ名寄せキャッシュを置かないか

KAWASEMI 側に crosswalk のキャッシュを**意図的に持ちません**。ステイルな名寄せ
（登録番号→患者キーの古い対応）による**患者取り違え**が、可用性低下より重大なリスクだからです。
キャッシュが古いまま別患者の値を引く事故は、GxP データインテグリティ上も許容できません。
可用性は resolver の HA 構成（施設責務）で担保する方針とし、将来キャッシュ導入を再検討する場合も
「取り違えリスク < 可用性メリット」を証明できない限り採用しません。

施設側 resolver / crosswalk への可用性・整合性・応答仕様の要件は、施設調整でそのまま配布できる
別紙 [`facility_resolver_requirements.md`](facility_resolver_requirements.md) にまとめています。
