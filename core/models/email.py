from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator


class EmailRead(BaseModel):
    sender: str
    body: str


class SESReceiptProof(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True, hide_input_in_errors=True)

    version: Literal[1]
    source: Literal["ses_direct"]
    message_id: str = Field(min_length=1, max_length=128, repr=False)
    received_at_ms: int = Field(ge=0)
    sender: str = Field(min_length=3, max_length=320, repr=False)
    recipient: str = Field(min_length=3, max_length=320, repr=False)
    dmarc: Literal["PASS"]
    spam: Literal["PASS"]
    virus: Literal["PASS"]
    headers_truncated: Literal[False]
    content_sha256: str = Field(pattern=r"^[0-9a-f]{64}$", repr=False)
    etag: str = Field(min_length=1, max_length=80, repr=False)
    version_id: str | None = Field(default=None, max_length=1024, repr=False)

    @field_validator("version", mode="before")
    @classmethod
    def exact_version(cls, value):
        if type(value) is not int or value != 1:
            raise ValueError("Invalid receipt version")
        return value

    @field_validator("headers_truncated", mode="before")
    @classmethod
    def exact_untruncated(cls, value):
        if value is not False:
            raise ValueError("Invalid receipt headers state")
        return value


class S3EmailNotification(BaseModel):
    # Retained Python type name, intentionally incompatible with legacy S3-only
    # notifications: they no longer carry authority to parse or share email.
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True, hide_input_in_errors=True)

    bucket: str = Field(min_length=3, max_length=63, repr=False)
    key: str = Field(min_length=1, max_length=160, repr=False)
    receipt: SESReceiptProof = Field(repr=False)
