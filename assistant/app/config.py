"""Central config — the one place env vars are read (lib/config.php analogue).

Everything else imports from here instead of touching os.environ, so the full
set of knobs the app needs is visible in one file. Auth/OAuth settings land
here in #26; for the #25 scaffold we only need the database.
"""

import os

# asyncpg wants a plain `postgresql://` DSN — NOT the `postgresql+asyncpg://`
# form (that prefix is a SQLAlchemy thing). See household/web for the same note.
DATABASE_URL = os.environ["DATABASE_URL"]
