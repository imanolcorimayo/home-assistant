"""Central config — the one place env vars are read (lib/config.php analogue).

Everything else imports from here instead of touching os.environ, so the full
set of knobs the app needs is visible in one file.
"""

import os

# asyncpg wants a plain `postgresql://` DSN — NOT the `postgresql+asyncpg://`
# form (that prefix is a SQLAlchemy thing). See household/web for the same note.
DATABASE_URL = os.environ["DATABASE_URL"]

# Google OAuth (OpenID Connect). Create one "Web application" OAuth client in
# Google Cloud Console; shared by all devs. Secrets live in .env (gitignored).
GOOGLE_CLIENT_ID = os.environ.get("GOOGLE_CLIENT_ID", "")
GOOGLE_CLIENT_SECRET = os.environ.get("GOOGLE_CLIENT_SECRET", "")

# Signs the session cookie (Starlette SessionMiddleware ≈ PHP $_SESSION). Any
# long random string; rotating it logs everyone out. MUST be set in prod.
SESSION_SECRET = os.environ.get("SESSION_SECRET", "dev-insecure-change-me")

# Gemini (the agent's model). Same key household uses. Models are tried in
# order; on a 429 (daily free-tier quota) or 5xx we rotate to the next.
GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY", "")
AGENT_MODELS = [
    m.strip()
    for m in os.environ.get(
        "AGENT_MODELS", "gemini-2.5-flash,gemini-2.5-flash-lite"
    ).split(",")
    if m.strip()
]
