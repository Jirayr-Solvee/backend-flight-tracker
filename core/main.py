from fastapi import FastAPI
from .routers import experiment_diagnostics

# from . import lifespan
from .routers import (apple_ads, flags, flights, incoming_email, legal,
                      subscriptions, users, webhook)

# app = FastAPI(lifespan=lifespan)
app = FastAPI()
app.include_router(experiment_diagnostics.router, prefix="/subscriptions", tags=["Experiment diagnostics"])

app.include_router(flights.router, prefix="/flights", tags=["Flights"])
app.include_router(users.router, prefix="/users", tags=["Users"])
app.include_router(flags.router, prefix="/flags", tags=["Country Flags"])
app.include_router(incoming_email.router, prefix="/emails", tags=["Incoming Email"])
app.include_router(webhook.router, prefix="/webhook", tags=["Webhook"])
app.include_router(legal.router, tags=["Legal"])
app.include_router(apple_ads.router, prefix="/apple-ads", tags=["Apple Ads"])
app.include_router(
    subscriptions.router, prefix="/subscriptions", tags=["apple subscriptions"]
)
