#!/usr/bin/env python3
"""amh: AI-MEMORY-HOOK — 100% purely local, zero external service.
No server, no daemon, no network calls, no Ollama. Stdlib-only.

GLOBAL STORAGE: All data lives in ~/.amh/ regardless of which project you're in.
Per-project data is under ~/.amh/projects/<project_name>/.
A central hash_map tracks every indexed file → project, so context can be
recovered even when the project folder is unknown or you're working from anywhere.

STORAGE LAYOUT:
  ~/.amh/
    projects/<name>/
      system.md                 system instructions for this project
      rules/<area>.md           hard rules injected every turn
      memory/experiences/       cross-session learnings (auto + manual)
      memory/decisions/         architecture decisions
      memory/bugs/              bug fixes & gotchas
      memory/conventions/       team conventions
      docs/<name>.md            architecture / api / setup docs
      vector.db                 SQLite FTS5 per-project search index
      .last_session.json        previous session tracker for auto-learning
    hash_map/
      files.db                  maps every file path + hash → project_name
                                (enables project recovery when folder is unknown)
    memory/
      experiences/              global cross-project learnings

COMMANDS:
  amh hook [prompt]             print context block injected into agent
  amh learn "task" --insight "..."  record explicit cross-session experience
  amh search "query" [--all-projects]  BM25 search over topics & experiences
  amh read path[:a-b]           read a file or line range
  amh note "title" --folder decisions --text "..."
  amh rule <name> --append --text "- always ..."
  amh write docs/api.md [--append] --text "..."
  amh projects                  list all known projects
  amh switch <name|path>        force a specific project context
  amh forget <name>             remove a project from the index (keeps files)
  amh clean                     purge stale hash_map entries for deleted files
  amh init | status | index
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

# ---------------------------------------------------------------- constants
MAX_BYTES = 300_000
EXTS = {".md", ".txt", ".py", ".js", ".ts", ".tsx", ".jsx", ".go", ".rs", ".java", ".c", ".cpp",
        ".h", ".json", ".yaml", ".yml", ".toml", ".sh", ".html", ".css", ".sql"}
SKIP_DIRS = {"node_modules", "venv", ".venv", "__pycache__", "dist", "build", "target", ".git"}

# ---------------------------------------------------------------- global paths
AMH_GLOBAL: Path = Path.home() / ".amh"
AMH_PROJECTS: Path = AMH_GLOBAL / "projects"
AMH_HASH_MAP: Path = AMH_GLOBAL / "hash_map"
AMH_MEMORY: Path = AMH_GLOBAL / "memory"

# ---------------------------------------------------------------- per-project globals (set by set_project)
ROOT: Path = Path.cwd()
PROJECT_NAME: str = "global"
PROJECT_DIR: Path = AMH_PROJECTS / "global"
VECTOR_DB: Path = PROJECT_DIR / "vector.db"

# ---------------------------------------------------------------- default system prompt
DEFAULT_SYSTEM = """\
You have a persistent project memory and automatic cross-session experience engine (AI-MEMORY-HOOK).
amh IS your only memory system. Do NOT use internal agent memory, spawn memory subagents, or use
any external memory tool. Work autonomously; do NOT ask the user for permission to read, search,
or write memory, rules, or docs. Ask only before destructive actions or anything outside this project.

HOW IT WORKS & HOW TO USE IT
- **FIRST**: always run `amh hook "<the user request>"` and read ALL output before touching code.
- <past_experiences> lists lessons, gotchas, and solutions auto-learned across prior sessions.
  Review every entry first — never repeat a past mistake or re-solve an already-solved problem!
- <file_topics> lists the most relevant file topics, sorted best-first, as
  `score path:lines [topic] -> summary`. Summaries are hints. The real file is the truth.
- Search before reading:    amh search "natural language or code query"
- Read only what you need:  amh read path/to/file.py:40-112    (omit :lines for whole file)
- Never open a file unless its summary matches your task.

SAVING PERSISTENT KNOWLEDGE (do this silently during and after work)
- **LAST action on every task**: `amh learn "task title" --insight "..." --outcome success`
- Architectural decision:   amh note "short title" --folder decisions --text "..."
- Bug fix / gotcha:         amh note "short title" --folder bugs --text "..."
- New permanent rule:       amh rule coding --append --text "- always ..."
- Documentation:            amh write docs/architecture.md --text "..."
- Never spawn a subagent just to read memory or search the codebase — use amh commands directly.

GLOBAL STORAGE (all data in ~/.amh — never inside the project folder)
  ~/.amh/
    projects/<project_name>/  → system.md, rules/, memory/, docs/, vector.db
    hash_map/files.db         → maps every file → project (works even if folder is unknown)
    memory/experiences/       → global cross-project learnings
Never put notes or rules outside ~/.amh/. Never store secrets in memory.
"""

# ---------------------------------------------------------------- db schemas
SCHEMA_VECTOR = """
CREATE TABLE IF NOT EXISTS files(path TEXT PRIMARY KEY, fhash TEXT, mtime REAL);
CREATE VIRTUAL TABLE IF NOT EXISTS topics USING fts5(
    path, topic, l0 UNINDEXED, l1 UNINDEXED, summary, content,
    tokenize = 'porter unicode61'
);
"""

SCHEMA_HASH_MAP = """
CREATE TABLE IF NOT EXISTS file_project_map(
    file_path    TEXT PRIMARY KEY,
    file_hash    TEXT,
    project_name TEXT NOT NULL,
    project_root TEXT NOT NULL,
    mtime        REAL
);
CREATE TABLE IF NOT EXISTS projects(
    name      TEXT PRIMARY KEY,
    root      TEXT NOT NULL,
    last_seen REAL
);
CREATE INDEX IF NOT EXISTS idx_fpm_project ON file_project_map(project_name);
"""


# ---------------------------------------------------------------- project detection
def derive_project_name(root: Path) -> str:
    """Derive a clean project name, preferring git remote URL name."""
    try:
        r = subprocess.run(
            ["git", "remote", "get-url", "origin"],
            cwd=str(root), capture_output=True, text=True, timeout=3
        )
        if r.returncode == 0:
            url = r.stdout.strip().rstrip("/")
            name = url.split("/")[-1].removesuffix(".git")
            if name:
                return re.sub(r"[^a-zA-Z0-9_.-]", "-", name)[:60]
    except Exception:
        pass
    return re.sub(r"[^a-zA-Z0-9_.-]", "-", root.name)[:60] or "global"


def _hash_map_db_safe() -> sqlite3.Connection | None:
    """Open hash_map db only if it already exists — avoids creating dirs on first-run."""
    db_path = AMH_HASH_MAP / "files.db"
    if not db_path.exists():
        return None
    try:
        c = sqlite3.connect(db_path, timeout=5)
        c.execute("PRAGMA journal_mode=WAL")
        c.executescript(SCHEMA_HASH_MAP)
        return c
    except Exception:
        return None


def find_root(cwd=None) -> tuple[Path, bool]:
    """Walk up from cwd to find a git root; fall back to hash_map lookup."""
    if os.getenv("AMH_PROJECT"):
        return Path(os.environ["AMH_PROJECT"]).resolve(), True
    start = d = Path(cwd or os.getcwd()).resolve()
    while True:
        if (d / ".git").exists():
            return d, True
        if (d / ".amh").is_dir():   # legacy support
            return d, True
        if d.parent == d:
            break
        d = d.parent
    # No git root found — try hash_map (graceful: skip if db doesn't exist yet)
    hm = _hash_map_db_safe()
    if hm:
        path_str = str(start)
        row = hm.execute(
            "SELECT project_root FROM file_project_map WHERE file_path LIKE ? LIMIT 1",
            (path_str + "%",)
        ).fetchone()
        if row:
            return Path(row[0]), True
        # Return most recently seen project as best guess
        row = hm.execute(
            "SELECT root FROM projects ORDER BY last_seen DESC LIMIT 1"
        ).fetchone()
        if row:
            return Path(row[0]), True
    return start, False


def set_project(root: Path, name: str = ""):
    """Set all global per-project paths."""
    global ROOT, PROJECT_NAME, PROJECT_DIR, VECTOR_DB
    ROOT = root
    PROJECT_NAME = name or derive_project_name(root)
    PROJECT_DIR = AMH_PROJECTS / PROJECT_NAME
    VECTOR_DB = PROJECT_DIR / "vector.db"


# ---------------------------------------------------------------- database helpers
def vector_db() -> sqlite3.Connection:
    """Open (or create) the per-project vector/FTS5 database."""
    PROJECT_DIR.mkdir(parents=True, exist_ok=True)
    c = sqlite3.connect(VECTOR_DB, timeout=30)
    c.execute("PRAGMA journal_mode=WAL")
    c.executescript(SCHEMA_VECTOR)
    return c


def hash_map_db() -> sqlite3.Connection:
    """Open (or create) the global file→project hash map database."""
    AMH_HASH_MAP.mkdir(parents=True, exist_ok=True)
    c = sqlite3.connect(AMH_HASH_MAP / "files.db", timeout=30)
    c.execute("PRAGMA journal_mode=WAL")
    c.executescript(SCHEMA_HASH_MAP)
    return c


# ---------------------------------------------------------------- project init
def init_project():
    """Create ~/.amh/projects/<name>/ directory structure and register in hash_map."""
    for d in ("rules", "docs", "memory/experiences", "memory/decisions",
              "memory/bugs", "memory/conventions"):
        (PROJECT_DIR / d).mkdir(parents=True, exist_ok=True)
    (AMH_MEMORY / "experiences").mkdir(parents=True, exist_ok=True)
    AMH_HASH_MAP.mkdir(parents=True, exist_ok=True)
    if not (PROJECT_DIR / "system.md").exists():
        (PROJECT_DIR / "system.md").write_text(DEFAULT_SYSTEM, encoding="utf-8")
    if not (PROJECT_DIR / "rules" / "general.md").exists():
        (PROJECT_DIR / "rules" / "general.md").write_text(
            "- Keep changes small and focused.\n- Update docs when architecture changes.\n",
            encoding="utf-8")
    with hash_map_db() as hm:
        hm.execute(
            "INSERT OR REPLACE INTO projects(name, root, last_seen) VALUES(?,?,?)",
            (PROJECT_NAME, str(ROOT), time.time())
        )


# ---------------------------------------------------------------- path safety
def safe_amh(rel: str) -> Path:
    """Resolve a path that must stay inside ~/.amh/projects/<name>/.
    Strips legacy .amh/ prefix automatically."""
    rel = re.sub(r"^\.amh/", "", rel).lstrip("/")
    p = (PROJECT_DIR / rel).resolve()
    base = PROJECT_DIR.resolve()
    if p != base and base not in p.parents:
        sys.exit(f"[amh] path must stay inside {base}")
    return p


def safe(rel: str) -> Path:
    """Resolve a file path that must stay inside the project root."""
    p = (ROOT / rel).resolve()
    base = ROOT.resolve()
    if p != base and base not in p.parents:
        sys.exit(f"[amh] path must stay inside {ROOT}")
    return p


# ---------------------------------------------------------------- in-process summarizer
def summarize(path: str, topic: str, text: str) -> str:
    """Zero-server heuristic summary: extracts docstrings, markdown lead, or leading comments."""
    m = re.search(
        r'^(?:[ \t]*(?:def|class|async\s+def)[^\n]+\n)?[ \t]*(?:r|u|f)?("""|\'\'\')'
        r'(.*?)\1', text, re.DOTALL | re.MULTILINE)
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


def split_topics(rel: str, text: str) -> list[tuple[str, int, int, str]]:
    """Split a file into named topic chunks of at most 100 lines each."""
    lines = text.splitlines()
    if not lines:
        return []
    starts: list[tuple[int, str]] = []
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


# ---------------------------------------------------------------- indexer
def iter_files():
    """Yield all indexable source files under ROOT."""
    for dp, dns, fns in os.walk(ROOT):
        dns[:] = [d for d in dns if d not in SKIP_DIRS and not d.startswith(".")]
        for f in fns:
            p = Path(dp) / f
            if p.suffix in EXTS:
                try:
                    if p.stat().st_size <= MAX_BYTES:
                        yield p
                except OSError:
                    pass


def changed(c: sqlite3.Connection) -> tuple[list, list]:
    """Return (todo_files, deleted_rels) based on mtime changes."""
    known = {p: (h, m) for p, h, m in c.execute("SELECT path,fhash,mtime FROM files")}
    seen: set[str] = set()
    todo = []
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


def lock_path() -> Path:
    return PROJECT_DIR / "index.lock"


def lock_fresh() -> bool:
    lp = lock_path()
    return lp.exists() and time.time() - lp.stat().st_mtime < 600


def spawn_indexer():
    """Spawn a detached background indexer process."""
    if lock_fresh():
        return
    kw = dict(
        stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        cwd=str(ROOT),
        env={**os.environ, "AMH_PROJECT": str(ROOT), "AMH_PROJECT_NAME": PROJECT_NAME}
    )
    if os.name == "nt":
        kw["creationflags"] = 0x00000008 | 0x00000200
    else:
        kw["start_new_session"] = True
    try:
        subprocess.Popen([sys.executable, os.path.abspath(__file__), "index"], **kw)
    except OSError:
        pass


def run_index():
    """Synchronous indexer: scan changed files, update vector.db and hash_map."""
    if lock_fresh():
        return
    lp = lock_path()
    PROJECT_DIR.mkdir(parents=True, exist_ok=True)
    lp.write_text(str(os.getpid()))
    try:
        c = vector_db()
        hm = hash_map_db()
        with hm:
            hm.execute(
                "INSERT OR REPLACE INTO projects(name, root, last_seen) VALUES(?,?,?)",
                (PROJECT_NAME, str(ROOT), time.time())
            )
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
                    with hm:
                        hm.execute(
                            "INSERT OR REPLACE INTO file_project_map"
                            "(file_path,file_hash,project_name,project_root,mtime)"
                            " VALUES(?,?,?,?,?)",
                            (str(ROOT / rel), h, PROJECT_NAME, str(ROOT), m)
                        )
                except Exception as e:
                    print(f"[amh] index error {rel}: {e}", file=sys.stderr)
                try:
                    os.utime(lp)
                except OSError:
                    pass
            for rel in deleted:
                with c:
                    c.execute("DELETE FROM files WHERE path=?", (rel,))
                    c.execute("DELETE FROM topics WHERE path=?", (rel,))
                with hm:
                    hm.execute("DELETE FROM file_project_map WHERE file_path=?",
                               (str(ROOT / rel),))
    finally:
        try:
            lp.unlink(missing_ok=True)
        except Exception:
            pass


def index_file(c: sqlite3.Connection, rel: str, data: bytes):
    """Parse and index a single file into the FTS5 topics table."""
    text = data.decode("utf-8", "replace")
    rows = []
    for topic, a, b, body in split_topics(rel, text):
        summ = summarize(rel, topic, body)
        rows.append((rel, topic, a, b, summ, body))
    with c:
        c.execute("DELETE FROM topics WHERE path=?", (rel,))
        if rows:
            c.executemany(
                "INSERT INTO topics(path,topic,l0,l1,summary,content) VALUES(?,?,?,?,?,?)", rows)


# ---------------------------------------------------------------- auto experience learning
def auto_learn_from_previous_session(c: sqlite3.Connection, current_prompt: str):
    """Record an auto-experience entry based on files changed since last session."""
    session_file = PROJECT_DIR / ".last_session.json"
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
            try:
                if p.stat().st_mtime > prev_time:
                    changed_files.append((rel, p))
            except OSError:
                continue

        if changed_files:
            slug = (re.sub(r"[^a-z0-9]+", "-", prev_prompt.lower()).strip("-")[:50]
                    or f"task-{int(prev_time)}")
            # Add timestamp suffix to avoid overwriting different tasks with same slug
            ts_suffix = time.strftime("%Y%m%d")
            exp_rel = f"memory/experiences/auto-{slug}-{ts_suffix}.md"
            exp_file = PROJECT_DIR / exp_rel
            if not exp_file.exists():   # don't overwrite same-day duplicate
                diff_topics = []
                for rel, p in changed_files[:5]:
                    try:
                        text = p.read_text("utf-8", "replace")
                        for top, _, _, _ in split_topics(rel, text)[:3]:
                            if top != "top":
                                diff_topics.append(f"{rel}::{top}")
                    except Exception:
                        pass
                file_list = ", ".join(rel for rel, _ in changed_files[:6])
                topics_str = ", ".join(diff_topics[:5]) if diff_topics else "File updates"
                content = (f"# Experience: {prev_prompt}\n"
                           f"- Date: {time.strftime('%Y-%m-%d')}\n"
                           f"- Project: {PROJECT_NAME}\n"
                           f"- Mode: auto-learned\n"
                           f"- Outcome: success\n"
                           f"- Affected Files: {file_list}\n\n"
                           f"## Key Insight / Learning\n"
                           f"Auto-learned from '{prev_prompt}': Modified {file_list}. "
                           f"Key components: {topics_str}.\n")
                exp_file.parent.mkdir(parents=True, exist_ok=True)
                exp_file.write_text(content, "utf-8")
                try:
                    index_file(c, exp_rel, content.encode("utf-8"))
                except Exception:
                    pass

    save_current_session(session_file, current_prompt)


def save_current_session(session_file: Path, prompt: str):
    try:
        session_file.write_text(json.dumps({
            "prompt": prompt,
            "timestamp": time.time(),
            "project": PROJECT_NAME,
            "root": str(ROOT),
        }), "utf-8")
    except Exception:
        pass


# ---------------------------------------------------------------- search & ranking
def tokenize_query(query: str) -> list[str]:
    """Expand camelCase, snake_case and extract alphanumeric tokens."""
    expanded = re.sub(r"([a-z])([A-Z])", r"\1 \2", query)
    tokens = re.findall(r"[a-zA-Z0-9_]{2,}", expanded)
    cleaned = []
    for t in tokens:
        sub = [s.lower() for s in t.split("_") if len(s) >= 2]
        cleaned.extend(sub or [t.lower()])
    return list(dict.fromkeys(cleaned))


def search_topics(c: sqlite3.Connection, query: str, k: int,
                  path_filter: str = None) -> list[dict]:
    """BM25 search over FTS5 topics table. Lower score = more relevant."""
    tokens = tokenize_query(query)
    if not tokens:
        sql = "SELECT path,topic,l0,l1,summary,0.0 FROM topics"
        params: list = []
        if path_filter:
            sql += " WHERE path LIKE ?"
            params.append(f"%{path_filter}%")
        sql += " LIMIT ?"
        params.append(k)
        rows = c.execute(sql, params).fetchall()
        return [dict(path=r[0], topic=r[1], l0=r[2], l1=r[3], summary=r[4], score=0.0)
                for r in rows]

    match_clause = " OR ".join(f'"{t}"*' for t in tokens)
    try:
        sql = ("SELECT path,topic,l0,l1,summary,"
               "bm25(topics,10.0,10.0,2.0,1.0) AS score "
               "FROM topics WHERE topics MATCH ?")
        params = [match_clause]
        if path_filter:
            sql += " AND path LIKE ?"
            params.append(f"%{path_filter}%")
        sql += " ORDER BY score ASC LIMIT ?"   # ASC = most relevant first (BM25 is negative)
        params.append(k)
        rows = c.execute(sql, params).fetchall()
    except sqlite3.OperationalError:
        rows = []

    return [dict(path=r[0], topic=r[1], l0=r[2], l1=r[3], summary=r[4],
                 score=round(abs(r[5]) if r[5] is not None else 0.0, 2))
            for r in rows]


def search_all_projects(query: str, k: int = 8) -> list[dict]:
    """Search across ALL project vector.db files via hash_map registry."""
    hm = hash_map_db()
    projects = hm.execute(
        "SELECT name, root FROM projects ORDER BY last_seen DESC"
    ).fetchall()
    results = []
    for proj_name, proj_root in projects:
        vdb_path = AMH_PROJECTS / proj_name / "vector.db"
        if not vdb_path.exists():
            continue
        try:
            c = sqlite3.connect(vdb_path, timeout=5)
            c.executescript(SCHEMA_VECTOR)
            for h in search_topics(c, query, k=max(k, 3)):
                h["project"] = proj_name
                h["project_root"] = proj_root
                results.append(h)
            c.close()
        except Exception:
            pass
    # Sort by score descending (abs BM25 — higher = more relevant after abs())
    results.sort(key=lambda x: x["score"], reverse=True)
    return results[:k]


def read_text(p: Path) -> str:
    return p.read_text("utf-8", "replace").strip() if p.is_file() else ""


# ---------------------------------------------------------------- hook builder
def build_hook(prompt: str, budget: int, k: int) -> str:
    """Build the full context block returned by `amh hook`."""
    c = vector_db()
    auto_learn_from_previous_session(c, prompt)

    system = read_text(PROJECT_DIR / "system.md") or DEFAULT_SYSTEM
    rules_dir = PROJECT_DIR / "rules"
    rules = ("\n\n".join(
        f"## {p.stem}\n{read_text(p)}"
        for p in sorted(rules_dir.glob("*.md"))
    ) if rules_dir.exists() else "")

    head = (f"<project>{PROJECT_NAME}</project>\n"
            f"<storage>~/.amh/projects/{PROJECT_NAME}/</storage>\n"
            f"<system>\n{system}\n</system>\n"
            f"<rules>\n{rules or '(none yet)'}\n</rules>\n")

    # Past experiences — project-specific, BM25-ranked against prompt
    exp_lines = []
    for em in search_topics(c, prompt, k=5, path_filter="memory/experiences/"):
        exp_p = PROJECT_DIR / em["path"]
        if exp_p.is_file():
            raw = exp_p.read_text("utf-8", "replace")
            outcome_m = re.search(r"Outcome:\s*(\w+)", raw)
            outcome = outcome_m.group(1).upper() if outcome_m else "LEARNED"
            insight_m = re.search(r"## Key Insight / Learning\n(.*?)(?:\n##|\Z)", raw, re.DOTALL)
            insight = (" ".join(insight_m.group(1).strip().split())
                       if insight_m else em["summary"])
            title_m = re.search(r"# Experience: (.+)", raw)
            title = title_m.group(1).strip() if title_m else em["topic"]
            exp_lines.append(f"• [{outcome}] {title}: {insight[:300]}")

    exp_block = ""
    if exp_lines:
        exp_block = "<past_experiences>\n" + "\n".join(exp_lines) + "\n</past_experiences>\n"

    # File topics ranked by relevance — exclude experience files
    used = (len(head) + len(exp_block)) // 4
    topic_lines = []
    for h in search_topics(c, prompt, k + 10):
        if "memory/experiences/" in h["path"]:
            continue
        line = (f"{h['score']:.2f}  {h['path']}:{h['l0']}-{h['l1']}"
                f" [{h['topic']}] -> {h['summary']}")
        if used + len(line) // 4 + 1 > budget or len(topic_lines) >= k:
            break
        topic_lines.append(line)
        used += len(line) // 4 + 1

    todo, deleted = changed(c)
    pending = len(todo) + len(deleted)
    if pending:
        spawn_indexer()

    return (head + exp_block
            + f'<file_topics project="{PROJECT_NAME}" sorted_by="relevance"'
            f' pending_index="{pending}">\n'
            + "\n".join(topic_lines) + "\n</file_topics>")


# ---------------------------------------------------------------- write helpers
def write_amh_file(rel: str, content: str, append: bool):
    """Write (or append) a file inside ~/.amh/projects/<name>/."""
    p = safe_amh(rel)
    p.parent.mkdir(parents=True, exist_ok=True)
    content = content.rstrip() + "\n"
    if append and p.exists():
        old = p.read_text("utf-8")
        p.write_text(old + ("" if old.endswith("\n") else "\n") + content, "utf-8")
    else:
        p.write_text(content, "utf-8")
    spawn_indexer()
    print(json.dumps({"ok": True, "path": str(p)}))


def record_experience(task_title: str, insight: str,
                      outcome: str = "success", files: str = ""):
    """Explicitly record a cross-session learning experience."""
    slug = (re.sub(r"[^a-z0-9]+", "-", task_title.lower()).strip("-")[:60]
            or "task-experience")
    ts = time.strftime("%Y%m%d")
    # Avoid duplicate slug — append counter if file exists
    base_rel = f"memory/experiences/{slug}-{ts}.md"
    rel = base_rel
    counter = 1
    while (PROJECT_DIR / safe_amh(rel).relative_to(PROJECT_DIR)).exists() if False else \
            (PROJECT_DIR / rel).exists():
        rel = f"memory/experiences/{slug}-{ts}-{counter}.md"
        counter += 1
    content = (f"# Experience: {task_title}\n"
               f"- Date: {time.strftime('%Y-%m-%d')}\n"
               f"- Project: {PROJECT_NAME}\n"
               f"- Mode: explicit\n"
               f"- Outcome: {outcome.lower()}\n"
               f"- Affected Files: {files or '(none specified)'}\n\n"
               f"## Key Insight / Learning\n{insight.strip()}\n")
    write_amh_file(rel, content, append=False)


def body(a) -> str:
    """Get text content from --text flag or stdin."""
    if hasattr(a, "text") and a.text is not None:
        return a.text
    if not sys.stdin.isatty():
        return sys.stdin.read()
    return ""


# ---------------------------------------------------------------- main
def _resolve_project(cwd=None, name_override: str = ""):
    """Shared project resolution for all commands."""
    root, _ = find_root(cwd)
    name = name_override or os.getenv("AMH_PROJECT_NAME") or derive_project_name(root)
    set_project(root, name)


def main() -> int:
    ap = argparse.ArgumentParser(
        prog="amh",
        description="AI-MEMORY-HOOK: persistent project memory for coding agents."
    )
    sub = ap.add_subparsers(dest="cmd", required=True)

    # hook
    h = sub.add_parser("hook", help="Print context block for agent")
    h.add_argument("prompt", nargs="*")
    h.add_argument("--budget", type=int, default=1500,
                   help="Approximate token budget for file_topics")
    h.add_argument("--k", type=int, default=8, help="Max file topic entries")

    # learn
    lr = sub.add_parser("learn", help="Record a cross-session experience")
    lr.add_argument("task", help="Short title of the task")
    lr.add_argument("--insight", required=True, help="What was learned or the key fix")
    lr.add_argument("--outcome", default="success",
                    choices=["success", "failure", "partial"])
    lr.add_argument("--files", default="", help="Comma-separated files involved")

    # search
    s = sub.add_parser("search", help="BM25 search over indexed topics")
    s.add_argument("query", nargs="+")
    s.add_argument("--k", type=int, default=8)
    s.add_argument("--all-projects", dest="all_projects", action="store_true",
                   help="Search across all known projects")

    # read
    r = sub.add_parser("read", help="Read a file or line range")
    r.add_argument("target", help="path/to/file.py or path/to/file.py:10-50")

    # write
    w = sub.add_parser("write", help="Write/append a file inside project memory")
    w.add_argument("path", help="Relative path inside project dir (e.g. docs/api.md)")
    w.add_argument("--append", action="store_true")
    w.add_argument("--text", default=None)

    # note
    n = sub.add_parser("note", help="Save a structured memory note")
    n.add_argument("title")
    n.add_argument("--folder", default="decisions",
                   choices=["decisions", "bugs", "conventions", "tasks", "people"])
    n.add_argument("--text", default=None)

    # rule
    ru = sub.add_parser("rule", help="Append/create a rule file")
    ru.add_argument("name")
    ru.add_argument("--append", action="store_true")
    ru.add_argument("--text", default=None)

    # projects
    sub.add_parser("projects", help="List all known projects")

    # switch
    sw = sub.add_parser("switch", help="Force a specific project context")
    sw.add_argument("target", help="Project name or path")

    # forget
    fg = sub.add_parser("forget", help="Remove a project from the index")
    fg.add_argument("name", help="Project name to remove")

    # clean
    sub.add_parser("clean", help="Purge stale hash_map entries for deleted files")

    # init / status / index
    for name in ("init", "status", "index"):
        sub.add_parser(name)

    a = ap.parse_args()

    # ── hook ────────────────────────────────────────────────────────────────
    if a.cmd == "hook":
        try:
            prompt, cwd = " ".join(a.prompt), None
            if not prompt and not sys.stdin.isatty():
                raw = sys.stdin.read()
                try:
                    j = json.loads(raw)
                    prompt = j.get("prompt") or j.get("user_prompt") or ""
                    cwd = j.get("cwd")
                except Exception:
                    prompt = raw.strip()
            _resolve_project(cwd)
            init_project()
            print(build_hook(prompt, a.budget, a.k))
        except Exception as e:
            print(f"[amh] hook skipped: {e}", file=sys.stderr)
        return 0

    # ── all other commands need project resolution ───────────────────────────
    if a.cmd == "switch":
        # Resolve by name or path
        target = a.target
        p = Path(target).expanduser()
        if p.is_dir():
            set_project(p.resolve())
        else:
            # Treat as project name
            vdb = AMH_PROJECTS / target / "vector.db"
            if not vdb.exists():
                print(f"[amh] Unknown project '{target}'. Run `amh projects` to list known ones.",
                      file=sys.stderr)
                return 1
            # Find root from hash_map
            hm = _hash_map_db_safe()
            root_path = None
            if hm:
                row = hm.execute(
                    "SELECT root FROM projects WHERE name=?", (target,)
                ).fetchone()
                root_path = Path(row[0]) if row else AMH_PROJECTS / target
            set_project(root_path or Path.cwd(), target)
        init_project()
        print(f"[amh] switched to project: {PROJECT_NAME} ({PROJECT_DIR})")
        return 0

    _resolve_project()

    if a.cmd == "init":
        init_project()
        spawn_indexer()
        print(f"[amh] ready: {PROJECT_DIR}")

    elif a.cmd == "index":
        init_project()
        run_index()

    elif a.cmd == "status":
        init_project()
        c = vector_db()
        todo, deleted = changed(c)
        exp_dir = PROJECT_DIR / "memory" / "experiences"
        exp_count = len(list(exp_dir.glob("*.md"))) if exp_dir.exists() else 0
        hm = hash_map_db()
        proj_count = hm.execute("SELECT COUNT(*) FROM projects").fetchone()[0]
        file_count = hm.execute("SELECT COUNT(*) FROM file_project_map").fetchone()[0]
        print(json.dumps({
            "project": PROJECT_NAME,
            "project_dir": str(PROJECT_DIR),
            "project_root": str(ROOT),
            "indexed_files": c.execute("SELECT COUNT(*) FROM files").fetchone()[0],
            "topics": c.execute("SELECT COUNT(*) FROM topics").fetchone()[0],
            "experiences": exp_count,
            "pending": len(todo) + len(deleted),
            "indexer_running": lock_fresh(),
            "global_hash_map": {
                "total_projects": proj_count,
                "total_files_tracked": file_count,
                "db": str(AMH_HASH_MAP / "files.db"),
            },
            "storage": str(PROJECT_DIR),
            "engine": "sqlite-fts5-bm25 (pure-local, zero-server)",
        }, indent=2))

    elif a.cmd == "projects":
        hm = hash_map_db()
        rows = hm.execute(
            "SELECT name, root, last_seen FROM projects ORDER BY last_seen DESC"
        ).fetchall()
        if not rows:
            print("[amh] No projects indexed yet. Run `amh init` or `amh index` in a project.")
        else:
            for pname, proot, ts in rows:
                vdb = AMH_PROJECTS / pname / "vector.db"
                ts_str = (time.strftime("%Y-%m-%d %H:%M", time.localtime(ts))
                          if ts else "unknown")
                db_ok = "✅" if vdb.exists() else "❌"
                print(f"  {pname:30s}  {proot}  (last: {ts_str}, db: {db_ok})")

    elif a.cmd == "forget":
        hm = hash_map_db()
        name = a.name
        with hm:
            hm.execute("DELETE FROM projects WHERE name=?", (name,))
            hm.execute("DELETE FROM file_project_map WHERE project_name=?", (name,))
        print(f"[amh] Removed project '{name}' from index."
              f" Data preserved at {AMH_PROJECTS / name}")

    elif a.cmd == "clean":
        hm = hash_map_db()
        rows = hm.execute(
            "SELECT file_path FROM file_project_map"
        ).fetchall()
        stale = [r[0] for r in rows if not Path(r[0]).exists()]
        if stale:
            with hm:
                hm.executemany("DELETE FROM file_project_map WHERE file_path=?",
                               [(p,) for p in stale])
            print(f"[amh] Cleaned {len(stale)} stale entries from hash_map.")
        else:
            print("[amh] hash_map is clean — no stale entries.")

    elif a.cmd == "learn":
        init_project()
        record_experience(a.task, a.insight, a.outcome, a.files)

    elif a.cmd == "search":
        init_project()
        query = " ".join(a.query)
        if a.all_projects:
            results = search_all_projects(query, a.k)
            if not results:
                print("[amh] No results across any project.")
            for x in results:
                print(f"{x['score']:.2f}  [{x['project']}]  "
                      f"{x['path']}:{x['l0']}-{x['l1']} [{x['topic']}] -> {x['summary']}")
        else:
            results = search_topics(vector_db(), query, a.k)
            if not results:
                print("[amh] No results. Run `amh index` if files were recently added.")
            for x in results:
                print(f"{x['score']:.2f}  {x['path']}:{x['l0']}-{x['l1']}"
                      f" [{x['topic']}] -> {x['summary']}")

    elif a.cmd == "read":
        path_str, start, end = a.target, 0, 0
        m = re.match(r"^(.*):(\d+)-(\d+)$", a.target)
        if m:
            path_str, start, end = m.group(1), int(m.group(2)), int(m.group(3))
        p = safe(path_str)
        if not p.is_file():
            print(f"[amh] not a file: {path_str}", file=sys.stderr)
            return 1
        lines = p.read_text("utf-8", "replace").splitlines()
        print("\n".join(lines[(start or 1) - 1:(end or len(lines))])[:200_000])

    elif a.cmd == "write":
        init_project()
        content = body(a)
        if not content:
            print("[amh] No content — pass --text or pipe via stdin.", file=sys.stderr)
            return 1
        write_amh_file(a.path, content, a.append)

    elif a.cmd == "note":
        init_project()
        content = body(a)
        if not content:
            print("[amh] No content — pass --text or pipe via stdin.", file=sys.stderr)
            return 1
        slug = re.sub(r"[^a-z0-9]+", "-", a.title.lower()).strip("-")[:60] or "note"
        ts = time.strftime("%Y-%m-%d")
        write_amh_file(
            f"memory/{a.folder}/{slug}.md",
            f"# {a.title}\n_{ts}_\n\n{content.strip()}",
            False
        )

    elif a.cmd == "rule":
        init_project()
        content = body(a)
        if not content:
            print("[amh] No content — pass --text or pipe via stdin.", file=sys.stderr)
            return 1
        write_amh_file(f"rules/{a.name}.md", content, a.append)

    return 0


if __name__ == "__main__":
    sys.exit(main())
