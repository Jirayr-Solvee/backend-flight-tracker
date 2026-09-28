import logging

from sqlmodel import Session, SQLModel, create_engine

from .activation_recovery import PurchaseActivationRecovery
from .ai_consent import AIConsentReceipt, UserAIConsent, UserAIEmailIdentity, UserAIEmailReceipt
from .activation_journey import (
    ActivationJourneyIdentity, ActivationJourneyAssignment, ActivationJourneyEnrollment,
    ActivationJourneyDiagnosticContext, ActivationJourneySelection,
    ActivationJourneyAttribution, ActivationJourneyGoalSelection, ActivationJourneyGoalReceipt,
)
from .apple_ads import AppleAdsAttribution, AppleAdsSpendDaily, AppStoreRevenueEvent
from .device import Device
from .story_push import StoryPushDevice, StoryPushDelivery
from .experiment import (
    ExperimentConversion,
    ExperimentExposure,
    ExperimentGoalSelection,
    ExperimentGoalConfirmation,
    ExperimentGoalConfirmationReceipt,
    ExperimentEnrollment,
    ExperimentDiagnosticEvent,
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
engine = create_engine(DATABASE_URL, hide_parameters=True)

SQLModel.metadata.create_all(engine)


def get_session():
    session = Session(engine)
    try:
        yield session
    finally:
        try:
            session.close()
        except Exception:
            # Database driver messages can echo credential-bearing parameters.
            # Teardown must not reintroduce a traceback after a safe route error.
            logging.getLogger(__name__).error(
                "database_session_cleanup_failed", exc_info=False, stack_info=False
            )
            try:
                session.invalidate()
            except Exception:
                logging.getLogger(__name__).error(
                    "database_session_invalidation_failed",
                    exc_info=False,
                    stack_info=False,
                )
