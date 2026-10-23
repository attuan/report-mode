---
name: report-mode
description: レポートモード（生成AI利用記録 / generative-AI use log for academic reports）の操作。ON にすると、このセッションのプロンプト・AIの回答・AIが書き込んだ箇所・利用者による修正がフックで AI利用記録.md に自動追記される。/report-mode on [対象パス] [--all] | off | status | note <文> | rescan | export [tex|md]
argument-hint: on [path] [--all] | off | status | note <text> | rescan | export [tex|md]
---

# レポートモード（生成AI利用記録）

大学レポートで求められる「生成AI利活用の明記」のため、Claude Code のフックが以下を
`AI利用記録.md`（対象ディレクトリ内、対象がファイルなら `<ファイル名>_AI利用記録.md`）に自動追記する。
生データは `.claude/report-mode/log.jsonl` に全文保存される。

- **プロンプト**: 利用者の入力（UserPromptSubmit フック）
- **AIの回答**: Claude の返答本文（Stop フックが transcript から抽出）
- **AIが書き込んだ箇所**: 対象内ファイルの差分。Write/Edit だけでなく Bash 経由の変更も検知（PostToolUse フック）
- **利用者による修正**: AI 書き込み後に Claude 外（エディタ等）で変更された差分（次のプロンプト／Stop／セッション開始時に検知）

引数: `$ARGUMENTS`

## サブコマンドごとの手順

すべてのコマンドはプロジェクトルートで Bash ツールから実行する。スクリプトの場所は次の順で決める。

1. 文脈にフックが出力した `[report-mode] スクリプト: python3 "<パス>"` があれば、そのコマンドを使う。
2. なければ `python3 "${CLAUDE_PLUGIN_ROOT}/scripts/report_mode.py"` を試す。
3. それも失敗したら `find ~/.claude/plugins -path "*report-mode/scripts/report_mode.py" | head -1` で探す。

以下の例では、このコマンドを `RM` と表記する。実行結果はそのまま利用者に伝える。

### `on [対象パス] [--all]`
```bash
RM on <対象パス があれば> <--all があれば>
```
- 対象パス省略時はプロジェクト全体（`out`, `.claude`, `.git`, LaTeX の中間生成物は除外。追加の除外は `.claude/report-mode/config.json` の `exclude_dirs`）。
- 対象には `.tex` ファイル単体も指定できる（その場合はそのファイルだけ監視）。
- 既定は**このセッションのみ**記録。`--all` で全セッション対象。
- 実行後、利用者に「次のやり取りから記録される」ことと記録先を伝える。
- ON 中は、レポート本文に何をどこへ書いたかを回答の中で明示すること（記録の読み手が利用箇所を追えるように）。

### `off`
```bash
RM off
```

### `status`
```bash
RM status
```

### `note <文>`
利用者の補足（例: 「第3段落はAI案を元に自分で書き直した」）を記録に追記する。
```bash
RM note "<文>"
```

### `rescan`
利用者が手で直した差分を今すぐ記録に反映する。
```bash
RM rescan
```

### `export [tex|md]`（既定: tex）
記録 md を読み、レポートに添付する「生成AI利用記録」ページを作る。**既定はプロンプト全文・回答全文を転記**し、
各記録に利用者が手打ちする「修正」欄を設ける（要約版は利用者が「要約で」と言った場合のみ）。手順:

1. `RM status` で記録先を確認し、その `AI利用記録.md` を読む。
2. 対象レポートの `\documentclass` を確認する。
   - beamer → `\begin{frame}[allowframebreaks]{生成AI利用記録} ... \end{frame}`（長文は複数 frame に分ける）
   - それ以外（jsarticle 等） → `\section*{生成AIの利用について}`
3. ページの構成（この順で）:
   1. **冒頭文**: `RM preamble` の出力（1行目の出典コメントを除く）を**一字一句そのまま**載せる（段落ごとに `\par`）。
   2. **利用環境**: 「利用ツール: Claude Code（モデル: 記録 md の各回答見出しにあるモデル名をすべて列挙）」「記録期間」「対象ファイル」。
   3. **記録一覧**: 記録番号 `[n]` ごとに、次の4項目を**すべて**載せる。
      - **プロンプト（全文）**: md の引用部分を省略せず転記。
      - **AIの回答（全文）**: md の引用部分を省略せず転記。md 内で「…文字省略」とある場合は `.claude/report-mode/log.jsonl` の `answer` から全文を取る。
      - **AIが書き込んだ箇所**: ファイル名と差分の要点（差分全文は長いので、変更行数と該当節・段落を示す。利用者が求めれば差分全文）。
      - **修正**: md の「回答の修正（利用者記入）」欄と、自動検知された「利用者による修正」差分の要点を載せる。
        利用者記入欄が未記入のままなら、`.tex` 側にも手書き用の空欄を残す:
        `\noindent\textbf{修正:}\ \underline{\hspace{10cm}}` に続けて空行を2〜3行（`\vspace{1.5em}`）。
4. 整形ルール:
   - プロンプト・回答の全文は `\begin{quote} ... \end{quote}` に入れ、改行は `\\`、空行は段落分けにする。
   - LaTeX 特殊文字（`_ % & # $ { } \ ^ ~ < >`）は必ずエスケープする。回答中のコードブロック・数式の生テキストは `\begin{verbatim}`（beamer では `[fragile]` が必要）に入れる。
   - 記録どうしは `\hrulefill` か `\subsection*{記録 n（日時）}` で区切る。
5. `<レポート名>_AI利用記録.tex` として対象レポートと同じディレクトリに保存し、
   `\end{document}` の直前に `\input{<レポート名>_AI利用記録}` を追加するよう案内する（利用者が頼めば Edit ツールで挿入する）。
   `md` 指定時は同内容を `<レポート名>_AI利用記録_付録.md` として保存する（冒頭文・全文転記・修正欄は同じ）。

## 注意
- 利用者が除外を指定したディレクトリ（`config.json` の `exclude_dirs`）は監視対象外であり、読まないこと。
- 付録冒頭文を変えたいときは `.claude/report-mode/export_preamble.txt` をプロジェクト側に作る（同梱テンプレートより優先）。
- 記録データ（`.claude/report-mode/`）にはプロンプト全文が含まれる。`on` 実行時に `.gitignore` が自動生成される。
- フックは失敗しても利用者の作業を止めない（エラーは `.claude/report-mode/hook_errors.log`）。
- セッションを新しく開いた場合、既定（セッション限定）では再度 `/report-mode on` が必要。
