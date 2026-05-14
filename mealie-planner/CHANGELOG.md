# Changelog

## 0.1.1

- Fix: add-on failed to load after update — pages showed a blank error screen
- Update: all Python dependencies updated to latest stable versions

## 0.1.0

- Fix: background task manager with error tracking and graceful shutdown to prevent silent crashes and OOM from task leaks
- Fix: deduplicated cache refresh (flag + lock) to prevent N-way stampede on cold start
- Fix: single persistent SQLite connection with WAL mode and busy timeout to eliminate concurrent write contention
- Fix: shared httpx.AsyncClient with connection pooling limits to prevent file descriptor exhaustion
- Fix: in-memory credential cache to avoid sync file I/O blocking the event loop on every request
- Fix: background tasks now properly cancelled on shutdown to prevent database corruption