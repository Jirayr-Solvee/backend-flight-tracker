from sqlmodel import Session, SQLModel, create_engine

from .device import Device
from .flight import Airline, Airport, Arrival, Departure, Flight
from .subscription import Subscription
from .transaction import Transaction

DATABASE_URL = "sqlite:///./database.db"
engine = create_engine(DATABASE_URL)

SQLModel.metadata.create_all(engine)


def get_session():
    with Session(engine) as session:
        yield session
