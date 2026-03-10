import json

from ...models.aerodatabox import (
    AerodataboxAirport, AerodataboxAirportFlight, AerodataboxFlight,
    AerodataboxOriginAndDestinationInformation,
    AerodataboxOriginAndDestinationInformationForAirportResult)
from ...models.flight import (AirlineRead, Airport,
                              AirportFlightAirportInfoRead,
                              AirportFlightOriginAndDestinationInfoRead,
                              AirportFlightRead, Arrival, Departure, Flight)
from ...utils import get_time


class FlightMapper:
    @staticmethod
    def aero_airport_to_flight_airport(
        aerodatabox_airport: AerodataboxAirport,
    ) -> Airport:
        return Airport(
            name=aerodatabox_airport.name,
            iata=aerodatabox_airport.iata or "",
            municipality_name=aerodatabox_airport.municipalityName or "",
            lat=(
                aerodatabox_airport.location.lat
                if aerodatabox_airport.location
                else 0.0
            ),
            lon=(
                aerodatabox_airport.location.lon
                if aerodatabox_airport.location
                else 0.0
            ),
            country_code=aerodatabox_airport.countryCode or "",
        )

    @staticmethod
    def aero_departure_to_flight_departure(
        aero_departure: AerodataboxOriginAndDestinationInformation, airport: Airport
    ) -> Departure:
        return FlightMapper.aero_origin_to_flight_origin(
            aero_data=aero_departure, airport=airport, target_class=Departure
        )

    @staticmethod
    def aero_arrival_to_flight_arrival(
        aero_arrival: AerodataboxOriginAndDestinationInformation, airport: Airport
    ) -> Arrival:
        return FlightMapper.aero_origin_to_flight_origin(
            aero_data=aero_arrival, airport=airport, target_class=Arrival
        )

    @staticmethod
    def aero_origin_to_flight_origin[
        T: (Departure, Arrival)
    ](
        aero_data: AerodataboxOriginAndDestinationInformation,
        airport: Airport,
        target_class: type[T],
    ) -> T:
        fields = {
            "terminal": aero_data.terminal,
            "gate": aero_data.gate,
            "baggage_belt": aero_data.baggageBelt,
            "checkin_desk": aero_data.checkInDesk,
            "scheduled_time_local": get_time(aero_data.scheduledTime, "local"),
            "scheduled_time_utc": get_time(aero_data.scheduledTime, "utc"),
            "revised_time_local": get_time(aero_data.revisedTime, "local"),
            "revised_time_utc": get_time(aero_data.revisedTime, "utc"),
            "predicted_time_local": get_time(aero_data.predictedTime, "local"),
            "predicted_time_utc": get_time(aero_data.predictedTime, "utc"),
            "runway_time_local": get_time(aero_data.runwayTime, "local"),
            "runway_time_utc": get_time(aero_data.runwayTime, "utc"),
            "quality": json.dumps(aero_data.quality),
            "airport": airport,
        }
        return target_class(**fields)


class AirportFlightMapper:

    @staticmethod
    def airport_deparr_to_airport_deparr_read(
        origin_and_destination_info: AerodataboxOriginAndDestinationInformationForAirportResult,
        airport_iata: str,
    ) -> AirportFlightOriginAndDestinationInfoRead:
        fields = {
            "terminal": origin_and_destination_info.terminal,
            "gate": origin_and_destination_info.gate,
            "baggage_belt": origin_and_destination_info.baggageBelt,
            "checkin_desk": origin_and_destination_info.checkInDesk,
            "scheduled_time_local": get_time(
                origin_and_destination_info.scheduledTime, "local"
            ),
            "scheduled_time_utc": get_time(
                origin_and_destination_info.scheduledTime, "utc"
            ),
            "revised_time_local": get_time(
                origin_and_destination_info.revisedTime, "local"
            ),
            "revised_time_utc": get_time(
                origin_and_destination_info.revisedTime, "utc"
            ),
            "predicted_time_local": get_time(
                origin_and_destination_info.predictedTime, "local"
            ),
            "predicted_time_utc": get_time(
                origin_and_destination_info.predictedTime, "utc"
            ),
            "runway_time_local": get_time(
                origin_and_destination_info.runwayTime, "local"
            ),
            "runway_time_utc": get_time(origin_and_destination_info.runwayTime, "utc"),
            "quality": json.dumps(origin_and_destination_info.quality),
            "airport": AirportFlightAirportInfoRead(iata=airport_iata),
        }

        return AirportFlightOriginAndDestinationInfoRead(**fields)

    @staticmethod
    def airport_flight_to_airport_flight_read(
        flight: AerodataboxAirportFlight,
        departure_date: str,
        departure: AerodataboxOriginAndDestinationInformationForAirportResult,
        arrival: AerodataboxOriginAndDestinationInformationForAirportResult,
        departure_iata: str,
        arrival_iata: str,
    ) -> AirportFlightRead:
        return AirportFlightRead(
            number=flight.number,
            status=flight.status,
            date=departure_date,
            airline=(
                AirlineRead(
                    name=flight.airline.name,
                    iata=flight.airline.iata or "",
                    icao=flight.airline.icao or "",
                )
                if flight.airline
                else None
            ),
            departure=AirportFlightMapper.airport_deparr_to_airport_deparr_read(
                origin_and_destination_info=departure, airport_iata=departure_iata
            ),
            arrival=AirportFlightMapper.airport_deparr_to_airport_deparr_read(
                origin_and_destination_info=arrival, airport_iata=arrival_iata
            ),
        )
