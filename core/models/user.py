from sqlmodel import Field, Relationship, SQLModel


class UserFlightLink(SQLModel, table=True):
    user_id: str = Field(foreign_key="user.id", primary_key=True)
    flight_id: int = Field(foreign_key="flight.id", primary_key=True)


class UserSubscriptionLink(SQLModel, table=True):
    user_id: str = Field(foreign_key="user.id", primary_key=True)
    subscription_id: str = Field(foreign_key="subscription.id", primary_key=True)


class User(SQLModel, table=True):
    id: str = Field(primary_key=True, unique=True)
    apple_id: str | None = None
    email: str | None = None

    verified: bool = Field(default=False)
    full_name: str | None = None

    has_searched: bool = Field(default=False)

    notification_count: int = Field(default=0)

    premium_valid_until: int | None = None

    flights: list["Flight"] = Relationship(
        back_populates="users", link_model=UserFlightLink
    )

    subscriptions: list["Subscription"] = Relationship(
        back_populates="users", link_model=UserSubscriptionLink
    )

    devices: list["Device"] = Relationship(back_populates="user")
