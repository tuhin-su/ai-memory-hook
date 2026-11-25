# Architecture & Design of amh (AI-MEMORY-HOOK)

`amh` is an in-process, zero-external-service memory hook and topic index designed for AI coding agents.

---

## 1. System Architecture

```mermaid
flowchart TD
    subgraph AGENT["AI Agent Environment"]
        LLM["Agent Model (Claude, Antigravity, Gemini)"]
    end

    subgraph CLI["amh.py (Zero External Services, Stdlib Only)"]
        HOOK["amh hook"]
        SEARCH["amh search / read"]
        MUTATE["amh note / rule / write"]
        DETACHED["Detached Indexer (amh index)"]
    end

    subgraph ENGINE["Local In-Process Storage & Search Engine"]
        FTS5["SQLite index.db (WAL Mode + FTS5 BM25)"]
        HEURISTIC["Heuristic Docstring / Code Extractor"]
    end

    subgraph FS["Project Directory (.amh/)"]
        SYSTEM_MD[".amh/system.md"]
        RULES_MD[".amh/rules/*.md"]
        NOTES_MD[".amh/memory/**/*.md"]
        DOCS_MD[".amh/docs/*.md"]
        CODE_FILES["Project Source Code (*.py, *.ts, *.go, ...)"]
    end

    LLM -->|"1. UserPromptSubmit"| HOOK
    HOOK -->|"Reads rules & system"| RULES_MD
    HOOK -->|"Reads rules & system"| SYSTEM_MD
    HOOK -->|"BM25 Query (<2ms)"| FTS5
    HOOK -->|"Spawns if files changed (non-blocking)"| DETACHED

    DETACHED -->|"Scans files (mtime & SHA-1)"| CODE_FILES
    DETACHED -->|"Extracts summaries"| HEURISTIC
    DETACHED -->|"Updates index & topics"| FTS5

    LLM -->|"2. Read exact line slice"| SEARCH
    SEARCH -->|"Reads targeted lines"| CODE_FILES

    LLM -->|"3. Save durable decisions/rules"| MUTATE
    MUTATE -->|"Writes markdown note/rule"| FS
```

---

## 2. Non-Blocking Hook Lifecycle

The agent prompt is never delayed. Context is returned in `<5ms` from the existing index, while changed files are indexed in a detached background worker.

```mermaid
sequenceDiagram
    autonumber
    actor User as User / Agent Loop
    participant Hook as amh hook (CLI)
    participant DB as SQLite index.db (FTS5)
    participant Worker as Detached Indexer (Background Process)
    participant Model as LLM Context Window

    User->>Hook: Invokes "amh hook [user prompt]"
    Hook->>Hook: Read .amh/system.md & .amh/rules/*.md
    Hook->>DB: Query top topics via BM25 (token budget ~1500)
    DB-->>Hook: Return relevant file topics & line hints
    
    Hook->>Hook: Check for modified files (fast stat/mtime scan)
    
    alt Files have changed & no active lock
        Hook-)Worker: Spawn background "amh index" (detached, non-blocking)
    end
    
    Hook-->>Model: Output <system> + <rules> + <file_topics> (Exit 0)
    Note over Hook,Model: Hook finishes in < 5ms without waiting for indexer
    
    opt Asynchronous Background Indexing
        Worker->>Worker: Parse code into 100-line topic blocks
        Worker->>Worker: Extract heuristic summaries & docstrings
        Worker->>DB: Update FTS5 virtual table
        Worker->>Worker: Delete lock file on completion
    end
```

---

## 3. Agent Autonomous Read/Write Feedback Loop

How the agent navigates code and persists durable knowledge:

```mermaid
flowchart LR
    A["Task Started"] --> B["amh hook"]
    B --> C["Review <file_topics> Hints"]
    
    C --> D{"Needs full file?"}
    D -- "No, summary is enough" --> E["Work on Solution"]
    D -- "Yes, read slice" --> F["amh read file.py:40-80"] --> E

    E --> G{"Learned durable fact / decision?"}
    G -- "Yes" --> H["amh note / amh rule"]
    G -- "No" --> I["Task Completed"]
    H --> I
```

---

## 4. Key Components

- **Storage**: `.amh/index.db` (SQLite with Write-Ahead Logging `WAL` mode).
- **Search Engine**: SQLite `fts5` virtual table with BM25 ranking (`bm25(topics, 10.0, 10.0, 2.0, 1.0)`).
- **Query Parser**: Splits camelCase and snake_case tokens with prefix wildcard matching (`"term"*`).
- **Summarizer**: In-process regex extractor for docstrings, markdown headers, and opening comments.
- **Detached Indexer**: Spawns detached OS process with PID lock file and heartbeat monitoring.
