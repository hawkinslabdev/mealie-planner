# Contributing

Thank you for considering to helping me out. Please follow the steps below to get your local environment running; then submit a pull request to contribute. If you're more into planning features, please help me along planning features or joining discussions in (open) issues.

## Local development

```bash
python3 -m venv .venv
source .venv/bin/activate

cd mealie-planner
cp .env.example .env   # fill in your values
uvicorn main:app --port 3000 --reload
```

## Build