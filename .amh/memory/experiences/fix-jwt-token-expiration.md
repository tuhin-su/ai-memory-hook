# Experience: Fix JWT token expiration
- Date: 2026-11-25
- Outcome: success
- Affected Files: src/auth.py

## Key Insight / Learning
Server clock drift caused false expiration. Always configure leeway=10 in jwt.decode()
