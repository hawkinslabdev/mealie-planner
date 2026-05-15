# Mealie Planner

A week-view meal planner for [Mealie](https://mealie.io). Browse your recipe library, assign meals to days, drag to rearrange, and use the sparkle feature to pick a random dish for any slot.

## Requirements

A running Mealie instance accessible from your network, and a Mealie API token.

## Configuration

After starting the add-on, open the web UI. Enter your Mealie instance URL and API token in the settings panel. The add-on connects to Mealie over your local network and caches your recipe library for fast browsing. You can also configure the following variables from the Add-on settings instead:

| Option | Description |
|---|---|
| `mealie_url` | Base URL of your Mealie instance, e.g. `http://192.168.1.x:9000` |
| `api_token` | Your Mealie API token (generated in Mealie under user settings) |

## Notes

- Recipe data is cached locally. Use the refresh button in settings if your Mealie library has changed.
- Multiple household members can use the add-on simultaneously — credentials are stored server-side.
