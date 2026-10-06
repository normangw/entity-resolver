---
name: Bug report
about: Errors in behavior or functionality
title: ''
labels: bug
assignees: ''

---

**Describe the bug**
A clear and concise description of what the bug is.

**Affected endpoint(s)**
e.g. `/health`, `/resolve`

**To Reproduce**
Steps to reproduce the behavior, ideally a copy-pasteable request:
```
curl -X POST http://localhost:8000/... -H "Content-Type: application/json" -d '{"name": "..."}'
```

**Input**
The company name (and language, if relevant) you sent.

**Expected behavior**
What you expected back (canonical name, parent company, headquarters address).

**Actual behavior**
What the service returned instead, including any error message or traceback.

**Environment**
- Run via: local `uv run uvicorn` / Docker / Compose on Jetstream2
- Branch or commit:

**Additional context**
Add any other context about the problem here.
