# Mealie Planner

[![License](https://img.shields.io/badge/license-AGPL%203.0-blue)](LICENSE)
[![GitHub Actions Workflow Status](https://img.shields.io/github/actions/workflow/status/hawkinslabdev/mealie-planner/.github%2Fworkflows%2Fbuild-container.yml)](#)
[![Support](https://img.shields.io/badge/Support-Buy%20me%20a%20coffee-fdd734?logo=buy-me-a-coffee)](https://coff.ee/hawkinslabdev)

Mealie Planner is a self-hosted week-view meal planner for [Mealie](https://mealie.io). Browse your recipe library, drag meals onto your week grid, and let the sparkle feature suggest random dishes. 

> [!WARNING]
> Mealie Planner is not built to expose to the internet. Also, you may encounter bugs. Feel free to help improve the project by reporting any issues you find.

<img width="100%" alt="Mealie Planner screenshot" src="https://github.com/hawkinslabdev/mealie-planner/blob/main/.github/images/example.webp" />

We want to make it incredibly simple to plan your meals for the week ahead. Manage your weekly meal plans with Mealie Planner. Share access and use the week-at-a-glance view.

## Getting Started

You can run Mealie Planner locally using Docker Compose:

```yaml
services:
  mealie-planner:
    image: ghcr.io/hawkinslabdev/mealie-planner:latest
    ports:
      - "3000:3000"
    volumes:
      - ./data:/app/data
    environment:
      - MEALIE_API_URL=https://mealie.yourdomain.com
      - MEALIE_API_KEY=your-api-key-here
      - PIN_CODE=ABC123
    restart: unless-stopped
```

After downloading the image and starting the container, the application will be available at `http://localhost:3000`.

> **Note for Home Assistant users:** This add-on is available in the Home Assistant add-on store and reads its configuration from the Supervisor's `/data/options.json` instead of environment variables.

## License

This project is licensed under the **AGPL 3.0** license. See [LICENSE](LICENSE) for details.

## Contributing

Contributions including ideas, bug reports, and pull requests are welcome. Please open an issue to discuss any proposed changes or identified issues.
