# Repository Guidance

This repository owns the local durable cross-agent job supervisor and thin
bindings for coding clients. Keep it independent of any one agent application.

Before every commit, update `docs/SESSION_LOG.md` and any durable documentation
affected by behavior, configuration, architecture, operations, or verification.
Never commit credentials, provider transcripts, runtime state, sockets, or logs.

Run the full test suite before publishing changes:

```bash
.venv/bin/python -m unittest discover -s tools/tests -v
```
