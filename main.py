from fastapi import FastAPI

# from . import lifespan
from .routers import (flags, flights, incoming_email, subscriptions, users,
                      webhook)

# app = FastAPI(lifespan=lifespan)
app = FastAPI()

app.include_router(flights.router, prefix="/flights", tags=["Flights"])
app.include_router(users.router, prefix="/users", tags=["Users"])
app.include_router(flags.router, prefix="/flags", tags=["Country Flags"])
app.include_router(incoming_email.router, prefix="/emails", tags=["Incoming Email"])
app.include_router(webhook.router, prefix="/webhook", tags=["Webhook"])
app.include_router(
    subscriptions.router, prefix="/subscriptions", tags=["apple subscriptions"]
)
