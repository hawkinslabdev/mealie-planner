# Changelog

## 0.1.5

- Fix: removing a recipe no longer causes it to reappear after refreshing the page
- Fix: scrolling on mobile no longer accidentally moves the page behind a dialog
- Fix: the "Connect to Mealie" screen no longer flashes briefly on page load
- Change: recipe action buttons (random, change, open) are now larger and easier to tap

## 0.1.3

- Fix: add-on failed to load after update; pages showed a blank error screen
- Update: all Python dependencies updated to latest stable versions

## 0.1.0

- Fix: background task manager with error tracking and graceful shutdown to prevent silent crashes
- Fix: deduplicated cache refresh on cold start
- Fix: single persistent SQLite connection with WAL mode and busy timeout to eliminate concurrent write contention
- Fix: shared connection pooling limits to prevent file exhaustion
- Fix: in-memory credential cache to avoid sync file I/O blocking the event loop on every request
- Fix: background tasks now properly cancelled on shutdown to prevent database corruption