from sqlmodel import Field, Relationship, SQLModel


class Device(SQLModel, table=True):
    id: str = Field(..., primary_key=True)
    apn_token: str | None = None
    apn_token_active: bool = Field(default=False)

    user_id: str = Field(foreign_key="user.id")
    user: "User" = Relationship(back_populates="devices")


# from .flight_model import Flight
