import asyncio
import math
import re
from datetime import datetime, timezone
from statistics import median
from typing import Any

from pydantic import BaseModel, Field

from ...models.flight import Flight
from .api_client import AerodataboxClient


class DelayRiskDataPoints(BaseModel):
    flight_history_available: bool = False
    airport_delay_available: bool = False
    origin_samples: int | None = None
    destination_samples: int | None = None
    departure_airport_delay_index: float | None = None
    arrival_airport_delay_index: float | None = None
    historical_median_delay_minutes: int | None = None
    current_departure_delay_minutes: int | None = None
    current_arrival_delay_minutes: int | None = None


class DelayRiskResponse(BaseModel):
    flight_id: int
    level: str
    score: int = Field(ge=0, le=100)
    confidence: str
    confidence_score: float = Field(ge=0, le=1)
    likely_delay_min: int | None = None
    likely_delay_max: int | None = None
    summary: str
    reasons: list[str] = Field(default_factory=list)
    data_points: DelayRiskDataPoints
    generated_at: str


class DelayRiskService:
    @staticmethod
    async def build_delay_risk(flight: Flight) -> DelayRiskResponse:
        api_client = AerodataboxClient()

        full_number = flight.number.strip().replace(" ", "").upper()
        departure_airport_iata = (
            flight.departure.airport.iata
            if flight.departure and flight.departure.airport
            else None
        )
        arrival_airport_iata = (
            flight.arrival.airport.iata
            if flight.arrival and flight.arrival.airport
            else None
        )

        (
            flight_delay_stats,
            departure_airport_delay,
            arrival_airport_delay,
        ) = await asyncio.gather(
            api_client.get_flight_delays(full_number=full_number),
            api_client.get_airport_delay(airport_iata=departure_airport_iata),
            api_client.get_airport_delay(airport_iata=arrival_airport_iata),
        )
        flight_delay_stats = DelayRiskService._as_dict(flight_delay_stats)
        departure_airport_delay = DelayRiskService._as_dict(departure_airport_delay)
        arrival_airport_delay = DelayRiskService._as_dict(arrival_airport_delay)

        departure_hour = DelayRiskService._scheduled_hour(
            flight.departure.scheduled_time_utc if flight.departure else None
        )
        arrival_hour = DelayRiskService._scheduled_hour(
            flight.arrival.scheduled_time_utc if flight.arrival else None
        )

        origin_stats = DelayRiskService._as_list(
            flight_delay_stats.get("origins")
            or flight_delay_stats.get("origin")
            or flight_delay_stats.get("departure")
            or []
        )
        destination_stats = DelayRiskService._as_list(
            flight_delay_stats.get("destinations")
            or flight_delay_stats.get("destination")
            or flight_delay_stats.get("arrival")
            or []
        )

        origin_stat = DelayRiskService._pick_flight_delay_stat(
            stats=origin_stats, scheduled_hour=departure_hour
        )
        destination_stat = DelayRiskService._pick_flight_delay_stat(
            stats=destination_stats, scheduled_hour=arrival_hour
        )

        historical_median = DelayRiskService._best_historical_median(
            origin_stat=origin_stat, destination_stat=destination_stat
        )
        historical_p75 = DelayRiskService._best_percentile(
            origin_stat=origin_stat, destination_stat=destination_stat, percentile=75
        )
        historical_p90 = DelayRiskService._best_percentile(
            origin_stat=origin_stat, destination_stat=destination_stat, percentile=90
        )

        departure_current_delay = DelayRiskService._current_delay_minutes(
            flight.departure
        )
        arrival_current_delay = DelayRiskService._current_delay_minutes(flight.arrival)

        departure_airport_info = DelayRiskService._airport_delay_info(
            departure_airport_delay, direction="departure"
        )
        arrival_airport_info = DelayRiskService._airport_delay_info(
            arrival_airport_delay, direction="arrival"
        )

        score = 0
        confidence_score = 0.35
        reasons: list[str] = []
        likely_delay_inputs: list[int] = []

        if historical_median is not None:
            score += DelayRiskService._minutes_score(historical_median)
            likely_delay_inputs.append(historical_median)
            confidence_score += 0.2
            if historical_median > 5:
                reasons.append(
                    f"Historical median delay for this flight is {historical_median} min."
                )
            elif historical_median < -5:
                reasons.append(
                    f"This flight has historically run {-historical_median} min early."
                )
            else:
                reasons.append("Historical median delay is close to on time.")

        if historical_p75 is not None and historical_p75 > 15:
            score += min(15, max(0, round(historical_p75 / 4)))
            likely_delay_inputs.append(historical_p75)
            reasons.append(
                f"75th percentile historical delay is {historical_p75} min."
            )

        airport_indexes = [
            value
            for value in [
                departure_airport_info.get("delay_index"),
                arrival_airport_info.get("delay_index"),
            ]
            if value is not None
        ]
        if airport_indexes:
            max_index = max(airport_indexes)
            score += DelayRiskService._airport_index_score(max_index)
            confidence_score += 0.2
            if departure_airport_info.get("delay_index") is not None:
                reasons.append(
                    f"Departure airport delay index is {DelayRiskService._format_number(departure_airport_info['delay_index'])}/5."
                )
            if arrival_airport_info.get("delay_index") is not None:
                reasons.append(
                    f"Arrival airport delay index is {DelayRiskService._format_number(arrival_airport_info['delay_index'])}/5."
                )

        for airport_info in [departure_airport_info, arrival_airport_info]:
            airport_median_delay = airport_info.get("median_delay")
            if airport_median_delay is not None and airport_median_delay > 5:
                likely_delay_inputs.append(airport_median_delay)

        for label, delay in [
            ("departure", departure_current_delay),
            ("arrival", arrival_current_delay),
        ]:
            if delay is None:
                continue
            confidence_score += 0.05
            if delay > 0:
                score += min(25, max(4, round(delay / 2)))
                likely_delay_inputs.append(delay)
                reasons.append(
                    f"Current {label} timing is already {delay} min behind schedule."
                )
            elif delay < -5:
                score -= 4

        status = getattr(flight.status, "value", flight.status)
        if status == "Delayed":
            score += 18
            reasons.append("AeroDataBox currently marks the flight as delayed.")
        elif status in {"Canceled", "CanceledUncertain", "Diverted"}:
            score = max(score, 85)
            reasons.append(f"AeroDataBox currently marks the flight as {status}.")
        elif status in {"Arrived", "EnRoute", "Departed"}:
            confidence_score += 0.1

        sample_counts = [
            DelayRiskService._sample_count(origin_stat),
            DelayRiskService._sample_count(destination_stat),
        ]
        known_sample_counts = [count for count in sample_counts if count is not None]
        if known_sample_counts:
            best_count = max(known_sample_counts)
            if best_count >= 30:
                confidence_score += 0.15
            elif best_count >= 10:
                confidence_score += 0.08
            elif best_count < 5:
                confidence_score -= 0.08
                reasons.append("Historical sample size is limited.")

        score = max(0, min(100, score))
        confidence_score = max(0.1, min(0.95, confidence_score))

        level = DelayRiskService._level(score=score)
        confidence = DelayRiskService._confidence(confidence_score)
        likely_min, likely_max = DelayRiskService._likely_delay_range(
            values=likely_delay_inputs,
            p90=historical_p90,
            level=level,
        )

        if not reasons:
            reasons.append("No strong delay signals were found from current data.")

        return DelayRiskResponse(
            flight_id=flight.id or 0,
            level=level,
            score=score,
            confidence=confidence,
            confidence_score=round(confidence_score, 2),
            likely_delay_min=likely_min,
            likely_delay_max=likely_max,
            summary=DelayRiskService._summary(
                level=level, likely_min=likely_min, likely_max=likely_max
            ),
            reasons=reasons[:4],
            data_points=DelayRiskDataPoints(
                flight_history_available=bool(origin_stat or destination_stat),
                airport_delay_available=bool(airport_indexes),
                origin_samples=DelayRiskService._sample_count(origin_stat),
                destination_samples=DelayRiskService._sample_count(destination_stat),
                departure_airport_delay_index=departure_airport_info.get(
                    "delay_index"
                ),
                arrival_airport_delay_index=arrival_airport_info.get("delay_index"),
                historical_median_delay_minutes=historical_median,
                current_departure_delay_minutes=departure_current_delay,
                current_arrival_delay_minutes=arrival_current_delay,
            ),
            generated_at=datetime.now(timezone.utc).isoformat(),
        )

    @staticmethod
    def _as_list(value: Any) -> list[dict[str, Any]]:
        if isinstance(value, list):
            return [item for item in value if isinstance(item, dict)]
        if isinstance(value, dict):
            return [value]
        return []

    @staticmethod
    def _as_dict(value: Any) -> dict[str, Any]:
        return value if isinstance(value, dict) else {}

    @staticmethod
    def _first_present(data: dict[str, Any], *keys: str) -> Any:
        for key in keys:
            if key in data and data[key] is not None:
                return data[key]
        return None

    @staticmethod
    def _pick_flight_delay_stat(
        stats: list[dict[str, Any]], scheduled_hour: int | None
    ) -> dict[str, Any] | None:
        if not stats:
            return None

        if scheduled_hour is None:
            return max(stats, key=lambda item: DelayRiskService._sample_count(item) or 0)

        def score(item: dict[str, Any]) -> tuple[int, int]:
            item_hour = DelayRiskService._int_value(
                DelayRiskService._first_present(
                    item, "scheduledHourUtc", "scheduledHour", "hourUtc"
                )
            )
            sample_count = DelayRiskService._sample_count(item) or 0
            if item_hour is None:
                return (24, -sample_count)
            distance = abs(item_hour - scheduled_hour)
            distance = min(distance, 24 - distance)
            return (distance, -sample_count)

        return min(stats, key=score)

    @staticmethod
    def _best_historical_median(
        origin_stat: dict[str, Any] | None, destination_stat: dict[str, Any] | None
    ) -> int | None:
        values = [
            DelayRiskService._delay_minutes(stat.get("medianDelay"))
            for stat in [origin_stat, destination_stat]
            if stat
        ]
        values = [value for value in values if value is not None]
        if not values:
            return None
        return round(median(values))

    @staticmethod
    def _best_percentile(
        origin_stat: dict[str, Any] | None,
        destination_stat: dict[str, Any] | None,
        percentile: int,
    ) -> int | None:
        values = [
            DelayRiskService._percentile_minutes(stat, percentile)
            for stat in [origin_stat, destination_stat]
            if stat
        ]
        values = [value for value in values if value is not None]
        if not values:
            return None
        return round(median(values))

    @staticmethod
    def _percentile_minutes(
        stat: dict[str, Any] | None, percentile: int
    ) -> int | None:
        if not stat:
            return None

        candidates = [
            stat.get(f"p{percentile}"),
            stat.get(f"delayP{percentile}"),
            stat.get(f"percentile{percentile}"),
        ]
        for candidate in candidates:
            minutes = DelayRiskService._delay_minutes(candidate)
            if minutes is not None:
                return minutes

        percentiles = stat.get("delayPercentiles") or stat.get("percentiles") or []
        if isinstance(percentiles, dict):
            return DelayRiskService._delay_minutes(
                percentiles.get(str(percentile)) or percentiles.get(percentile)
            )

        if isinstance(percentiles, list):
            for item in percentiles:
                if not isinstance(item, dict):
                    continue
                item_percentile = DelayRiskService._int_value(
                    item.get("percentile") or item.get("level")
                )
                if item_percentile != percentile:
                    continue
                return DelayRiskService._delay_minutes(
                    item.get("delay") or item.get("value")
                )

        return None

    @staticmethod
    def _airport_delay_info(data: dict[str, Any], direction: str) -> dict[str, Any]:
        if not data:
            return {}

        keys = (
            [
                "departuresDelayInformation",
                "departureDelayInformation",
                "departures",
                "departure",
            ]
            if direction == "departure"
            else [
                "arrivalsDelayInformation",
                "arrivalDelayInformation",
                "arrivals",
                "arrival",
            ]
        )

        info = None
        for key in keys:
            candidate = data.get(key)
            if isinstance(candidate, dict):
                info = candidate
                break

        if not info:
            info = data

        return {
            "delay_index": DelayRiskService._float_value(
                DelayRiskService._first_present(info, "delayIndex", "index")
            ),
            "median_delay": DelayRiskService._delay_minutes(info.get("medianDelay")),
            "cancelled": DelayRiskService._int_value(
                DelayRiskService._first_present(
                    info, "cancelled", "canceled", "numCancelled"
                )
            ),
        }

    @staticmethod
    def _current_delay_minutes(deparr: Any) -> int | None:
        if not deparr:
            return None

        scheduled = deparr.scheduled_time_utc
        current = (
            deparr.runway_time_utc
            or deparr.predicted_time_utc
            or deparr.revised_time_utc
        )
        if not scheduled or not current:
            return None

        scheduled_date = DelayRiskService._parse_utc(scheduled)
        current_date = DelayRiskService._parse_utc(current)
        if not scheduled_date or not current_date:
            return None

        return round((current_date - scheduled_date).total_seconds() / 60)

    @staticmethod
    def _scheduled_hour(value: str | None) -> int | None:
        parsed = DelayRiskService._parse_utc(value)
        if not parsed:
            return None
        return parsed.hour

    @staticmethod
    def _parse_utc(value: str | None) -> datetime | None:
        if not value:
            return None

        for fmt in ("%Y-%m-%d %H:%MZ", "%Y-%m-%dT%H:%MZ", "%Y-%m-%dT%H:%M:%SZ"):
            try:
                return datetime.strptime(value, fmt).replace(tzinfo=timezone.utc)
            except ValueError:
                continue
        return None

    @staticmethod
    def _delay_minutes(value: Any) -> int | None:
        if value is None:
            return None

        if isinstance(value, (int, float)):
            return round(value)

        if not isinstance(value, str):
            return None

        value = value.strip()
        match = re.match(
            r"^(?P<sign>-)?(?:(?P<days>\d+)\.)?(?P<hours>\d{1,2}):(?P<minutes>\d{2})(?::(?P<seconds>\d{2}))?$",
            value,
        )
        if not match:
            return None

        days = int(match.group("days") or 0)
        hours = int(match.group("hours") or 0)
        minutes = int(match.group("minutes") or 0)
        seconds = int(match.group("seconds") or 0)
        total = days * 1440 + hours * 60 + minutes + round(seconds / 60)
        return -total if match.group("sign") else total

    @staticmethod
    def _sample_count(stat: dict[str, Any] | None) -> int | None:
        if not stat:
            return None

        return DelayRiskService._int_value(
            DelayRiskService._first_present(
                stat,
                "numConsideredFlights",
                "consideredFlights",
                "sampleSize",
                "count",
            )
        )

    @staticmethod
    def _int_value(value: Any) -> int | None:
        if value is None:
            return None
        try:
            return int(value)
        except (TypeError, ValueError):
            return None

    @staticmethod
    def _float_value(value: Any) -> float | None:
        if value is None:
            return None
        try:
            parsed = float(value)
            if math.isnan(parsed):
                return None
            return parsed
        except (TypeError, ValueError):
            return None

    @staticmethod
    def _minutes_score(minutes: int) -> int:
        if minutes >= 45:
            return 35
        if minutes >= 30:
            return 28
        if minutes >= 15:
            return 18
        if minutes >= 5:
            return 8
        if minutes <= -5:
            return -4
        return 0

    @staticmethod
    def _airport_index_score(index: float) -> int:
        if index >= 4:
            return 30
        if index >= 3:
            return 22
        if index >= 2:
            return 13
        if index >= 1:
            return 5
        return 0

    @staticmethod
    def _level(score: int) -> str:
        if score >= 55:
            return "high"
        if score >= 25:
            return "medium"
        return "low"

    @staticmethod
    def _confidence(confidence_score: float) -> str:
        if confidence_score >= 0.7:
            return "high"
        if confidence_score >= 0.45:
            return "medium"
        return "low"

    @staticmethod
    def _likely_delay_range(
        values: list[int], p90: int | None, level: str
    ) -> tuple[int | None, int | None]:
        positive_values = [max(0, value) for value in values if value is not None]
        if not positive_values:
            if level == "low":
                return (0, 10)
            return (None, None)

        likely = round(median(positive_values))
        lower = max(0, likely - 10)
        upper = max(likely + 10, p90 or likely + 10)
        upper = min(240, upper)
        return (lower, upper)

    @staticmethod
    def _summary(
        level: str, likely_min: int | None, likely_max: int | None
    ) -> str:
        title = {
            "high": "High delay risk",
            "medium": "Medium delay risk",
            "low": "Low delay risk",
        }.get(level, "Delay risk")

        if likely_min is None or likely_max is None:
            return title

        if likely_min == 0 and likely_max <= 10:
            return f"{title} - likely on time"

        return f"{title} - likely {likely_min}-{likely_max} min"

    @staticmethod
    def _format_number(value: float) -> str:
        if value.is_integer():
            return str(int(value))
        return f"{value:.1f}"
