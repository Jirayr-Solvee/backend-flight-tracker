import asyncio
import json
import logging
import re
from datetime import timedelta
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from google import genai
from google.genai.types import GenerateContentConfig, GenerateContentResponse
from pydantic import BaseModel

from ...config import settings
from .config import REQUIRED_FIELDS, email_config, query_config

logger = logging.getLogger(__name__)


class FunctionCallResult(BaseModel):
    function_name: str
    args: dict[str, Any]


class ResolvedFunctionCall(FunctionCallResult):
    handler: Callable[..., Any]


class GeminiService:
    _iata_by_icao_cache: dict[str, str] | None = None

    def __init__(self):
        api_key = (settings.GEMINI_API_KEY or "").strip()
        if not api_key:
            logger.error("GEMINI_API_KEY is not configured")
            self.client = None
            return

        self.client = genai.Client(api_key=api_key)

    async def _generate(
        self, contents: str, config: GenerateContentConfig
    ) -> GenerateContentResponse:
        if self.client is None:
            raise RuntimeError("Gemini client is not configured")

        return await asyncio.to_thread(
            lambda: self.client.models.generate_content(
                model="gemini-2.5-flash",
                contents=contents,
                config=config,
            )
        )

    def _extract_function_call(
        self, response: GenerateContentResponse
    ) -> FunctionCallResult | None:
        if not response.candidates:
            return None

        for candidate in response.candidates:
            content = candidate.content
            if not content or not content.parts:
                continue

            # other wise loop on each part
            for part in content.parts:

                if part.function_call:
                    fc = part.function_call

                    if not fc.name or not fc.args:
                        logger.warning(
                            f"function name or function args not found for function call: {fc}"
                        )
                        continue

                    return FunctionCallResult(
                        function_name=fc.name,
                        args=dict(fc.args),
                    )
        logger.warning(f"Unable to extract function call from response: {response}")
        return None

    async def get_function_call(
        self, query: str, email: bool = False, language: str | None = None
    ) -> ResolvedFunctionCall | None:
        if not email:
            fallback = self._deterministic_function_call(query)
            if fallback:
                return fallback

            if self.client is None:
                return None

            today_utc = datetime.now(timezone.utc).strftime("%Y-%m-%d")
            language_context = language or "unknown"
            query = (
                f"Current date (UTC) is {today_utc}.\n"
                f"User locale/language is {language_context}.\n"
                "The user may write the flight search in any language. "
                "Interpret translated city names, airport names, route words such as from/to, "
                "and date words such as today/tomorrow/yesterday according to the user locale. "
                "Always normalize airports to valid IATA airport codes, airline codes to IATA airline codes, "
                "and dates to YYYY-MM-DD. Do not translate flight numbers or IATA codes. "
                "If the user asks for a random flight in any language, call extract_random_flight. "
                f"User query: {query}"
            )

        attempts = 3
        for attempt in range(attempts):
            try:
                response = await self._generate(
                    query, email_config if email else query_config
                )
                if not response:
                    logger.warning(
                        f"Gemini produced invalid response={response}, for query={query}, email={email}, attempt={attempt}"
                    )
                    continue

                # check validity of those functions name and args
                extracted_function_call = self._extract_function_call(response)
                if not extracted_function_call:
                    continue

                valid_function_call = self._validate_function_args(
                    function_name=extracted_function_call.function_name,
                    args=extracted_function_call.args,
                )
                if not valid_function_call:
                    logger.warning(
                        f"Gemini produced invalid function_call={extracted_function_call}, for query={query}, email={email}, attempt={attempt}"
                    )
                    continue

                function_def = REQUIRED_FIELDS.get(
                    extracted_function_call.function_name
                )

                handler = function_def.handler # type: ignore

                return ResolvedFunctionCall(
                    function_name=extracted_function_call.function_name,
                    args=extracted_function_call.args,
                    handler=handler,
                )
            except Exception:
                logger.exception(
                    f"Error while retriving a function call for query={query}, email={email}, attempt={attempt}"
                )

        logger.warning(
            f"Gemini unable to extract a function call for query={query}, email={email}, after all attempts"
        )
        return None

    def _validate_function_args(
        self,
        function_name: str,
        args: dict[str, Any],
    ) -> bool:
        function_def = REQUIRED_FIELDS.get(function_name)

        if not function_def:
            logger.warning(f"un-registred function: {function_name}")
            return False

        required_fields = function_def.required_fields
        missing_fields = [
            field
            for field in required_fields
            if field not in args
            or args[field] is None
            or (isinstance(args[field], str) and args[field].strip() == "")
        ]

        if missing_fields:
            logger.warning(
                f"Missing fields {missing_fields} for function {function_name}"
            )
            return False

        return True

    def _deterministic_function_call(self, query: str) -> ResolvedFunctionCall | None:
        normalized = query.strip()
        lowered = normalized.casefold()

        if self._looks_like_random_request(lowered):
            return self._resolved_function_call(
                function_name="extract_random_flight",
                args={"random": True},
            )

        flight = self._flight_from_query(normalized)
        if flight:
            return self._resolved_function_call(
                function_name="extract_flight_info",
                args={
                    "airline_iata": flight[0],
                    "flight_number": flight[1],
                    "departure_date": self._date_from_query(lowered),
                },
            )

        route_match = re.search(
            r"\b(?:FROM\s+)?([A-Z]{3})\s+(?:TO|INTO|-|→)\s+([A-Z]{3})\b",
            normalized.upper(),
        )
        if route_match:
            return self._resolved_function_call(
                function_name="extract_flight_info_via_airport",
                args={
                    "departure_airport_iata": route_match.group(1),
                    "arrival_airport_iata": route_match.group(2),
                    "departure_date": self._date_from_query(lowered),
                },
            )

        aliased_route = self._route_from_aliases(lowered)
        if aliased_route:
            return self._resolved_function_call(
                function_name="extract_flight_info_via_airport",
                args={
                    "departure_airport_iata": aliased_route[0],
                    "arrival_airport_iata": aliased_route[1],
                    "departure_date": self._date_from_query(lowered),
                },
            )

        single_airport = self._single_airport_from_aliases(lowered)
        if single_airport:
            return self._resolved_function_call(
                function_name="extract_flight_info_via_airport_single_derection",
                args={
                    "airport_iata": single_airport[0],
                    "direction": single_airport[1],
                    "departure_date": self._date_from_query(lowered),
                },
            )

        airline_iata = self._airline_only_query(lowered)
        if airline_iata:
            return self._resolved_function_call(
                function_name="extract_airline_live_flights",
                args={
                    "airline_iata": airline_iata,
                    "departure_date": self._date_from_query(lowered),
                },
            )

        return None

    def _resolved_function_call(
        self,
        function_name: str,
        args: dict[str, Any],
    ) -> ResolvedFunctionCall | None:
        function_def = REQUIRED_FIELDS.get(function_name)
        if not function_def:
            return None

        return ResolvedFunctionCall(
            function_name=function_name,
            args=args,
            handler=function_def.handler,
        )

    @staticmethod
    def _looks_like_random_request(lowered_query: str) -> bool:
        random_tokens = {
            "random",
            "suggest random",
            "suggested",
            "surprise",
            "sample",
            "rastgele",
            "aleatorio",
            "aleatório",
            "azar",
            "aléatoire",
            "zufällig",
            "casuale",
            "عشوائي",
        }
        return any(token in lowered_query for token in random_tokens)

    @staticmethod
    def _date_from_query(lowered_query: str) -> str:
        today = datetime.now(timezone.utc).date()
        tomorrow_tokens = {
            "tomorrow",
            "mañana",
            "demain",
            "morgen",
            "domani",
            "amanhã",
            "yarın",
            "غد",
            "غدًا",
        }
        yesterday_tokens = {
            "yesterday",
            "ayer",
            "hier",
            "gestern",
            "ieri",
            "ontem",
            "dün",
            "أمس",
        }

        if any(token in lowered_query for token in tomorrow_tokens):
            return (today + timedelta(days=1)).isoformat()

        if any(token in lowered_query for token in yesterday_tokens):
            return (today - timedelta(days=1)).isoformat()

        return today.isoformat()

    @classmethod
    def _flight_from_query(cls, query: str) -> tuple[str, str] | None:
        lowered = query.casefold()
        for alias, airline_iata in cls._airline_aliases_by_length():
            match = re.search(
                rf"(?<![a-z0-9]){re.escape(alias)}(?![a-z0-9])"
                r"(?:\s+flight)?\s+([0-9]{1,4}[a-z]?)\b",
                lowered,
            )
            if match:
                return airline_iata, match.group(1).upper()

        upper = query.upper()
        for pattern in (
            r"(?<![A-Z0-9])([A-Z]{3})\s*([0-9]{1,4}[A-Z]?)(?![A-Z0-9])",
            r"(?<![A-Z0-9])([A-Z0-9]{2})\s*([0-9]{1,4}[A-Z]?)(?![A-Z0-9])",
        ):
            match = re.search(pattern, upper)
            if not match:
                continue

            airline_iata = cls._iata_for_airline_token(match.group(1))
            if airline_iata:
                return airline_iata, match.group(2)

        return None

    @classmethod
    def _route_from_aliases(cls, lowered_query: str) -> tuple[str, str] | None:
        normalized = cls._normalized_route_text(lowered_query)
        for departure_name, departure_iata in cls._airport_aliases_by_length():
            if departure_name not in normalized:
                continue

            for arrival_name, arrival_iata in cls._airport_aliases_by_length():
                if departure_iata == arrival_iata or arrival_name not in normalized:
                    continue

                if cls._ordered_route_match(
                    normalized,
                    departure_name=departure_name,
                    arrival_name=arrival_name,
                ):
                    return departure_iata, arrival_iata

        return None

    @classmethod
    def _single_airport_from_aliases(cls, lowered_query: str) -> tuple[str, str] | None:
        normalized = cls._normalized_route_text(lowered_query)
        stripped = normalized.strip()

        for airport_name, airport_iata in cls._airport_aliases_by_length():
            if len(airport_name) < 3 and stripped != airport_name:
                continue

            if not re.search(
                rf"(?<![a-z0-9]){re.escape(airport_name)}(?![a-z0-9])",
                normalized,
            ):
                continue

            return airport_iata, cls._single_airport_direction(normalized)

        return None

    @staticmethod
    def _single_airport_direction(normalized_query: str) -> str:
        if re.search(r"\b(to|into|arrivals?|arriving|inbound)\b", normalized_query):
            return "Arrival"

        return "Departure"

    @classmethod
    def _airline_only_query(cls, lowered_query: str) -> str | None:
        normalized = re.sub(r"\s+", " ", lowered_query).strip()
        normalized = re.sub(
            r"\b(flights?|airline|airlines|departures?|arrivals?|today|tomorrow|yesterday)\b",
            "",
            normalized,
        )
        normalized = re.sub(r"\s+", " ", normalized).strip()

        for alias, airline_iata in cls._airline_aliases_by_length():
            if normalized == alias:
                return airline_iata

        return cls._iata_for_airline_token(normalized.upper())

    @staticmethod
    def _normalized_route_text(value: str) -> str:
        normalized = re.sub(
            r"\s+",
            " ",
            value.replace("→", " to ").replace("-", " to "),
        ).strip()
        return f" {normalized} "

    @staticmethod
    def _ordered_route_match(
        normalized_query: str,
        departure_name: str,
        arrival_name: str,
    ) -> bool:
        departure_index = normalized_query.find(departure_name)
        arrival_index = normalized_query.find(arrival_name)
        if departure_index < 0 or arrival_index < 0 or departure_index >= arrival_index:
            return False

        between = normalized_query[
            departure_index + len(departure_name):arrival_index
        ]
        route_words = (" to ", " from ", " a ", " à ", " para ", " nach ", " vers ")
        return any(word in between for word in route_words)

    @staticmethod
    def _airport_aliases() -> dict[str, str]:
        return {
            "los angeles": "LAX",
            "la": "LAX",
            "lax": "LAX",
            "new york": "JFK",
            "nyc": "JFK",
            "jfk": "JFK",
            "london": "LHR",
            "london heathrow": "LHR",
            "lhr": "LHR",
            "london luton": "LTN",
            "luton": "LTN",
            "ltn": "LTN",
            "paris": "CDG",
            "parís": "CDG",
            "cdg": "CDG",
            "istanbul": "IST",
            "ist": "IST",
            "dubai": "DXB",
            "dxb": "DXB",
            "sao paulo": "GRU",
            "são paulo": "GRU",
            "gru": "GRU",
            "yerevan": "EVN",
            "evn": "EVN",
            "prishtina": "PRN",
            "pristina": "PRN",
            "prn": "PRN",
            "honolulu": "HNL",
            "hawaii": "HNL",
            "hnl": "HNL",
            "noida international airport": "DXN",
            "noida intertional": "DXN",
            "noida": "DXN",
            "dxn": "DXN",
            "ahmedabad": "AMD",
            "amd": "AMD",
            "nanded": "NDC",
            "nandad": "NDC",
            "ndc": "NDC",
        }

    @classmethod
    def _airport_aliases_by_length(cls) -> list[tuple[str, str]]:
        return sorted(
            cls._airport_aliases().items(),
            key=lambda item: len(item[0]),
            reverse=True,
        )

    @staticmethod
    def _airline_aliases() -> dict[str, str]:
        return {
            "aer lingus": "EI",
            "air canada": "AC",
            "air france": "AF",
            "air india": "AI",
            "alaska": "AS",
            "alaska airlines": "AS",
            "american": "AA",
            "american airlines": "AA",
            "ana": "NH",
            "british airways": "BA",
            "delta": "DL",
            "delta air lines": "DL",
            "easyjet": "U2",
            "emirates": "EK",
            "etihad": "EY",
            "flynas": "XY",
            "flynass": "XY",
            "frontier": "F9",
            "indigo": "6E",
            "jet blue": "B6",
            "jetblue": "B6",
            "klm": "KL",
            "lufthansa": "LH",
            "qantas": "QF",
            "qatar": "QR",
            "qatar airways": "QR",
            "ryanair": "FR",
            "southwest": "WN",
            "spirit": "NK",
            "swiss": "LX",
            "turkish": "TK",
            "turkish airlines": "TK",
            "united": "UA",
            "united airlines": "UA",
            "vueling": "VY",
            "wizz": "W6",
            "wizz air": "W6",
        }

    @classmethod
    def _airline_aliases_by_length(cls) -> list[tuple[str, str]]:
        aliases = cls._airline_aliases()
        aliases.update({iata.casefold(): iata for iata in set(aliases.values())})
        return sorted(
            aliases.items(),
            key=lambda item: len(item[0]),
            reverse=True,
        )

    @classmethod
    def _iata_for_airline_token(cls, token: str) -> str | None:
        normalized = token.strip().upper()
        if re.fullmatch(r"[A-Z0-9]{2}", normalized):
            return normalized

        if re.fullmatch(r"[A-Z]{3}", normalized):
            return cls._iata_by_icao().get(normalized)

        return None

    @classmethod
    def _iata_by_icao(cls) -> dict[str, str]:
        if cls._iata_by_icao_cache is not None:
            return cls._iata_by_icao_cache

        try:
            airline_map_path = Path(settings.AIRLINE_MAP_JSON)
            if not airline_map_path.is_absolute():
                airline_map_path = Path.cwd() / airline_map_path

            with airline_map_path.open("r") as file:
                iata_to_icao = json.load(file)

            cls._iata_by_icao_cache = {
                str(icao).strip().upper(): str(iata).strip().upper()
                for iata, icao in iata_to_icao.items()
                if str(iata).strip() and str(icao).strip()
            }
            cls._iata_by_icao_cache.update(
                {
                    "DLH": "LH",
                    "GLO": "G3",
                    "SIA": "SQ",
                }
            )
        except Exception:
            logger.exception("Unable to load airline IATA/ICAO map")
            cls._iata_by_icao_cache = {}

        return cls._iata_by_icao_cache
