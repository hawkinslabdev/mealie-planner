# Changelog

## 0.1.0

- Fix: background task manager with error tracking and graceful shutdown to prevent silent crashes and OOM from task leaks
- Fix: deduplicated cache refresh (flag + lock) to prevent N-way stampede on cold start
- Fix: single persistent SQLite connection with WAL mode and busy timeout to eliminate concurrent write contention
- Fix: shared httpx.AsyncClient with connection pooling limits to prevent file descriptor exhaustion
- Fix: in-memory credential cache to avoid sync file I/O blocking the event loop on every request
- Fix: background tasks now properly cancelled on shutdown to prevent database corruption

## 0.0.5 

- Enhance various mobile UI components
- Fix the recipe actions, which didn't properly trigger actions

## 0.0.4

- Fix `recipe-actions` API-response which prevented data from being loaded

## 0.0.3

- Fix DATA_PATH to use Supervisor-backed /data volume (credentials persisted across HA updates)
- Add recipe action menu and API integration
- Add debug endpoint for raw recipe actions

## 0.0.2

- Implement mobile infinite scroll for meal planner
- Enhance mobile UI components
- Improve status dot margin and HAOS navbar display

## 0.0.1

- FastAPI backend with encrypted credential storage
- Alpine.js frontend with week-view meal planner
- Multi-day configurable range (configurable past/future days)
- Recipe search and browse from Mealie library
- Drag-and-drop meal scheduling
- Sparkle (weighted random) recipe suggestion with last-week rotation bias
- Recipe image proxy with caching
- SQLite recipe cache with hourly refresh
- PIN authentication for Docker deployments
- Dark/light/system theme support
- Home Assistant ingress support
