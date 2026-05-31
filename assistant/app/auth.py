"""Google sign-in + session + first-login family bootstrap.

Flow: /auth/google → Google → /auth/callback. On callback we exchange the code
for the user's Google identity (email + stable `sub`), find-or-create their
`member` row, and stash member_id in the signed session cookie.

First-login bootstrap (#26 scope): a brand-new Google identity gets its OWN new
family. Joining an EXISTING family (invites) is #24 — until then, to put two
people in one family, attach the second member's family_id by hand in adminer.

The agent and all queries take family_id from the logged-in member here — never
from user/agent input. That's the tenant-isolation boundary.
"""

import uuid

from authlib.integrations.starlette_client import OAuth
from fastapi import APIRouter, Request
from fastapi.responses import RedirectResponse

from app import config, db

oauth = OAuth()
oauth.register(
    name="google",
    client_id=config.GOOGLE_CLIENT_ID,
    client_secret=config.GOOGLE_CLIENT_SECRET,
    # OIDC discovery — Authlib pulls Google's endpoints from this document.
    server_metadata_url="https://accounts.google.com/.well-known/openid-configuration",
    client_kwargs={"scope": "openid email profile"},
)

router = APIRouter()


async def current_member(request: Request):
    """Return the logged-in member row, or None. Loaded fresh from the DB each
    request so a deactivated member can't keep a stale session alive."""
    mid = request.session.get("member_id")
    if not mid:
        return None
    return await db.fetchrow(
        "SELECT * FROM member WHERE member_id = $1::uuid AND is_active",
        mid,
    )


@router.get("/auth/google")
async def auth_google(request: Request):
    redirect_uri = request.url_for("auth_callback")
    return await oauth.google.authorize_redirect(request, redirect_uri)


@router.get("/auth/callback", name="auth_callback")
async def auth_callback(request: Request):
    token = await oauth.google.authorize_access_token(request)
    info = token["userinfo"]
    sub, email = info["sub"], info["email"]
    name = info.get("name") or email

    member = await db.fetchrow("SELECT * FROM member WHERE google_sub = $1", sub)
    if member is None:
        # New identity → bootstrap a fresh family and make them its first member.
        family_id = await db.fetchval(
            "INSERT INTO family (name) VALUES ($1) RETURNING family_id",
            f"Familia de {name}",
        )
        member = await db.fetchrow(
            """
            INSERT INTO member (family_id, full_name, email, google_sub)
            VALUES ($1, $2, $3, $4)
            RETURNING *
            """,
            family_id, name, email, sub,
        )

    request.session["member_id"] = str(member["member_id"])
    return RedirectResponse("/", status_code=303)


@router.get("/logout")
async def logout(request: Request):
    request.session.clear()
    return RedirectResponse("/", status_code=303)
