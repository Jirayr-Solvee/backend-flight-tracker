from enum import Enum

from pydantic import BaseModel, field_validator

# ---------------- Models for reading aerodatabox responses ------------------


class QualityEnum(str, Enum):
    BASIC = "Basic"
    LIVE = "Live"
    APPROXIMATE = "Approximate"


class FlightStatusEnum(str, Enum):
    UNKNOWN = "Unknown"
    EXPECTED = "Expected"
    ENROUTE = "EnRoute"

    CHECKIN = "CheckIn"
    BOARDING = "Boarding"
    GATECLOSED = "GateClosed"

    DEPARTED = "Departed"
    DELAYED = "Delayed"
    APPROACHING = "Approaching"

    ARRIVED = "Arrived"
    CANCELED = "Canceled"
    DIVERTED = "Diverted"

    CANCELEDUNCERTAIN = "CanceledUncertain"


class AerodataboxAirline(BaseModel):
    name: str
    iata: str | None = None
    icao: str | None = None


class AerodataboxAircraft(BaseModel):
    reg: str | None = None
    modeS: str | None = None
    model: str | None = None


class AerodataboxGreatCircleDistance(BaseModel):
    meter: float
    km: float
    mile: float
    nm: float
    feet: float


class AerodataboxTimeStamp(BaseModel):
    utc: str
    local: str


class AerodataboxAirportLocation(BaseModel):
    lat: float
    lon: float


class AerodataboxAirport(BaseModel):
    name: str
    icao: str | None = None
    iata: str | None = None
    localCode: str | None = None
    shortName: str | None = None
    municipalityName: str | None = None

    location: AerodataboxAirportLocation | None = None

    countryCode: str | None = None
    timeZone: str | None = None


class AerodataboxOriginAndDestinationInformation(BaseModel):
    airport: AerodataboxAirport

    scheduledTime: AerodataboxTimeStamp | None = None
    revisedTime: AerodataboxTimeStamp | None = None
    predictedTime: AerodataboxTimeStamp | None = None
    runwayTime: AerodataboxTimeStamp | None = None

    terminal: str | None = None
    checkInDesk: str | None = None
    gate: str | None = None
    baggageBelt: str | None = None
    runway: str | None = None

    quality: list[QualityEnum]


class AerodataboxFlight(BaseModel):
    greatCircleDistance: AerodataboxGreatCircleDistance | None = None

    departure: AerodataboxOriginAndDestinationInformation
    arrival: AerodataboxOriginAndDestinationInformation

    lastUpdatedUtc: str

    number: str
    callSign: str | None = None
    status: FlightStatusEnum

    codeshareStatus: str
    isCargo: bool

    aircraft: AerodataboxAircraft | None = None

    airline: AerodataboxAirline | None = None


# ---------------- Models for webhook ------------------

status_map = {
    0: FlightStatusEnum.UNKNOWN,
    1: FlightStatusEnum.EXPECTED,
    2: FlightStatusEnum.ENROUTE,
    3: FlightStatusEnum.CHECKIN,
    4: FlightStatusEnum.BOARDING,
    5: FlightStatusEnum.GATECLOSED,
    6: FlightStatusEnum.DEPARTED,
    7: FlightStatusEnum.DELAYED,
    8: FlightStatusEnum.APPROACHING,
    9: FlightStatusEnum.ARRIVED,
    10: FlightStatusEnum.CANCELED,
    11: FlightStatusEnum.DIVERTED,
    12: FlightStatusEnum.CANCELEDUNCERTAIN,
}

airport_quality_map = {
    0: QualityEnum.BASIC,
    1: QualityEnum.LIVE,
    2: QualityEnum.APPROXIMATE,
}

from typing import Union


class AerodataboxOriginAndDestinationInformationWebhook(BaseModel):
    airport: AerodataboxAirport

    scheduledTime: AerodataboxTimeStamp | None = None
    revisedTime: AerodataboxTimeStamp | None = None
    predictedTime: AerodataboxTimeStamp | None = None
    runwayTime: AerodataboxTimeStamp | None = None

    terminal: str | None = None
    checkInDesk: str | None = None
    gate: str | None = None
    baggageBelt: str | None = None
    runway: str | None = None

    quality: list[Union[int, QualityEnum]]

    @field_validator("quality", mode="before")
    def map_status(cls, v):
        if not isinstance(v, list):
            v = [v]

        return [
            airport_quality_map.get(i, QualityEnum.BASIC) if isinstance(i, int) else i
            for i in v
        ]


class AerodataboxFlightWebhook(BaseModel):
    departure: AerodataboxOriginAndDestinationInformationWebhook
    arrival: AerodataboxOriginAndDestinationInformationWebhook

    lastUpdatedUtc: str

    number: str
    status: Union[int, FlightStatusEnum]

    # isCargo: bool

    aircraft: AerodataboxAircraft | None = None

    airline: AerodataboxAirline | None = None

    @field_validator("status", mode="before")
    def map_status(cls, v):
        # If the webhook sends a number, convert it
        if isinstance(v, int):
            return status_map.get(v, FlightStatusEnum.UNKNOWN)
        return v


class FlightNotificationContractSubject(BaseModel):
    type: str
    id: str | None = None


class FlightNotificationContractsubscriber(BaseModel):
    type: str
    id: str


class FlightNotificationContractSubscription(BaseModel):
    id: str
    isActive: bool
    activateBeforeUtc: str | None = None
    expiresOnUtc: str | None = None
    createdOnUtc: str
    # subject: FlightNotificationContractSubject
    # subscriber: FlightNotificationContractsubscriber


class FlightNotificationContractItem(AerodataboxFlightWebhook):
    notificationSummary: str | None = None
    notificationRemark: str | None = None


class FlightNotificationContract(BaseModel):
    flights: list[FlightNotificationContractItem] = []
    subscription: FlightNotificationContractSubscription | None = None


# ---------------- Models for reading response of aerodatabox-api-search-via-airport ------------------


# NOTE: striped it down -> only reading departure as of now
class AerodataboxAirportDetailForAirportResult(BaseModel):
    iata: str | None = None


class AerodataboxOriginAndDestinationInformationForAirportResult(
    AerodataboxOriginAndDestinationInformation
):
    airport: AerodataboxAirportDetailForAirportResult | None = (  # type: ignore[assignment]
        AerodataboxAirportDetailForAirportResult()
    )


class AerodataboxAirportFlight(BaseModel):
    departure: AerodataboxOriginAndDestinationInformationForAirportResult | None = None
    arrival: AerodataboxOriginAndDestinationInformationForAirportResult | None = None

    number: str
    status: FlightStatusEnum

    airline: AerodataboxAirline | None = None


class AirportFidsContract(BaseModel):
    departures: list[AerodataboxAirportFlight] | None = []
    arrivals: list[AerodataboxAirportFlight] | None = []
