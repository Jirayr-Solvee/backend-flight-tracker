from enum import Enum

from sqlmodel import Field, Relationship, SQLModel


class TransactionReason(str, Enum):
    PURCHASE = "PURCHASE"
    RENEWAL = "RENEWAL"


class InAppOwnershipType(str, Enum):
    PURCHASED = "PURCHASED"
    FAMILY_SHARED = "FAMILY_SHARED"


class Environment(str, Enum):
    XCODE = "Xcode"
    SANDBOX = "Sandbox"
    PRODUCTION = "Production"


class TransactionType(str, Enum):
    AUTO_RENEWABLE = "Auto-Renewable Subscription"
    NON_RENEWABLE = "Non-Renewable Subscription"
    CONSUMABLE = "Consumable"
    NON_CONSUMABLE = "Non-Consumable"


class Transaction(SQLModel, table=True):
    id: str = Field(
        primary_key=True, description="Actual transaction id not original id"
    )
    product_id: str | None = None

    purchase_date: int | None = None
    original_purchase_date: int | None = None
    signed_date: int | None = None
    expires_date: int | None = None
    revoked_date: int | None = None

    type: str | None = None
    environment: str | None = None
    transaction_reason: str | None = None

    price: int | None = None
    currency: str | None = None

    app_account_token: str | None = None

    is_upgraded: bool | None = None

    subscription_id: str = Field(foreign_key="subscription.id")

    subscription: "Subscription" = Relationship(back_populates="transactions")
