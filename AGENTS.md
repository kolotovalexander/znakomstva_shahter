# Repository Guidelines

## Project Structure & Module Organization
- `bot.py` hosts the Telegram conversation handlers, keyboards, and start-up logic. Run it with `python bot.py` during development and deployment.
- `database.py` wraps SQLite persistence (users and likes) with thread-safe helpers. The database file `bot.db` is created in the project root when the bot runs.
- `requirements.txt` lists runtime dependencies; `README.md` summarises setup steps. A local `.env` stores secrets and stays untracked.
- Runtime artefacts such as `.venv/` and `bot.db` should remain outside version control to keep the repository clean.

## Build, Test, and Development Commands
- `python -m venv .venv` creates an isolated environment. Activate it with `source .venv/bin/activate` (or `.venv\\Scripts\\activate` on Windows).
- `pip install -r requirements.txt` installs `python-telegram-bot==20.7` and `python-dotenv==1.0.0`.
- `python bot.py` starts the bot, reading `BOT_TOKEN` from `.env`. For ad-hoc runs you can prefix with `BOT_TOKEN=...`.
- `sqlite3 bot.db '.tables'` helps inspect the schema while debugging.

## Coding Style & Naming Conventions
- Follow PEP 8: four-space indentation, snake_case functions, and lowercase module names.
- Keep async handler names verb-driven (`ask_age`, `finish_profile`) and prefer descriptive constants for conversation states.
- Type hints and concise docstrings are encouraged for new helpers; reuse the module-level logger for diagnostic output.

## Testing Guidelines
- Automated tests are absent; exercise core flows manually with a staging Telegram bot before merging.
- Validate `/start` onboarding, profile browsing, mutual likes, and reset behaviours end-to-end.
- Future tests should live under `tests/`, named `test_<feature>.py`, using `pytest` or the standard library `unittest`.

## Commit & Pull Request Guidelines
- Mirror the current history: short, imperative commit titles such as `Improve start command guidance`.
- Pull requests should outline the change, list manual test evidence, and reference related issues. Add screenshots or transcripts for UX-impacting tweaks.

## Security & Configuration Tips
- Keep `.env` and real tokens private; rotate credentials immediately if they leak.
- Remove or scrub `bot.db` before sharing logs or reproductions to avoid exposing user data.
- Enable Telegram privacy mode via @BotFather to limit unsolicited production traffic during tests.
