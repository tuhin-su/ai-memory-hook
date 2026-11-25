# Experience: PostgreSQL connection pool leak
- Date: 2026-11-25
- Outcome: failure
- Affected Files: src/database.py

## Key Insight / Learning
Unclosed cursors leaked pool connections. Always use with db.get_cursor() context manager.
