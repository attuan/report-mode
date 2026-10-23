#!/usr/bin/env python3
"""
report_mode.py — Claude Code「レポートモード」（生成AI利用記録）

役割
  1) フック (UserPromptSubmit / PostToolUse / Stop / SessionStart) として呼ばれ、
     ・利用者のプロンプト
     ・AIの回答（transcript から抽出）
     ・AIが書き込んだ箇所（対象ディレクトリ内ファイルの差分。Write/Edit だけでなく Bash 経由の変更も検知）
     ・利用者による修正（Claude 外で行われた変更の差分）
     を AI利用記録.md と log.jsonl に追記する。
  2) コマンド (on / off / status / note / rescan) としてスキルから呼ばれる。

使い方
  python3 report_mode.py hook <EventName>        # settings.json から呼ばれる（stdin に JSON）
  python3 report_mode.py on [対象パス] [--all]    # 対象ディレクトリ/ファイルを指定して開始
  python3 report_mode.py off
  python3 report_mode.py status
  python3 report_mode.py note <テキスト>
  python3 report_mode.py rescan                  # 手動で利用者修正を検知
  python3 report_mode.py preamble                # export 用の冒頭文（プロジェクト設定 > 同梱テンプレート）

データの保存先は各プロジェクトの .claude/report-mode/（state.json, log.jsonl, snapshots/, config.json）。
"""
import datetime
import difflib
import hashlib
import json
import os
import re
import shutil
import sys

SCRIPT = os.path.abspath(__file__)
PLUGIN_ROOT = os.path.dirname(os.path.dirname(SCRIPT))


def _find_project():
    env = os.environ.get("CLAUDE_PROJECT_DIR")
    if env:
        return os.path.abspath(env)
    d = os.getcwd()
    while True:  # 既に記録中のプロジェクトがあれば上位ディレクトリから探す
        if os.path.exists(os.path.join(d, ".claude", "report-mode", "state.json")):
            return d
        parent = os.path.dirname(d)
        if parent == d:
            return os.getcwd()
        d = parent


PROJECT = _find_project()
BASE = os.path.join(PROJECT, ".claude", "report-mode")
CONFIG_PATH = os.path.join(BASE, "config.json")
PREAMBLE_OVERRIDE = os.path.join(BASE, "export_preamble.txt")
PREAMBLE_TEMPLATE = os.path.join(PLUGIN_ROOT, "templates", "export_preamble.txt")
STATE_PATH = os.path.join(BASE, "state.json")
SNAP_DIR = os.path.join(BASE, "snapshots")
RAW_LOG = os.path.join(BASE, "log.jsonl")
ERR_LOG = os.path.join(BASE, "hook_errors.log")

LOG_BASENAME = "AI利用記録.md"
DEFAULT_EXCLUDE_DIRS = {"out", ".claude", ".git", "node_modules", "__pycache__", ".venv"}
EXCLUDE_EXT = {".aux", ".log", ".dvi", ".pdf", ".nav", ".snm", ".toc", ".out", ".gz", ".fls",
               ".fdb_latexmk", ".bbl", ".blg", ".bcf", ".xml", ".synctex", ".png", ".jpg", ".jpeg",
               ".gif", ".zip", ".pyc", ".ds_store"}
MAX_FILE_BYTES = 2 * 1024 * 1024
MAX_MD_CHARS = 20000       # md に載せる回答/差分の上限（全文は log.jsonl に残す）


def load_config():
    """プロジェクトの .claude/report-mode/config.json（任意）
    例: {"exclude_dirs": ["private", "data"], "max_md_chars": 50000}"""
    try:
        with open(CONFIG_PATH, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, json.JSONDecodeError):
        return {}


_CFG = load_config()
EXCLUDE_DIRS = DEFAULT_EXCLUDE_DIRS | set(_CFG.get("exclude_dirs", []))
MAX_MD_CHARS = int(_CFG.get("max_md_chars", MAX_MD_CHARS))


# ---------------------------------------------------------------- utils
def now():
    return datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def load_state():
    if os.path.exists(STATE_PATH):
        with open(STATE_PATH, encoding="utf-8") as f:
            return json.load(f)
    return {"on": False, "target": None, "log_md": None, "scope": "session",
            "sessions": [], "pending_bind": False, "counter": 0, "manifest": {}, "turns": {}}


def save_state(st):
    tmp = STATE_PATH + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(st, f, ensure_ascii=False, indent=1)
    os.replace(tmp, STATE_PATH)


def sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def display_path(p):
    try:
        rel = os.path.relpath(p, PROJECT)
        return rel if not rel.startswith("..") else p
    except ValueError:
        return p


def is_text(data: bytes) -> bool:
    return b"\x00" not in data[:8192]


def iter_target_files(target):
    """対象（ディレクトリまたは単一ファイル）配下の記録対象ファイルを絶対パスで列挙"""
    if os.path.isfile(target):
        yield target
        return
    for root, dirs, files in os.walk(target):
        dirs[:] = [d for d in dirs if d not in EXCLUDE_DIRS and not d.startswith(".")]
        for fn in files:
            if fn.startswith("."):
                continue
            if os.path.splitext(fn)[1].lower() in EXCLUDE_EXT:
                continue
            if fn == LOG_BASENAME or fn.endswith("_" + LOG_BASENAME):
                continue
            p = os.path.join(root, fn)
            try:
                if os.path.getsize(p) > MAX_FILE_BYTES:
                    continue
            except OSError:
                continue
            yield p


def read_bytes(p):
    try:
        with open(p, "rb") as f:
            return f.read()
    except OSError:
        return None


def snap_path(rel):
    return os.path.join(SNAP_DIR, rel)


def write_snapshot(rel, data: bytes):
    p = snap_path(rel)
    os.makedirs(os.path.dirname(p), exist_ok=True)
    with open(p, "wb") as f:
        f.write(data)


def read_snapshot(rel):
    return read_bytes(snap_path(rel))


def rel_key(target, p):
    return os.path.basename(p) if os.path.isfile(target) else os.path.relpath(p, target)


def build_manifest(st):
    """対象ファイルのハッシュ一覧とスナップショットを作り直す"""
    target = st["target"]
    manifest = {}
    if os.path.isdir(SNAP_DIR):
        shutil.rmtree(SNAP_DIR)
    for p in iter_target_files(target):
        data = read_bytes(p)
        if data is None or not is_text(data):
            continue
        rel = rel_key(target, p)
        manifest[rel] = sha(data)
        write_snapshot(rel, data)
    st["manifest"] = manifest


# ---------------------------------------------------------------- logging
def md_append(st, text):
    with open(st["log_md"], "a", encoding="utf-8") as f:
        f.write(text.rstrip("\n") + "\n\n")


def raw_append(rec):
    rec = dict(rec)
    rec.setdefault("time", now())
    with open(RAW_LOG, "a", encoding="utf-8") as f:
        f.write(json.dumps(rec, ensure_ascii=False) + "\n")


_STRIP_TAGS = ("ide_opened_file", "ide_selection", "system-reminder")


def clean_prompt(text):
    """Claude Code / IDE が付加するタグ（<ide_opened_file> 等）を取り除く"""
    for tag in _STRIP_TAGS:
        text = re.sub(rf"<{tag}>.*?</{tag}>", "", text or "", flags=re.S)
    return text.strip()


def blockquote(text):
    lines = (text or "").rstrip("\n").split("\n")
    return "\n".join("> " + ln if ln.strip() else ">" for ln in lines)


def fence(text, lang=""):
    text = (text or "").rstrip("\n")
    return f"````{lang}\n{text}\n````"


def clip(text, limit=None):
    limit = limit or MAX_MD_CHARS
    if len(text) <= limit:
        return text
    return text[:limit] + f"\n…（{len(text) - limit} 文字省略。全文は .claude/report-mode/log.jsonl を参照）"


def ensure_log_header(st):
    if os.path.exists(st["log_md"]):
        return
    scope = "このセッションのみ" if st["scope"] == "session" else "全セッション"
    header = (
        "# 生成AI利用記録\n\n"
        "本ファイルは Claude Code のフックにより自動生成・追記されています。\n\n"
        "- 利用ツール: Claude Code（使用モデルは各回答の見出しに記載）\n"
        f"- 記録対象: `{display_path(st['target'])}`\n"
        f"- 記録範囲: {scope}\n"
        f"- 記録開始: {now()}\n\n"
        "凡例: **プロンプト**＝利用者が入力した指示 / **AIの回答**＝Claude の返答本文 / "
        "**AIが書き込んだ箇所**＝Claude がファイルに加えた変更の差分 / "
        "**利用者による修正**＝AI書き込み後に Claude 外（エディタ等）で加えられた変更の差分\n\n---"
    )
    md_append(st, header)


def record_changes(st, attribution, tool=None, tool_hint=None):
    """manifest と現状を比較し、変更があれば差分を記録。attribution: 'ai' | 'user'"""
    target = st["target"]
    manifest = st.get("manifest", {})
    current = {}
    changes = []
    for p in iter_target_files(target):
        data = read_bytes(p)
        if data is None or not is_text(data):
            continue
        rel = rel_key(target, p)
        h = sha(data)
        current[rel] = h
        if manifest.get(rel) != h:
            old = read_snapshot(rel) if rel in manifest else b""
            changes.append((rel, "追加" if rel not in manifest else "変更", old, data))
            write_snapshot(rel, data)
    for rel in manifest:
        if rel not in current:
            old = read_snapshot(rel) or b""
            changes.append((rel, "削除", old, b""))
            sp = snap_path(rel)
            if os.path.exists(sp):
                os.remove(sp)
    st["manifest"] = current
    if not changes:
        return []

    t = now()
    if attribution == "ai":
        title = f"### AIが書き込んだ箇所（ツール: {tool or '不明'}）"
    else:
        title = "### 利用者による修正（Claude 外での変更を検知）"
    parts = [title]
    for rel, kind, old, new in changes:
        old_s = old.decode("utf-8", "replace")
        new_s = new.decode("utf-8", "replace")
        diff = "".join(difflib.unified_diff(
            old_s.splitlines(True), new_s.splitlines(True),
            fromfile=f"変更前/{rel}", tofile=f"変更後/{rel}", n=2))
        if not diff.endswith("\n"):
            diff += "\n"
        shown = display_path(os.path.join(target, rel)) if os.path.isdir(target) else display_path(target)
        parts.append(f"- `{shown}`（{kind}、{t}）\n\n{fence(clip(diff), 'diff')}")
        raw_append({"event": "change", "attribution": attribution, "tool": tool, "tool_hint": tool_hint,
                    "file": shown, "kind": kind, "diff": diff, "entry": st.get("counter")})
    md_append(st, "\n\n".join(parts))
    return changes


# ---------------------------------------------------------------- transcript
def extract_last_turn(transcript_path):
    """transcript(JSONL) から最後の利用者プロンプトとその後の AI 回答テキストを取り出す"""
    if not transcript_path or not os.path.exists(transcript_path):
        return None, "", None
    entries = []
    with open(transcript_path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                entries.append(json.loads(line))
            except json.JSONDecodeError:
                continue

    def user_text(e):
        if e.get("type") != "user" or e.get("isSidechain"):
            return None
        c = (e.get("message") or {}).get("content")
        if isinstance(c, str):
            return c
        if isinstance(c, list):
            if any(isinstance(b, dict) and b.get("type") == "tool_result" for b in c):
                return None
            texts = [b.get("text", "") for b in c if isinstance(b, dict) and b.get("type") == "text"]
            return "\n".join(texts) if texts else None
        return None

    last_idx = None
    for i in range(len(entries) - 1, -1, -1):
        if user_text(entries[i]) is not None:
            last_idx = i
            break
    if last_idx is None:
        return None, "", None
    prompt = user_text(entries[last_idx])
    texts, model = [], None
    for e in entries[last_idx + 1:]:
        if e.get("type") != "assistant" or e.get("isSidechain"):
            continue
        msg = e.get("message") or {}
        model = msg.get("model") or model
        c = msg.get("content")
        show_updates = e.get("thinkingDisplay") == "updates"
        if isinstance(c, str):
            texts.append(c)
        elif isinstance(c, list):
            for b in c:
                if not isinstance(b, dict):
                    continue
                if b.get("type") == "text" and b.get("text"):
                    texts.append(b["text"])
                elif b.get("type") == "thinking" and show_updates and (b.get("thinking") or "").strip():
                    # 利用者に途中経過として表示されたテキスト
                    texts.append(b["thinking"].strip())
    return clean_prompt(prompt), "\n\n".join(t.strip() for t in texts if t.strip()), model


# ---------------------------------------------------------------- activation
def is_active(st, sid):
    if not st.get("on"):
        return False
    if st.get("scope") == "all":
        return True
    if st.get("pending_bind") and sid:
        st["sessions"] = sorted(set(st.get("sessions", []) + [sid]))
        st["pending_bind"] = False
        save_state(st)
        return True
    return sid in st.get("sessions", [])


# ---------------------------------------------------------------- hooks
def script_hint():
    return f'[report-mode] スクリプト: python3 "{SCRIPT}"'


def hook_user_prompt(st, data):
    sid = data.get("session_id")
    prompt = clean_prompt(data.get("prompt", ""))
    if prompt.lstrip().lstrip("/").startswith(("report-mode", "report-mode:report-mode")):
        print(script_hint())  # スキルがスクリプトの場所を知るための文脈
    if not is_active(st, sid):
        return
    # /report-mode 系の操作自体は記録しない
    if prompt.strip().startswith("/report-mode"):
        return
    record_changes(st, "user")
    st["counter"] = st.get("counter", 0) + 1
    n = st["counter"]
    st.setdefault("turns", {})[sid] = n
    md_append(st, f"---\n\n## [{n}] {now()}\n\n### プロンプト（利用者）\n\n{blockquote(prompt)}")
    raw_append({"event": "prompt", "entry": n, "session": sid, "prompt": prompt})
    save_state(st)
    print(f"[レポートモード ON] この対話は記録 #{n} として {display_path(st['log_md'])} に自動記録されます。"
          "レポートに書いた内容・根拠は後で利用者が確認できるよう、回答では何をどこに書いたかを明示してください。")


def hook_post_tool(st, data):
    sid = data.get("session_id")
    if not is_active(st, sid):
        return
    tool = data.get("tool_name")
    ti = data.get("tool_input") or {}
    hint = ti.get("file_path") or ti.get("notebook_path") or (ti.get("command", "")[:200] if tool == "Bash" else None)
    record_changes(st, "ai", tool=tool, tool_hint=hint)
    save_state(st)


def hook_stop(st, data):
    sid = data.get("session_id")
    just_bound = st.get("on") and st.get("pending_bind") and st.get("scope") != "all"
    if not is_active(st, sid):
        return
    if just_bound:
        # /report-mode on を実行したターン自体は記録しない（紐付けのみ）
        return
    n = st.get("turns", {}).get(sid)
    if n is None:
        return  # このセッションでプロンプトを記録していない（例: ON直後の中断）
    record_changes(st, "user")
    _, answer, model = extract_last_turn(data.get("transcript_path"))
    # Stop 時点では最後の返答が transcript に未書き込みのことがあるため、入力の値で補う
    last = (data.get("last_assistant_message") or "").strip()
    if last and not answer.rstrip().endswith(last):
        answer = (answer.rstrip() + "\n\n" + last).strip()
    model_s = f"（モデル: {model}）" if model else ""
    body = blockquote(clip(answer)) if answer.strip() else "> （本文なし／ツール操作のみ）"
    md_append(st, f"### AIの回答{model_s}\n\n{body}")
    md_append(st, "### 回答の修正（利用者記入）\n\n"
                  "> （AIの回答をどう修正・取捨選択したかをここに記入。そのまま採用した場合は「修正なし」。"
                  "手入力のほか、Claude 外で直した差分は次のプロンプト送信時に自動で下に追記されます）")
    raw_append({"event": "answer", "entry": n, "session": sid, "model": model, "answer": answer})
    st["turns"].pop(sid, None)
    save_state(st)


def hook_session_start(st, data):
    sid = data.get("session_id")
    if not st.get("on"):
        return
    if st.get("scope") == "all" or sid in st.get("sessions", []):
        record_changes(st, "user")
        save_state(st)
        print(f"[レポートモード ON] 対象: {display_path(st['target'])} / 記録先: {display_path(st['log_md'])}")
        print(script_hint())
    else:
        print("[レポートモード] 他のセッションで ON になっています。このセッションでも記録する場合は /report-mode on を実行してください。")


# ---------------------------------------------------------------- commands
def cmd_on(args):
    st = load_state()
    scope = "all" if "--all" in args else "session"
    paths = [a for a in args if not a.startswith("--")]
    target = os.path.abspath(os.path.join(PROJECT, paths[0])) if paths else PROJECT
    if not os.path.exists(target):
        print(f"対象が存在しません: {target}")
        return 1
    if os.path.isfile(target):
        stem = os.path.splitext(os.path.basename(target))[0]
        log_md = os.path.join(os.path.dirname(target), f"{stem}_{LOG_BASENAME}")
    else:
        log_md = os.path.join(target, LOG_BASENAME)
    st.update({"on": True, "target": target, "log_md": log_md, "scope": scope,
               "pending_bind": scope == "session", "turns": {}})
    if scope == "all":
        st["sessions"] = []
    os.makedirs(BASE, exist_ok=True)
    gi = os.path.join(BASE, ".gitignore")
    if not os.path.exists(gi):
        with open(gi, "w", encoding="utf-8") as f:
            f.write("# report-mode の内部データ（プロンプト全文を含む）\n*\n!.gitignore\n!config.json\n!export_preamble.txt\n")
    build_manifest(st)
    ensure_log_header(st)
    md_append(st, f"（記録開始: {now()} / 対象: `{display_path(target)}` / 範囲: {'全セッション' if scope == 'all' else 'このセッション'}）")
    save_state(st)
    print(f"レポートモード ON\n  対象: {display_path(target)}\n  記録先: {display_path(log_md)}\n"
          f"  範囲: {'全セッション' if scope == 'all' else 'このセッションのみ（次のやり取りから記録）'}\n"
          f"  監視ファイル数: {len(st['manifest'])}")
    return 0


def cmd_off(_args):
    st = load_state()
    if not st.get("on"):
        print("レポートモードは既に OFF です。")
        return 0
    record_changes(st, "user")
    md_append(st, f"（記録終了: {now()}）")
    st.update({"on": False, "sessions": [], "pending_bind": False, "turns": {}})
    save_state(st)
    print(f"レポートモード OFF（記録先: {display_path(st['log_md'])}）")
    return 0


def cmd_status(_args):
    st = load_state()
    if not st.get("on"):
        print("レポートモード: OFF")
        return 0
    print("レポートモード: ON")
    print(f"  対象: {display_path(st['target'])}")
    print(f"  記録先: {display_path(st['log_md'])}")
    print(f"  範囲: {'全セッション' if st['scope'] == 'all' else 'セッション限定'}"
          f"{'（次のフックでセッションに紐付け）' if st.get('pending_bind') else ''}")
    print(f"  紐付け済みセッション数: {len(st.get('sessions', []))}")
    print(f"  記録件数: {st.get('counter', 0)} / 監視ファイル数: {len(st.get('manifest', {}))}")
    return 0


def cmd_note(args):
    st = load_state()
    if not st.get("on"):
        print("レポートモードが OFF のためメモを追記できません。")
        return 1
    text = " ".join(args).strip()
    if not text:
        print("メモの本文を指定してください。")
        return 1
    md_append(st, f"### メモ（利用者、{now()}）\n\n{blockquote(text)}")
    raw_append({"event": "note", "entry": st.get("counter"), "note": text})
    print("メモを追記しました。")
    return 0


def cmd_rescan(_args):
    st = load_state()
    if not st.get("on"):
        print("レポートモードは OFF です。")
        return 1
    ch = record_changes(st, "user")
    save_state(st)
    print(f"検知した変更: {len(ch)} 件")
    return 0


def cmd_preamble(_args):
    path = PREAMBLE_OVERRIDE if os.path.exists(PREAMBLE_OVERRIDE) else PREAMBLE_TEMPLATE
    print(f"# 冒頭文の出典: {path}")
    with open(path, encoding="utf-8") as f:
        print(f.read().rstrip("\n"))
    return 0


def run_hook(event):
    try:
        data = json.load(sys.stdin)
    except Exception:
        data = {}
    st = load_state()
    if not st.get("on") and event != "UserPromptSubmit":
        return 0
    if event == "UserPromptSubmit":
        hook_user_prompt(st, data)
    elif event == "PostToolUse":
        hook_post_tool(st, data)
    elif event == "Stop":
        hook_stop(st, data)
    elif event == "SessionStart":
        hook_session_start(st, data)
    return 0


def main(argv):
    if not argv:
        print(__doc__)
        return 0
    cmd, args = argv[0], argv[1:]
    if cmd == "hook":
        return run_hook(args[0] if args else "")
    return {"on": cmd_on, "off": cmd_off, "status": cmd_status,
            "note": cmd_note, "rescan": cmd_rescan, "preamble": cmd_preamble}.get(cmd, lambda a: (print(__doc__), 1)[1])(args)


if __name__ == "__main__":
    try:
        sys.exit(main(sys.argv[1:]) or 0)
    except SystemExit:
        raise
    except Exception as e:  # フックで利用者の作業を止めないため、失敗はログに残して正常終了
        import traceback
        os.makedirs(BASE, exist_ok=True)
        with open(ERR_LOG, "a", encoding="utf-8") as f:
            f.write(f"[{now()}] {sys.argv[1:]}\n{traceback.format_exc()}\n")
        if sys.argv[1:2] != ["hook"]:
            print(f"エラー: {e}（詳細は {ERR_LOG}）")
            sys.exit(1)
        sys.exit(0)
