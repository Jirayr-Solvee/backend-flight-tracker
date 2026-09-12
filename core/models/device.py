from sqlalchemy import CheckConstraint
from sqlmodel import Field, Relationship, SQLModel


class Device(SQLModel, table=True):
    __table_args__ = (CheckConstraint(
        "typeof(localized_push_version) = 'integer' AND localized_push_version BETWEEN 0 AND 2",
        name="ck_device_localized_push_version",
    ),)
    id: str = Field(..., primary_key=True)
    apn_token: str | None = None
    apn_token_active: bool = Field(default=False)
    supports_localized_push: bool = Field(default=False)
    localized_push_version: int = Field(default=1, ge=0, le=2, sa_column_kwargs={"server_default": "1"})

    user_id: str = Field(foreign_key="user.id")
    user: "User" = Relationship(back_populates="devices")


# from .flight_model import Flight
