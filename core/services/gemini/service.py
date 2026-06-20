import asyncio
import logging
import re
from datetime import timedelta
from datetime import datetime, timezone
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
            if field not in args or args[field] in (None, "")
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

        flight_match = re.search(
            r"(?<![A-Z0-9])([A-Z0-9]{2})\s*([0-9]{1,4}[A-Z]?)(?![A-Z0-9])",
            normalized.upper(),
        )
        if flight_match:
            return self._resolved_function_call(
                function_name="extract_flight_info",
                args={
                    "airline_iata": flight_match.group(1),
                    "flight_number": flight_match.group(2),
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
    def _route_from_aliases(cls, lowered_query: str) -> tuple[str, str] | None:
        normalized = cls._normalized_route_text(lowered_query)
        for departure_name, departure_iata in cls._airport_aliases().items():
            if departure_name not in normalized:
                continue

            for arrival_name, arrival_iata in cls._airport_aliases().items():
                if departure_iata == arrival_iata or arrival_name not in normalized:
                    continue

                if cls._ordered_route_match(
                    normalized,
                    departure_name=departure_name,
                    arrival_name=arrival_name,
                ):
                    return departure_iata, arrival_iata

        return None

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
            "lhr": "LHR",
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
        }
