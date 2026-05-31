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
        "AGENT_MODELS", "gemini-3.1-flash-lite,gemini-2.5-flash-lite,gemini-3.5-flash,gemini-2.5-flash"
    ).split(",")
    if m.strip()
]

# ── Media (chat attachments) ───────────────────────────────────────────────
# Files land on a writable volume (NOT under app/, which is mounted read-only).
# Layout is family-scoped + date-bucketed under MEDIA_ROOT; the `media` table is
# the source of truth (relative paths only), so the tree stays portable and
# exportable later. See app/storage.py.
MEDIA_ROOT = os.environ.get("MEDIA_ROOT", "/data/media")

# Quality-over-quantity caps (also enforced server-side in /chat/stream).
MAX_IMAGES = int(os.environ.get("MAX_IMAGES", "3"))
MAX_AUDIOS = int(os.environ.get("MAX_AUDIOS", "2"))
MAX_IMAGE_BYTES = int(os.environ.get("MAX_IMAGE_BYTES", str(5 * 1024 * 1024)))   # 5 MB
MAX_AUDIO_BYTES = int(os.environ.get("MAX_AUDIO_BYTES", str(15 * 1024 * 1024)))  # ~2 min
ALLOWED_IMAGE_MIME = {"image/jpeg", "image/png", "image/webp"}
ALLOWED_AUDIO_MIME = {
    "audio/mpeg", "audio/mp3", "audio/wav", "audio/x-wav", "audio/ogg",
    "audio/webm", "audio/aac", "audio/mp4", "audio/m4a", "audio/x-m4a", "audio/flac",
}
