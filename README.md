# AI-MEMORY-HOOK (amh)

A 100% purely local, zero-external-service memory hook, topic index, and **cross-session experience engine** for AI coding agents.

- **Zero Server, Zero Daemon, Zero Ollama**: Runs entirely inside Python using standard library.
- **Cross-Session Experience Learning**: The agent learns from every session via `amh learn`. Past successes, failures, and gotchas are automatically recalled in future sessions before the agent touches code.
- **SQLite FTS5 + BM25**: Blazing fast topic search with camelCase/snake_case query tokenization and prefix matching.
- **Non-blocking Hook**: `amh hook` outputs system instructions, rules, past experiences, and topic hints instantly. If files changed, it indexes in a detached background process.

---

## Cross-Session Experience Learning

When an agent solves a tricky bug, completes a task, or encounters a gotcha, it records its experience:

```bash
amh learn "Fix JWT token expiration" \
  --insight "Server clock drift caused false expiration. Always configure leeway=10 in jwt.decode()." \
  --outcome success \
  --files "src/auth.py"
```

In any future session, whenever a related user request arrives (e.g. *"user reporting token validation issue"*), `amh hook` automatically injects the past experience into the prompt context:

```xml
<past_experiences>
• [SUCCESS] Experience: Fix JWT token expiration: Server clock drift caused false expiration. Always configure leeway=10 in jwt.decode().
</past_experiences>
```
This ensures the model **never repeats past mistakes** and **builds compound intelligence** over time.

---

## Installation via `install.sh`

```bash
chmod +x install.sh
./install.sh --auto       # Auto-detects runtime & wires Claude, Antigravity, Gemini
```

Runtime Modes:
* `--auto`: Auto-detects GPU (NVIDIA CUDA/AMD ROCm/Apple Silicon) vs CPU runtime.
* `--gpu-only`: Forces GPU runtime without checks.
* `--cpu-only`: Pure CPU mode.
* `--update`: Re-run `./install.sh` at any time to update in-place.

---

## Quick Start in a Project

```bash
cd /path/to/your/project
amh init
```

**All data is stored globally in `~/.amh/` — never inside the project folder.**

```
~/.amh/
  projects/
    <project_name>/
      system.md                 # System instructions injected to agents (editable)
      rules/general.md          # Hard rules injected every turn
      memory/experiences/       # Cross-session learnings (successes, failures, gotchas)
      memory/decisions/         # Architectural decisions saved by agent
      memory/bugs/              # Resolved bugs & gotchas
      memory/conventions/       # Team conventions
      docs/                     # High-level architecture docs
      vector.db                 # SQLite FTS5 search index (rebuildable anytime)
      .last_session.json        # Previous session tracker for auto-learning
  hash_map/
    files.db                    # Maps every indexed file path + hash → project_name
                                # Enables context recovery even when project folder is UNKNOWN
  memory/
    experiences/                # Global cross-project learnings
```

> **Project name** is derived from the git remote URL name, or falls back to the folder name.
> When working from an **unknown directory**, amh uses `hash_map/files.db` to find which
> project the files belong to automatically.

---

## CLI Reference

| Command | Description |
| :--- | :--- |
| `amh hook [prompt]` | Injects `<system>`, `<rules>`, `<past_experiences>`, and `<file_topics>` |
| `amh learn "title" --insight "..."` | Records a task outcome and lesson for future sessions |
| `amh search "query"` | BM25 search over code topics and past experiences (current project) |
| `amh search "query" --all-projects` | Search across **all** known projects via `hash_map/files.db` |
| `amh read path:l0-l1` | Reads a line range without loading whole file |
| `amh note "title" --folder decisions --text "..."` | Saves a note to `~/.amh/projects/<name>/memory/<folder>/` |
| `amh rule coding --append --text "- always ..."` | Appends a rule to `~/.amh/projects/<name>/rules/<name>.md` |
| `amh write .amh/docs/api.md --text "..."` | Writes/appends documentation |
| `amh projects` | Lists all known projects tracked in `~/.amh/hash_map/files.db` |
| `amh status` | Displays indexed files, topics, experiences, hash_map stats |
| `amh index` | Synchronously scans and indexes modified files |
| `amh init` | Initializes global `~/.amh/` store for this project |

---

## Agent Integration

### Claude Code (`~/.claude/settings.json`)
```json
{
  "hooks": {
    "UserPromptSubmit": [
      {
        "hooks": [
          {
            "type": "command",
            "command": "amh hook"
          }
        ]
      }
    ]
  }
}
```

### Codex / Antigravity / Gemini CLI
Add to `~/.agents/rules/amh_memory.md` or your project rules:
```markdown
At the start of every task run: `amh hook "<the user request>"`
Review `<past_experiences>` so you never repeat past mistakes.
Use `amh read path:l0-l1` to inspect code slices.
At the end of a task or bug fix: `amh learn "task title" --insight "..." --outcome success`
```
