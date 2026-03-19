from sqlmodel import Field, Relationship, SQLModel

from .user import UserSubscriptionLink


class Subscription(SQLModel, table=True):
    id: str = Field(primary_key=True, description="Original Transaction ID from JWS")

    transactions: list["Transaction"] = Relationship(back_populates="subscription")

    users: list["User"] = Relationship(
        back_populates="subscriptions", link_model=UserSubscriptionLink
    )
