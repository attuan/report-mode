# report-mode

Claude Code で大学のレポートなどを書くときに、**生成AIの利用内容を自動で記録し、レポートの付録として出力する**プラグインです。

[English](#english) is below.

## なぜ作ったか

チャット形式の生成AIなら、プロンプトと回答を後からまとめるのは簡単です。しかし Claude Code のようなエージェント型のツールでは、ファイルが細かく何度も書き換えられるため、「何を頼み、AIが何を書き、自分がどう直したか」を手作業で記録するのは困難です。

このプラグインは Claude Code のフックでそれらを自動保存し、提出用の開示ページ（LaTeX / Markdown）を作れるようにします。

## 記録される内容

レポートモードを ON にすると、次の内容が `AI利用記録.md` に自動で追記されます。

| 項目 | 取得方法 |
|---|---|
| プロンプト | 利用者の入力（UserPromptSubmit フック） |
| AIの回答 | Claude の返答本文（Stop フックが会話ログから抽出） |
| AIが書き込んだ箇所 | 対象ファイルの差分。Write/Edit だけでなく Bash 経由の変更も検知（PostToolUse フック） |
| 利用者による修正 | AI の書き込み後にエディタ等で直した差分を自動検知 |
| 回答の修正（手書き欄） | 各回答の後に、利用者が自由に記入できる欄 |

全文は `.claude/report-mode/log.jsonl` にも保存されます。

## インストール

Claude Code で次を実行します。

```
/plugin marketplace add attuan/report-mode
/plugin install report-mode@claude-report-mode
```

Python 3 が必要です。追加のライブラリは不要です。

## 使い方

```
/report-mode on                     # プロジェクト全体を対象に、このセッションだけ記録
/report-mode on report.tex          # 特定のファイルだけ監視
/report-mode on --all               # 全セッションで記録
/report-mode note 第3段落はAI案を元に自分で書き直した
/report-mode status
/report-mode rescan                 # 手で直した差分を今すぐ記録
/report-mode off
/report-mode export                 # レポートに \input する付録 .tex を生成
/report-mode export md              # Markdown で生成
```

`export` は既定でプロンプト全文と回答全文を転記し、記録ごとに「修正」欄を設けます。beamer のスライドなら frame として、それ以外は `\section*` として生成します。生成後、`\end{document}` の直前に次の1行を入れれば付録になります。

```latex
\input{report_AI利用記録}
```

### 記録の例

````markdown
## [1] 2026-10-20 16:03:41

### プロンプト（利用者）

> 序論を書いて

### AIが書き込んだ箇所（ツール: Edit）

- `report.tex`（変更、2026-10-20 16:03:55）

```diff
 \section{はじめに}
+本稿では傾向スコアを扱う。
 \section{方法}
```

### AIの回答（モデル: claude-opus-5-5）

> 「はじめに」節の冒頭に1文を追加しました。

### 回答の修正（利用者記入）

> 主題を「推定方法の検討」に書き直した

### 利用者による修正（Claude 外での変更を検知）

```diff
-本稿では傾向スコアを扱う。
+本稿では傾向スコアの推定方法を検討する。
```
````

## 設定

すべて任意です。プロジェクトの `.claude/report-mode/` に置きます。

- **`config.json`**: 監視から外すディレクトリなどを指定します。

  ```json
  { "exclude_dirs": ["private", "data"], "max_md_chars": 50000 }
  ```

  `out`, `.git`, `.claude`, `node_modules` と、LaTeX の中間生成物や PDF は最初から除外されています。
- **`export_preamble.txt`**: 付録ページの冒頭文です。置かなければ同梱のテンプレート（`plugins/report-mode/templates/export_preamble.txt`）が使われます。

## 注意点と制限

- **補完機能は記録できません。** GitHub Copilot などエディタの補完で書き足された文章は、Claude 外の変更として「利用者による修正」に分類されます。正確な開示が必要な場合は補完をオフにしてください。
- **記録にはプロンプト全文が含まれます。** `on` を実行すると `.claude/report-mode/.gitignore` が自動で作られますが、`AI利用記録.md` 自体を公開リポジトリに含めるかはご自身で判断してください。
- **回答の抽出は Claude Code の内部ログ形式に依存しています。** 公式に保証された仕様ではないため、将来のバージョンで回答が取れなくなる可能性があります。その場合も作業は止まらず、エラーは `.claude/report-mode/hook_errors.log` に出ます。
- フックは全プロジェクトで動きますが、OFF のときは何もせずに終了します。
- 大学や授業ごとの開示ルールを満たすかは、各自で確認してください。

## ライセンス

MIT

---

## English

**report-mode** is a Claude Code plugin that automatically logs how you used generative AI while writing an academic report, and exports the log as an appendix (LaTeX or Markdown).

When report mode is on, hooks record each **prompt**, **Claude's answer**, the **diffs Claude wrote** to your files, and the **edits you made yourself** afterwards. Each entry also has a free-text field for describing how you revised the answer.

```
/plugin marketplace add attuan/report-mode
/plugin install report-mode@claude-report-mode

/report-mode on [path] [--all]
/report-mode off | status | note <text> | rescan | export [tex|md]
```

Notes:

- The UI text and output are in Japanese.
- Editor completions such as GitHub Copilot cannot be attributed to AI and show up as your own edits.
- Answer extraction relies on Claude Code's internal transcript format, which may change.
- Requires Python 3 with no extra packages. Licensed under MIT.
