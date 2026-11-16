import hashlib
import hmac
import json
import re

from pydantic import BaseModel, ConfigDict, Field, field_validator


ID_PATTERN = r"[A-Za-z0-9_-]{1,64}"


class TicketRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    order_id: str = Field(pattern=f"^{ID_PATTERN}$")
    user_id: str = Field(min_length=1, max_length=128)
    reason: str = Field(min_length=1, max_length=500)

    @field_validator("reason", mode="before")
    @classmethod
    def clean_reason(cls, value):
        return value.strip() if isinstance(value, str) else value


class CursorCodec:
    """Client treats token as opaque; only a valid actor/dataset MAC may unlock an offset."""

    def __init__(self, secret: bytes, dataset_id: str):
        self.key = hmac.digest(secret, b"after-sales-mock-v1/page", "sha256")
        self.dataset_id = dataset_id

    def encode(self, actor: str, offset: int) -> str:
        message = json.dumps([self.dataset_id, actor, offset], separators=(",", ":")).encode()
        return f"{offset:x}.{hmac.new(self.key, message, hashlib.sha256).hexdigest()}"

    def decode(self, token: str, actor: str) -> int:
        if not re.fullmatch(r"[1-9a-f][0-9a-f]{0,7}\.[0-9a-f]{64}", token):
            raise ValueError("invalid_cursor")
        offset = int(token.split(".", 1)[0], 16)
        if not hmac.compare_digest(token, self.encode(actor, offset)):
            raise ValueError("invalid_cursor")
        return offset
