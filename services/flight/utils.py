from ...models.aerodatabox import (AerodataboxAirportDetailForAirportResult,
                                   AerodataboxAirportFlight)
from .service import AirportSearchDirection


def append_iatas(
    direction: AirportSearchDirection,
    iata: str,
    flights: list[AerodataboxAirportFlight],
):
    new_flights_list = []
    if direction == AirportSearchDirection.DEPARTURE:
        # append iata into departures
        for f in flights:
            if f.departure and f.arrival:
                f.departure.airport = AerodataboxAirportDetailForAirportResult(
                    iata=iata
                )
                new_flights_list.append(f)
        return flights
    else:
        # append iata into arrivals
        for f in flights:
            if f.departure and f.arrival:
                f.arrival.airport = AerodataboxAirportDetailForAirportResult(iata=iata)
                new_flights_list.append(f)
        return flights
