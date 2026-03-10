import json
from enum import Enum
from typing import List, Optional

from pydantic import field_validator
from sqlmodel import Field, Relationship, SQLModel

from .aerodatabox import FlightStatusEnum, QualityEnum
from .user import UserFlightLink


class TimestampTypes(str, Enum):
    ACTUAL = "Actual"
    ESTIMATED = "Estimated"
    SCHEDULED = "Scheduled"
    REVISED = "Revised"


class Flight(SQLModel, table=True):
    id: Optional[int] = Field(default=None, primary_key=True)
    date: str
    number: str
    status: str
    distance_km: float | None = None
    distance_mile: float | None = None
    subscription_id: str | None = None
    aircraft_reg: str | None = None
    aircraft_modeS: str | None = None
    aircraft_model: str | None = None

    airline_id: Optional[int] = Field(default=None, foreign_key="airline.id")
    airline: Optional["Airline"] = Relationship(back_populates="flights")

    departure: Optional["Departure"] = Relationship(
        back_populates="flight", sa_relationship_kwargs={"uselist": False}
    )
    arrival: Optional["Arrival"] = Relationship(
        back_populates="flight", sa_relationship_kwargs={"uselist": False}
    )

    users: List["User"] = Relationship(
        back_populates="flights", link_model=UserFlightLink
    )


class Airline(SQLModel, table=True):
    id: Optional[int] = Field(default=None, primary_key=True)
    name: str
    iata: str
    icao: str

    flights: List["Flight"] = Relationship(back_populates="airline")


class FlightOriginAndDestinationInformation(SQLModel):
    id: Optional[int] = Field(default=None, primary_key=True)
    terminal: str | None = None
    gate: str | None = None
    baggage_belt: str | None = None
    checkin_desk: str | None = None

    scheduled_time_local: str | None = None
    scheduled_time_utc: str | None = None

    revised_time_local: str | None = None
    revised_time_utc: str | None = None

    predicted_time_local: str | None = None
    predicted_time_utc: str | None = None

    runway_time_local: str | None = None
    runway_time_utc: str | None = None

    quality: str | None = None


class Departure(FlightOriginAndDestinationInformation, table=True):
    airport_id: Optional[int] = Field(default=None, foreign_key="airport.id")
    airport: Optional["Airport"] = Relationship(back_populates="departures")

    flight_id: Optional[int] = Field(default=None, foreign_key="flight.id")
    flight: Optional["Flight"] = Relationship(back_populates="departure")


class Arrival(FlightOriginAndDestinationInformation, table=True):
    airport_id: Optional[int] = Field(default=None, foreign_key="airport.id")
    airport: Optional["Airport"] = Relationship(back_populates="arrivals")

    flight_id: Optional[int] = Field(default=None, foreign_key="flight.id")
    flight: Optional["Flight"] = Relationship(back_populates="arrival")


class Airport(SQLModel, table=True):
    id: Optional[int] = Field(default=None, primary_key=True)
    iata: str
    name: str  # full name always
    municipality_name: str
    lat: float  # TODO: mark as null and ios app hanlde this case
    lon: float
    country_code: str

    departures: List[Departure] = Relationship(back_populates="airport")
    arrivals: List[Arrival] = Relationship(back_populates="airport")


# ---------------- Models for reading via our routes ------------------


class AirportRead(SQLModel):
    name: str
    iata: str | None
    municipality_name: str | None
    lat: float | None
    lon: float | None
    country_code: str | None


class DepartureRead(SQLModel):
    terminal: str | None
    baggage_belt: str | None
    checkin_desk: str | None
    gate: str | None
    scheduled_time_local: str | None
    scheduled_time_utc: str | None
    revised_time_local: str | None
    revised_time_utc: str | None
    predicted_time_local: str | None
    predicted_time_utc: str | None
    runway_time_local: str | None
    runway_time_utc: str | None
    # quality: str | None
    quality: list[QualityEnum] | None
    airport: AirportRead

    @field_validator("quality", mode="before")
    @classmethod
    def decode_quality(cls, v):
        if v is None:
            return None

        if isinstance(v, str):
            try:
                return json.loads(v)
            except Exception:
                return []

        return v


class ArrivalRead(DepartureRead):
    pass


class AirlineRead(SQLModel):
    name: str
    iata: str
    icao: str


class FlightRead(SQLModel):
    id: int
    number: str
    status: FlightStatusEnum
    date: str
    distance_km: float | None
    distance_mile: float | None
    airline: AirlineRead
    departure: DepartureRead | None
    arrival: ArrivalRead | None
    aircraft_reg: str | None = None
    aircraft_modeS: str | None = None
    aircraft_model: str | None = None


# ------- Models for airport search result --------
class AirportFlightAirportInfoRead(SQLModel):
    iata: str


class AirportFlightOriginAndDestinationInfoRead(DepartureRead):
    airport: AirportFlightAirportInfoRead  # type: ignore[assignment]


class AirportFlightRead(SQLModel):
    number: str
    status: str
    date: str
    airline: AirlineRead | None
    departure: AirportFlightOriginAndDestinationInfoRead | None
    arrival: AirportFlightOriginAndDestinationInfoRead | None


class QuerySearchResponse(SQLModel):
    flights_result: list[FlightRead] = []
    airport_flights_result: list[AirportFlightRead] = []
