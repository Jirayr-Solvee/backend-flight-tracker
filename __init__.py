import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI
from sqlmodel import SQLModel

from .models import engine

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    handlers=[logging.StreamHandler()],
)
logging.getLogger("watchfiles").setLevel(logging.WARNING)


# print("==========================")
# print(settings.deployed_url)
# print("==========================")

# @asynccontextmanager
# async def lifespan(app: FastAPI):

#     SQLModel.metadata.create_all(engine)
#     try:
#         yield
#     finally:
#         pass
#     # task = asyncio.create_task(manual_flight_status_updater())

#     # try:
#     #     yield
#     # finally:
#     #     task.cancel()
#     #     try:
#     #         await task
#     #     except asyncio.CancelledError:
#     #         pass
