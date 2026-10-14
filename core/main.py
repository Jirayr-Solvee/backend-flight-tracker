import logging

from fastapi import FastAPI
from .routers import activation_journey, experiment_diagnostics, cockpit

# from . import lifespan
from .routers import (ai_consent, apple_ads, flags, flight_share, flights, incoming_email,
                      legal, subscriptions, users, webhook)
from .services.flight_share_page import ShareTokenLogFilter

# Share tokens travel in the public page path: keep them out of access logs.
for _access_logger in ("uvicorn.access", "gunicorn.access"):
    logging.getLogger(_access_logger).addFilter(ShareTokenLogFilter())

# app = FastAPI(lifespan=lifespan)
app = FastAPI()
app.include_router(experiment_diagnostics.router, prefix="/subscriptions", tags=["Experiment diagnostics"])
app.include_router(activation_journey.router, prefix="/subscriptions", tags=["Activation journey"])

app.include_router(flights.router, prefix="/flights", tags=["Flights"])
app.include_router(flight_share.router, prefix="/flights", tags=["Flight sharing"])
app.include_router(flight_share.public_router, prefix="/public/share", tags=["Flight sharing"])
app.include_router(users.router, prefix="/users", tags=["Users"])
app.include_router(ai_consent.router, prefix="/users", tags=["AI consent"])
app.include_router(flags.router, prefix="/flags", tags=["Country Flags"])
app.include_router(incoming_email.router, prefix="/emails", tags=["Incoming Email"])
app.include_router(webhook.router, prefix="/webhook", tags=["Webhook"])
app.include_router(legal.router, tags=["Legal"])
app.include_router(apple_ads.router, prefix="/apple-ads", tags=["Apple Ads"])
app.include_router(
    subscriptions.router, prefix="/subscriptions", tags=["apple subscriptions"]
)

app.include_router(cockpit.router, prefix="/cockpit", tags=["Aircraft messages"])
