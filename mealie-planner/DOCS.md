# Mealie Quick Planner

A week-view meal planner for [Mealie](https://mealie.io). Browse your recipe library, drag meals onto the week grid, and let the Sparkle feature suggest random dishes based on your history.

> **Note:** This add-on is not intended to be exposed directly to the internet.

## Prerequisites

- A running [Mealie](https://mealie.io) instance reachable from your Home Assistant host
- A Mealie API token (see below)

## Getting your API token

1. Open your Mealie instance
2. Go to **Profile → API Tokens**
3. Click **Generate**, give it a name, and copy the token

## Configuration

Open the add-on settings panel (gear icon in the top bar) and fill in:

| Field | Description |
|---|---|
| **Instance URL** | Full URL of your Mealie instance, e.g. `https://mealie.yourdomain.com` |
| **API token** | The token you generated above |

Click **Save & validate** — the add-on will verify the connection and cache your recipe library before opening the planner.

## Using the planner

- **Select** — click an empty cell to pick a recipe from your library
- **Sparkle** (✦) — assigns a random recipe, weighted towards what you had the same weekday last week
- **Drag & drop** — move a recipe between days or meal slots
- **Remove** — click the × on a recipe chip to clear the slot (with undo)
- **Arrow keys** — scroll the week view left and right
- **Open in Mealie** (↗) — jump directly to the recipe in your Mealie instance

## Recipe cache

Recipes are cached locally in SQLite and refreshed automatically every hour. To force an immediate refresh go to **Settings → Recipe cache → Refresh**.

## Meal slots

By default only **Dinner** is shown. Enable Breakfast, Lunch, and Side from **Settings → Visible meal slots**.

## Troubleshooting

| Symptom | Likely cause |
|---|---|
| "Unreachable" status dot | Mealie URL is wrong or Mealie is down |
| No recipes shown | Cache empty — go to Settings and click Refresh |
| 401 error on save | API token is invalid or expired |
| Images not loading | Mealie URL mismatch between add-on config and actual host |
