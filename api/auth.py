"""Only a server-maintained, explicitly enabled demo map can identify an actor."""
import hmac
import re

from agent.contracts import Principal


TOKEN = re.compile(r"[A-Za-z0-9._~+/=-]{1,256}")
ACTOR = re.compile(r"[!-~]{1,128}")


def _valid_demo_entries(settings):
    if settings.demo_mode:
        for token, actor in settings.auth_tokens.items():
            if TOKEN.fullmatch(token) and ACTOR.fullmatch(actor):
                yield token, actor


def demo_identity_ready(settings) -> bool:
    return next(_valid_demo_entries(settings), None) is not None


def demo_principal(settings, credentials: list[str]) -> Principal | None:
    if not settings.demo_mode or len(credentials) != 1:
        return None
    scheme, sep, token = credentials[0].partition(" ")
    if not sep or scheme.lower() != "bearer" or TOKEN.fullmatch(token) is None:
        return None
    actor = None
    for candidate, mapped in _valid_demo_entries(settings):
        if hmac.compare_digest(candidate, token):
            actor = mapped
    # This repository's synthetic demo cohort represents domestic online users.
    return Principal(user_id=actor, conditions={"channel": "online", "region": "CN"}) if actor else None
