# Contributing

Thank you for considering helping me out. Please follow the steps below to get your local environment running; then submit a pull request to contribute. If you're more into planning features, please help me plan features or join discussions in the (open) issues.

## Local development

```bash
# Create and activate the virtual environment
cd mealieplanner/
python3 -m venv .venv
source .venv/bin/activate

# Install dependencies
pip install -r requirements.txt

# Set up the environment variables and run the app
cd mealie-planner
cp .env.example .env   # fill in your values
uvicorn main:app --port 3000 --reload
```

## Tests

```bash
# Run locale tests from the mealie-planner directory
python3 -m pytest tests/
```

## Packages

```bash
# Install pip-tools in the active virtual environment
python3 -m pip install pip-tools

# Upgrade all dependencies and write to requirements.txt
pip-compile --upgrade requirements.in

# Sync the virtual environment with the updated lockfile
pip-sync requirements.txt
```