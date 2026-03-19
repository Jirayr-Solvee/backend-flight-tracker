from pydantic import BaseModel


class EmailRead(BaseModel):
    sender: str
    body: str


class S3EmailNotification(BaseModel):
    bucket: str
    key: str
