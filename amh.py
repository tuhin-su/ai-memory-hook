#!/usr/bin/env python3
"""amh: AI-MEMORY-HOOK, 100% purely local, zero external service.
No server, no daemon, no network calls, no Ollama. Stdlib-only.

AUTOMATIC EXPERIENCE ENGINE: Automatically tracks tasks and code modifications
across sessions, synthesizing learned patterns with ZERO manual input.

HOOK output = System Instruction + Rules + Auto-Learned Experiences + BM25-ranked file-topic summaries.

  amh hook [prompt]        print the context block (reads Claude Code hook JSON on stdin)
  amh learn "task" --insight "..." [--outcome success|failure]   record explicit experience
  amh read path[:a-b]      read a file or a line range
  amh search "query"       BM25 search over file topics and past experiences
  amh note "title" --folder decisions --text "..."
  amh rule coding --append --text "- always ..."
  amh write .amh/docs/api.md [--append]     (content on stdin or --text; only inside .amh/)
  amh init | status | index

Thread engine: `hook` returns instantly from the existing index. If files changed, it starts a
detached background indexer (`amh index`, one at a time via a lock file). The current task is
never blocked, and the next hook sees the fresh index.
Storage: <project>/.amh/index.db (SQLite FTS5 + BM25 hash map). Rebuildable from files.
"""
import argparse
import hashlib
import json
import os
import re
import sqlite3
import subprocess
import sys
import time
from pathlib import Path

MAX_BYTES = 300_000
EXTS = {".md", ".txt", ".py", ".js", ".ts", ".tsx", ".jsx", ".go", ".rs", ".java", ".c", ".cpp",
        ".h", ".json", ".yaml", ".yml", ".toml", ".sh", ".html", ".css", ".sql"}
SKIP_DIRS = {"node_modules", "venv", ".venv", "__pycache__", "dist", "build", "target", ".git"}

DEFAULT_SYSTEM = """\
You have a persistent project memory and automatic cross-session experience engine (AI-MEMORY-HOOK).
Work autonomously; do NOT ask the user for permission to read, search, or write memory, rules, or docs.
Ask only before destructive actions or anything outside this project.

HOW IT WORKS & HOW TO USE IT
- <past_experiences> below lists lessons, gotchas, and solutions auto-learned across prior sessions.
  Review them first so you never repeat past mistakes or re-solve already solved problems!
- <file_topics> below lists the most relevant file topics, sorted best-first, as
  `score path:lines [topic] -> summary`. Summaries are hints. The real file is the truth.
- Read only what you need:   amh read path/to/file.py:40-112    (omit :lines for whole file)
- Search more:               amh search "natural language or code query"
- Never read everything. Read a file only if its summary matches your task.
- Indexing and experience learning run automatically in the background.

SAVING PERSISTENT KNOWLEDGE
- Automatic learning: amh automatically tracks your tasks and code changes across turns.
- Manual learning (optional):     amh learn "task title" --insight "..." --outcome success
- Architectural decision:         amh note "short title" --folder decisions --text "..."
- New permanent rule:             amh rule coding --append --text "- always ..."
- Documentation:                  amh write .amh/docs/architecture.md --text "..."

PROJECT LAYOUT
  <project>/
    .amh/
      system.md                 this instruction (editable)
      rules/<area>.md           hard rules, always injected (general, coding, security ...)
      memory/experiences/       auto-learned & manual experiences across sessions
      memory/<folder>/<slug>.md learned notes: decisions/ bugs/ conventions/ tasks/
      docs/<name>.md            architecture, api, setup
    <module>/<file>             source code
Never put notes or rules outside .amh/. Never put secrets in memory.
"""

SCHEMA = """
CREATE TABLE IF NOT EXISTS files(path TEXT PRIMARY KEY, fhash TEXT, mtime REAL);
CREATE VIRTUAL TABLE IF NOT EXISTS topics USING fts5(
    path, topic, l0 UNINDEXED, l1 UNINDEXED, summary, content,
    tokenize = 'porter unicode61'
);
"""

ROOT: Path = Path.cwd()
AMH: Path = ROOT / ".amh"


# ---------------------------------------------------------------- project + db
def find_root(cwd=None):
    if os.getenv("AMH_PROJECT"):
        return Path(os.environ["AMH_PROJECT"]).resolve(), True
    start = d = Path(cwd or os.getcwd()).resolve()
    while True:
        if (d / ".amh").is_dir() or (d / ".git").exists():
            return d, True
        if d.parent == d:
            return start, False
        d = d.parent


def set_root(root: Path):
    global ROOT, AMH
    ROOT, AMH = root, root / ".amh"


def db() -> sqlite3.Connection:
    AMH.mkdir(exist_ok=True)
    c = sqlite3.connect(AMH / "index.db", timeout=30)
    c.execute("PRAGMA journal_mode=WAL")
    c.executescript(SCHEMA)
    return c


def init_project():
    for d in ("rules", "docs", "memory/experiences", "memory/decisions", "memory/bugs", "memory/conventions"):
        (AMH / d).mkdir(parents=True, exist_ok=True)
    if not (AMH / "system.md").exists():
        (AMH / "system.md").write_text(DEFAULT_SYSTEM, encoding="utf-8")
    if not (AMH / "rules" / "general.md").exists():
        (AMH / "rules" / "general.md").write_text(
            "- Keep changes small and focused.\n- Update .amh/docs when architecture changes.\n",
            encoding="utf-8")
    if not (AMH / ".gitignore").exists():
        (AMH / ".gitignore").write_text("index.db*\nindex.lock\n.last_session.json\n", encoding="utf-8")


def safe(rel: str, inside_amh=False) -> Path:
    p = (ROOT / rel).resolve()
    base = (ROOT / ".amh").resolve() if inside_amh else ROOT.resolve()
    if p != base and base not in p.parents:
        sys.exit(f"[amh] path must stay inside {base}")
    return p


# ---------------------------------------------------------------- in-process summarizer
def summarize(path: str, topic: str, text: str) -> str:
    """Zero-server heuristic summary: extracts docstrings, markdown lead, or leading comments."""
    m = re.search(r'^(?:[ \t]*(?:def|class|async\s+def)[^\n]+\n)?[ \t]*(?:r|u|f)?("""|\'\'\')(.*?)\1',
                  text, re.DOTALL | re.MULTILINE)
    if m:
        doc = " ".join(m.group(2).strip().split())
        if doc:
            return doc[:200]
    out = []
    for line in text.splitlines():
        s = line.strip().strip("#/*-\"' ").strip()
        if not s or re.match(r"^(def|class|function|import|from|export|const|package|let|var)\b", s):
            continue
        out.append(s)
        if len(" ".join(out)) > 140:
            break
    return (" ".join(out) or topic)[:200]


_JS = r"^(?:export\s+)?(?:default\s+)?(?:async\s+)?(?:function|class|const)\s+(\w+)"
PATTERNS = {
    ".md": r"^#{1,4}\s+(.+)", ".txt": r"^#{1,4}\s+(.+)",
    ".py": r"^(?:async\s+def|def|class)\s+(\w+)",
    ".js": _JS, ".ts": _JS, ".jsx": _JS, ".tsx": _JS,
    ".go": r"^(?:func|type)\s+(?:\([^)]*\)\s*)?(\w+)",
    ".rs": r"^\s*(?:pub\s+)?(?:fn|struct|enum|impl|trait)\s+(\w+)",
    ".java": r"^\s*(?:public|private|protected)?\s*(?:static\s+)?(?:class|interface|enum)\s+(\w+)",
}


def split_topics(rel, text):
    lines = text.splitlines()
    if not lines:
        return []
    starts = []
    pat = PATTERNS.get(Path(rel).suffix)
    if pat:
        rx = re.compile(pat)
        for i, line in enumerate(lines):
            m = rx.match(line)
            if m:
                starts.append((i, m.group(1).strip()))
    if not starts or starts[0][0] > 0:
        starts.insert(0, (0, "top"))
    chunks = []
    for n, (s, name) in enumerate(starts):
        e = starts[n + 1][0] if n + 1 < len(starts) else len(lines)
        for a in range(s, e, 100):
            b = min(a + 100, e)
            label = name if a == s else f"{name}#{(a - s) // 100 + 1}"
            chunks.append((label, a + 1, b, "\n".join(lines[a:b])))
    return [c for c in chunks if c[3].strip()]


# ---------------------------------------------------------------- thread engine (detached indexer)
def iter_files():
    for dp, dns, fns in os.walk(ROOT):
        dns[:] = [d for d in dns if d not in SKIP_DIRS and (not d.startswith(".") or d == ".amh")]
        for f in fns:
            p = Path(dp) / f
            if p.suffix in EXTS:
                try:
                    if p.stat().st_size <= MAX_BYTES:
                        yield p
                except OSError:
                    pass


def changed(c):
    known = {p: (h, m) for p, h, m in c.execute("SELECT path,fhash,mtime FROM files")}
    seen, todo = set(), []
    for p in iter_files():
        rel = p.relative_to(ROOT).as_posix()
        seen.add(rel)
        try:
            m = p.stat().st_mtime
        except OSError:
            continue
        if rel not in known or known[rel][1] != m:
            todo.append((rel, p, m, known.get(rel, (None,))[0]))
    return todo, sorted(set(known) - seen)


def lock_path():
    return AMH / "index.lock"


def lock_fresh():
    lp = lock_path()
    return lp.exists() and time.time() - lp.stat().st_mtime < 600


def spawn_indexer():
    if lock_fresh():
        return
    kw = dict(stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
              cwd=str(ROOT), env={**os.environ, "AMH_PROJECT": str(ROOT)})
    if os.name == "nt":
        kw["creationflags"] = 0x00000008 | 0x00000200
    else:
        kw["start_new_session"] = True
    try:
        subprocess.Popen([sys.executable, os.path.abspath(__file__), "index"], **kw)
    except OSError:
        pass


def run_index():
    if lock_fresh():
        return
    lp = lock_path()
    lp.write_text(str(os.getpid()))
    try:
        c = db()
        for _ in range(5):
            todo, deleted = changed(c)
            if not todo and not deleted:
                break
            for rel, p, m, old_hash in todo:
                try:
                    data = p.read_bytes()
                    h = hashlib.sha1(data).hexdigest()
                    if h != old_hash:
                        index_file(c, rel, data)
                    with c:
                        c.execute("INSERT OR REPLACE INTO files VALUES(?,?,?)", (rel, h, m))
                except Exception as e:
                    print("index error", rel, e, file=sys.stderr)
                try:
                    os.utime(lp)
                except OSError:
                    pass
            for rel in deleted:
                with c:
                    c.execute("DELETE FROM files WHERE path=?", (rel,))
                    c.execute("DELETE FROM topics WHERE path=?", (rel,))
    finally:
        lp.unlink(missing_ok=True)


def index_file(c, rel, data):
    rows = []
    text = data.decode("utf-8", "replace")
    for topic, a, b, body in split_topics(rel, text):
        summ = summarize(rel, topic, body)
        rows.append((rel, topic, a, b, summ, body))
    with c:
        c.execute("DELETE FROM topics WHERE path=?", (rel,))
        c.executemany("INSERT INTO topics(path,topic,l0,l1,summary,content) VALUES(?,?,?,?,?,?)", rows)


# ---------------------------------------------------------------- automatic experience learning
def auto_learn_from_previous_session(c, current_prompt: str):
    """Automatically records learned patterns from previous session code modifications."""
    session_file = AMH / ".last_session.json"
    if not session_file.exists():
        save_current_session(session_file, current_prompt)
        return

    try:
        data = json.loads(session_file.read_text("utf-8"))
        prev_prompt = data.get("prompt", "").strip()
        prev_time = data.get("timestamp", 0)
    except Exception:
        save_current_session(session_file, current_prompt)
        return

    if len(prev_prompt) >= 6 and prev_time > 0:
        changed_files = []
        for p in iter_files():
            rel = p.relative_to(ROOT).as_posix()
            if rel.startswith(".amh/"):
                continue
            try:
                if p.stat().st_mtime > prev_time:
                    changed_files.append((rel, p))
            except OSError:
                continue

        if changed_files:
            slug = re.sub(r"[^a-z0-9]+", "-", prev_prompt.lower()).strip("-")[:50] or f"task-{int(prev_time)}"
            exp_file = AMH / "memory" / "experiences" / f"auto-{slug}.md"
            
            diff_topics = []
            for rel, p in changed_files[:5]:
                try:
                    text = p.read_text("utf-8", "replace")
                    for top, _, _, _ in split_topics(rel, text)[:3]:
                        if top != "top":
                            diff_topics.append(f"{rel}::{top}")
                except Exception:
                    pass

            file_list_str = ", ".join(rel for rel, _ in changed_files[:6])
            topics_str = ", ".join(diff_topics[:5]) if diff_topics else "File updates"
            
            content = f"""# Experience: {prev_prompt}
- Date: {time.strftime('%Y-%m-%d')}
- Mode: auto-learned
- Outcome: success
- Affected Files: {file_list_str}

## Key Insight / Learning
Auto-learned from previous turn '{prev_prompt}': Modified {file_list_str}. Key components involved: {topics_str}.
"""
            exp_file.parent.mkdir(parents=True, exist_ok=True)
            exp_file.write_text(content, "utf-8")
            try:
                index_file(c, exp_file.relative_to(ROOT).as_posix(), content.encode("utf-8"))
            except Exception:
                pass

    save_current_session(session_file, current_prompt)


def save_current_session(session_file: Path, prompt: str):
    try:
        session_file.write_text(json.dumps({
            "prompt": prompt,
            "timestamp": time.time()
        }), "utf-8")
    except Exception:
        pass


# ---------------------------------------------------------------- search & ranking (SQLite FTS5 BM25)
def tokenize_query(query: str):
    """Expands camelCase, snake_case, and extracts alphanumeric terms."""
    expanded = re.sub(r"([a-z])([A-Z])", r"\1 \2", query)
    tokens = re.findall(r"[a-zA-Z0-9_]{2,}", expanded)
    cleaned = []
    for t in tokens:
        sub = [s.lower() for s in t.split("_") if len(s) >= 2]
        cleaned.extend(sub or [t.lower()])
    return list(dict.fromkeys(cleaned))


def search_topics(c, query: str, k: int, path_filter: str = None):
    tokens = tokenize_query(query)
    if not tokens:
        sql = "SELECT path,topic,l0,l1,summary,0.0 FROM topics"
        params = []
        if path_filter:
            sql += " WHERE path LIKE ?"
            params.append(f"%{path_filter}%")
        sql += " LIMIT ?"
        params.append(k)
        rows = c.execute(sql, params).fetchall()
        return [dict(path=r[0], topic=r[1], l0=r[2], l1=r[3], summary=r[4], score=0.0) for r in rows]

    match_clause = " OR ".join(f'"{t}"*' for t in tokens)
    try:
        sql = """
            SELECT path, topic, l0, l1, summary, bm25(topics, 10.0, 10.0, 2.0, 1.0) AS score
            FROM topics
            WHERE topics MATCH ?
        """
        params = [match_clause]
        if path_filter:
            sql += " AND path LIKE ?"
            params.append(f"%{path_filter}%")
        sql += " ORDER BY score ASC LIMIT ?"
        params.append(k)
        rows = c.execute(sql, params).fetchall()
    except sqlite3.OperationalError:
        rows = []

    res = []
    for r in rows:
        s = round(abs(r[5]) if r[5] is not None else 0.0, 2)
        res.append(dict(path=r[0], topic=r[1], l0=r[2], l1=r[3], summary=r[4], score=s))
    return res


def read_text(p: Path) -> str:
    return p.read_text("utf-8", "replace").strip() if p.is_file() else ""


def build_hook(prompt, budget, k):
    c = db()
    
    # Auto-learn from previous session before building hook
    auto_learn_from_previous_session(c, prompt)

    system = read_text(AMH / "system.md") or DEFAULT_SYSTEM
    rules = "\n\n".join(f"## {p.stem}\n{read_text(p)}" for p in sorted((AMH / "rules").glob("*.md")))
    head = f"<system>\n{system}\n</system>\n<rules>\n{rules or '(none yet)'}\n</rules>\n"

    # Query matching past cross-session experiences (auto-learned + manual)
    exp_matches = search_topics(c, prompt, k=3, path_filter=".amh/memory/experiences/")
    exp_lines = []
    for em in exp_matches:
        exp_p = ROOT / em["path"]
        if exp_p.is_file():
            content = exp_p.read_text("utf-8", "replace")
            outcome_m = re.search(r"Outcome:\s*(\w+)", content)
            outcome = outcome_m.group(1).upper() if outcome_m else "LEARNED"
            insight_m = re.search(r"## Key Insight / Learning\n(.*?)(?:\n##|\Z)", content, re.DOTALL)
            insight = " ".join(insight_m.group(1).strip().split()) if insight_m else em["summary"]
            exp_lines.append(f"• [{outcome}] {em['topic']}: {insight[:300]}")

    exp_block = ""
    if exp_lines:
        exp_block = "<past_experiences>\n" + "\n".join(exp_lines) + "\n</past_experiences>\n"

    used = (len(head) + len(exp_block)) // 4
    lines = []

    for h in search_topics(c, prompt, k + 5):
        if h["path"].startswith(".amh/rules/") or h["path"] == ".amh/system.md" or h["path"].startswith(".amh/memory/experiences/"):
            continue
        line = f"{h['score']:.2f}  {h['path']}:{h['l0']}-{h['l1']} [{h['topic']}] -> {h['summary']}"
        if used + len(line) // 4 + 1 > budget or len(lines) >= k:
            break
        lines.append(line)
        used += len(line) // 4 + 1

    todo, deleted = changed(c)
    pending = len(todo) + len(deleted)
    if pending:
        spawn_indexer()

    return (head + exp_block + f'<file_topics sorted_by="relevance" pending_index="{pending}">\n'
            + "\n".join(lines) + "\n</file_topics>")


# ---------------------------------------------------------------- commands
def body(a):
    if hasattr(a, "text") and a.text is not None:
        return a.text
    if hasattr(a, "insight") and a.insight is not None:
        return a.insight
    return sys.stdin.read() if not sys.stdin.isatty() else ""


def write_file(rel, content, append):
    p = safe(rel, inside_amh=True)
    p.parent.mkdir(parents=True, exist_ok=True)
    content = content.rstrip() + "\n"
    if append and p.exists():
        old = p.read_text("utf-8")
        p.write_text(old + ("" if old.endswith("\n") else "\n") + content, "utf-8")
    else:
        p.write_text(content, "utf-8")
    spawn_indexer()
    print(json.dumps({"ok": True, "path": p.relative_to(ROOT).as_posix()}))


def record_experience(task_title: str, insight: str, outcome: str = "success", files: str = ""):
    """Explicitly records cross-session learning experience."""
    slug = re.sub(r"[^a-z0-9]+", "-", task_title.lower()).strip("-")[:60] or "task-experience"
    date_str = time.strftime('%Y-%m-%d')
    content = f"""# Experience: {task_title}
- Date: {date_str}
- Mode: explicit
- Outcome: {outcome.lower()}
- Affected Files: {files or '(none specified)'}

## Key Insight / Learning
{insight.strip()}
"""
    write_file(f".amh/memory/experiences/{slug}.md", content, append=False)


def main():
    ap = argparse.ArgumentParser(prog="amh")
    sub = ap.add_subparsers(dest="cmd", required=True)
    
    h = sub.add_parser("hook"); h.add_argument("prompt", nargs="*")
    h.add_argument("--budget", type=int, default=1500); h.add_argument("--k", type=int, default=8)
    
    lr = sub.add_parser("learn")
    lr.add_argument("task", help="Short summary of the task")
    lr.add_argument("--insight", required=True, help="What was learned, gotcha, or fix")
    lr.add_argument("--outcome", default="success", choices=["success", "failure", "partial"], help="Task outcome")
    lr.add_argument("--files", default="", help="Comma-separated files involved")
    
    s = sub.add_parser("search"); s.add_argument("query", nargs="+"); s.add_argument("--k", type=int, default=8)
    r = sub.add_parser("read"); r.add_argument("target")
    w = sub.add_parser("write"); w.add_argument("path"); w.add_argument("--append", action="store_true")
    w.add_argument("--text")
    n = sub.add_parser("note"); n.add_argument("title"); n.add_argument("--folder", default="decisions")
    n.add_argument("--text")
    ru = sub.add_parser("rule"); ru.add_argument("name"); ru.add_argument("--append", action="store_true")
    ru.add_argument("--text")
    for name in ("init", "status", "index"):
        sub.add_parser(name)
    a = ap.parse_args()

    if a.cmd == "hook":
        try:
            prompt, cwd = " ".join(a.prompt), None
            if not prompt and not sys.stdin.isatty():
                raw = sys.stdin.read()
                try:
                    j = json.loads(raw)
                    prompt, cwd = j.get("prompt") or j.get("user_prompt") or "", j.get("cwd")
                except Exception:
                    prompt = raw
            root, found = find_root(cwd)
            set_root(root)
            if not (AMH.is_dir() or found and (ROOT / ".git").exists() or os.getenv("AMH_AUTO_INIT")):
                return 0
            init_project()
            print(build_hook(prompt, a.budget, a.k))
        except Exception as e:
            print(f"[amh] hook skipped: {e}", file=sys.stderr)
        return 0

    root, _ = find_root()
    set_root(root)
    if a.cmd == "init":
        init_project(); spawn_indexer()
        print(f"[amh] ready: {AMH}")
    elif a.cmd == "index":
        run_index()
    elif a.cmd == "status":
        c = db()
        todo, deleted = changed(c)
        exp_count = len(list((AMH / "memory" / "experiences").glob("*.md"))) if (AMH / "memory" / "experiences").exists() else 0
        print(json.dumps({"root": str(ROOT),
                          "files": c.execute("SELECT COUNT(*) FROM files").fetchone()[0],
                          "topics": c.execute("SELECT COUNT(*) FROM topics").fetchone()[0],
                          "experiences": exp_count,
                          "pending": len(todo) + len(deleted), "indexer_running": lock_fresh(),
                          "engine": "sqlite-fts5-bm25 (pure-local, zero-server)"}, indent=2))
    elif a.cmd == "learn":
        record_experience(a.task, a.insight, a.outcome, a.files)
    elif a.cmd == "search":
        for x in search_topics(db(), " ".join(a.query), a.k):
            print(f"{x['score']:.2f}  {x['path']}:{x['l0']}-{x['l1']} [{x['topic']}] -> {x['summary']}")
    elif a.cmd == "read":
        path, start, end = a.target, 0, 0
        m = re.match(r"^(.*):(\d+)-(\d+)$", a.target)
        if m:
            path, start, end = m.group(1), int(m.group(2)), int(m.group(3))
        p = safe(path)
        if not p.is_file():
            sys.exit(f"[amh] not a file: {path}")
        lines = p.read_text("utf-8", "replace").splitlines()
        print("\n".join(lines[(start or 1) - 1:(end or len(lines))])[:200_000])
    elif a.cmd == "write":
        write_file(a.path, body(a), a.append)
    elif a.cmd == "note":
        slug = re.sub(r"[^a-z0-9]+", "-", a.title.lower()).strip("-")[:60] or "note"
        write_file(f".amh/memory/{a.folder}/{slug}.md",
                   f"# {a.title}\n_{time.strftime('%Y-%m-%d')}_\n\n{body(a).strip()}", False)
    elif a.cmd == "rule":
        write_file(f".amh/rules/{a.name}.md", body(a), a.append)
    return 0


if __name__ == "__main__":
    sys.exit(main())
