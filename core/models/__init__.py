from sqlmodel import Session, SQLModel, create_engine

from .apple_ads import AppleAdsAttribution, AppleAdsSpendDaily, AppStoreRevenueEvent
from .device import Device
from .experiment import (
    ExperimentConversion,
    ExperimentExposure,
    ExperimentGoalSelection,
)
from .flight import Airline, Airport, Arrival, Departure, Flight
from .live_activity import (
    LiveActivityPushToStartDelivery,
    LiveActivityPushToStartRegistration,
    LiveActivityRegistration,
)
from .search_failure import SearchFailureSample
from .subscription import Subscription
from .subscription_lifecycle import AppStoreSubscriptionLifecycleEvent
from .transaction import Transaction

DATABASE_URL = "sqlite:///./database.db"
engine = create_engine(DATABASE_URL)

SQLModel.metadata.create_all(engine)


def get_session():
    with Session(engine) as session:
        yield session
