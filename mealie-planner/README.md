# Mealie Planner

A week-view meal planner for [Mealie](https://mealie.io). Browse your recipe library, assign meals to days, drag to rearrange, and use the sparkle feature to pick a random dish for any slot.

## Requirements

A running Mealie instance accessible from your network, and a [Mealie API](https://docs.mealie.io/documentation/getting-started/api-usage/#getting-a-token) token.

## Configuration

After starting the app, open the web UI. Enter your Mealie instance URL and API token in the settings panel. The app connects to Mealie over your local network and caches your recipe library for fast browsing. You can also configure the following variables from the app settings instead:

| Option | Description |
|---|---|
| `mealie_url` | Base URL of your Mealie instance, e.g. `http://192.168.1.x:9000` |
| `api_token` | Your Mealie API token (generated in Mealie under user settings) |

After configuring your instance, your entire household can use the app using your preferred language.

## License

This project is licensed under the **AGPL 3.0** license. This project is not affiliated with [Mealie](https://mealie.io).
Also, if you're running into issues, please let us know on our [GitHub repository](https://github.com/hawkinslabdev/mealie-planner).
