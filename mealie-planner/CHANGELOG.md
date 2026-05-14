# Changelog

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
