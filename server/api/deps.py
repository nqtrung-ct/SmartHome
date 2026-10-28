"""Auth helpers: admin UI token + voice client tokens."""
from __future__ import annotations

from fastapi import Header, HTTPException, Request


def _token_from_header(authorization: str | None) -> str:
    if not authorization:
        return ""
    return authorization.removeprefix("Bearer ").strip()


def require_admin(request: Request, authorization: str | None = Header(None)) -> None:
    cfg = request.app.state.cfg
    if _token_from_header(authorization) != cfg.get("server.admin_token"):
        raise HTTPException(status_code=401, detail="Sai token quản trị")


def voice_identity(app, token: str) -> dict | None:
    """Validate a voice-client token (or the admin token, treated as room-less client)."""
    db = app.state.db
    row = db.one("SELECT * FROM voice_clients WHERE token=?", (token,))
    if row:
        return {
            "kind": "client",
            "client_id": row["id"],
            "name": row["name"],
            "room_id": row["room_id"],
        }
    if token and token == app.state.cfg.get("server.admin_token"):
        return {"kind": "admin", "client_id": 0, "name": "admin", "room_id": None}
    return None
