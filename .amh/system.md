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
- Indexing and experience learning run automatically in the background.

SAVING PERSISTENT KNOWLEDGE (do this silently during and after work)
- **LAST action on every task**: `amh learn "task title" --insight "..." --outcome success`
- Architectural decision:   amh note "short title" --folder decisions --text "..."
- Bug fix / gotcha:         amh note "short title" --folder bugs --text "..."
- New permanent rule:       amh rule coding --append --text "- always ..."
- Documentation:            amh write .amh/docs/architecture.md --text "..."
- Never spawn a subagent just to read memory or search the codebase — use amh commands directly.

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
