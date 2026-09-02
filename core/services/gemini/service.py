import asyncio
import difflib
import json
import logging
import re
import unicodedata
from datetime import timedelta
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from google import genai
from google.genai.types import GenerateContentConfig, GenerateContentResponse
from pydantic import BaseModel

from ...config import settings
from ...models.flight import SearchRecoveryRead, SearchSuggestionRead
from .config import REQUIRED_FIELDS, email_config, query_config

logger = logging.getLogger(__name__)


class FunctionCallResult(BaseModel):
    function_name: str
    args: dict[str, Any]


class ResolvedFunctionCall(FunctionCallResult):
    handler: Callable[..., Any]


class GeminiService:
    _iata_by_icao_cache: dict[str, str] | None = None
    _known_iata_airlines_cache: set[str] | None = None

    _flight_code_corrections = {
        # Confusions observed in real Sofly searches. Suggestions are shown to
        # the user and never silently substituted. Only keep corrections that
        # were confirmed by a successful follow-up search.
        "EL": "EK",
    }

    _country_airports = {
        "bangladesh": ("DAC", "CGP", "ZYL"),
        "canada": ("YYZ", "YVR", "YUL"),
        "cyprus": ("LCA", "PFO"),
        "mexico": ("MEX", "CUN", "GDL"),
        "turkey": ("IST", "SAW", "AYT"),
        "germany": ("FRA", "MUC", "BER", "DUS"),
        "georgia": ("TBS", "BUS", "KUT"),
        "indonesia": ("CGK", "DPS", "SUB"),
        "kosovo": ("PRN",),
        "lebanon": ("BEY",),
        "saudi_arabia": ("RUH", "JED", "DMM"),
        "switzerland": ("ZRH", "GVA", "BSL"),
        "venezuela": ("CCS", "MAR", "VLN"),
    }

    _country_aliases = {
        "bangladesh": {
            "bangladesh", "বাংলাদেশ", "بنغلاديش", "bangladés",
        },
        "canada": {
            "canada", "canadá", "canadà", "kanada", "كندا",
        },
        "cyprus": {
            "cyprus", "cipro", "chypre", "zypern", "kıbrıs", "قبرص",
        },
        "mexico": {
            "mexico", "méxico", "مكسيكو", "المكسيك",
        },
        "turkey": {
            "turkey", "turkiye", "türkiye", "turquía", "turquia",
            "turquie", "turchia", "türkei", "تركيا",
        },
        "germany": {
            "germany", "deutschland", "alemania", "allemagne", "germania",
            "alemanha", "ألمانيا", "المانيا",
        },
        "georgia": {
            "georgia", "géorgie", "georgien", "georgia country",
            "جورجيا", "საქართველო",
        },
        "indonesia": {
            "indonesia", "indonésie", "indonesie", "indonesien",
            "indonésia", "إندونيسيا", "اندونيسيا",
        },
        "kosovo": {
            "kosovo", "kosova", "كوسوفو",
        },
        "lebanon": {
            "lebanon", "liban", "libano", "líbano", "لبنان",
        },
        "saudi_arabia": {
            "saudi arabia", "saudi", "arabie saoudite", "saudi-arabien",
            "arabia saudita", "arábia saudita", "السعودية",
            "المملكة العربية السعودية",
        },
        "switzerland": {
            "switzerland", "suisse", "schweiz", "svizzera", "suiza",
            "suíça", "سويسرا",
        },
        "venezuela": {
            "venezuela", "venezüela", "فنزويلا",
        },
    }

    _today_words = {
        "today", "now", "hoy", "ahora", "aujourd'hui", "aujourd’hui",
        "aujourdhui",
        "maintenant", "heute", "jetzt", "oggi", "adesso", "ora", "hoje",
        "agora", "bugün", "şimdi", "اليوم", "الآن", "الان",
    }
    _tomorrow_words = {
        "tomorrow", "mañana", "demain", "morgen", "domani", "amanhã",
        "yarın", "غد", "غدًا",
    }
    _yesterday_words = {
        "yesterday", "ayer", "hier", "gestern", "ieri", "ontem", "dün",
        "أمس",
    }

    _aircraft_type_aliases = {
        "fa7x", "falcon 7x", "dassault falcon 7x",
    }
    _aircraft_type_pattern = re.compile(
        r"^(?:boeing|airbus|embraer|bombardier|cessna|dassault|gulfstream|"
        r"beechcraft|atr)\s+[a-z0-9][a-z0-9 .+-]{1,30}$",
        re.IGNORECASE,
    )

    _explicit_date_words = {
        "today", "now", "tomorrow", "yesterday",
        "hoy", "mañana", "ayer",
        "ahora", "aujourd'hui", "aujourd’hui", "aujourdhui", "maintenant", "demain", "hier",
        "heute", "jetzt", "morgen", "gestern",
        "oggi", "adesso", "ora", "domani", "ieri",
        "hoje", "agora", "amanhã", "ontem",
        "bugün", "şimdi", "yarın", "dün",
        "اليوم", "الآن", "الان", "غد", "غدًا", "أمس",
        "january", "jan", "enero", "ene", "janvier", "janv", "januar",
        "gennaio", "janeiro", "يناير",
        "february", "feb", "febrero", "février", "févr", "fevrier", "fevr",
        "februar", "febbraio", "fevereiro", "fev", "فبراير",
        "march", "mar", "marzo", "mars", "märz", "marz", "março", "marco",
        "مارس", "april", "apr", "abril", "abr", "avril", "avr", "aprile",
        "أبريل", "ابريل", "may", "mayo", "mai", "maggio", "maio", "mag",
        "مايو", "june", "jun", "junio", "juin", "juni", "giugno", "giu",
        "junho", "يونيو", "july", "jul", "julio", "juillet", "juli", "juil",
        "luglio", "lug", "julho", "يوليو", "august", "aug", "agosto", "août",
        "aout", "ago", "أغسطس", "اغسطس", "september", "sep", "septiembre",
        "septembre", "sept", "settembre", "setembro", "set", "سبتمبر",
        "october", "oct", "octubre", "octobre", "oktober", "okt", "ottobre",
        "ott", "outubro", "out", "أكتوبر", "اكتوبر", "november", "nov",
        "noviembre", "novembre", "نوفمبر", "december", "dec", "diciembre",
        "décembre", "déc", "decembre", "dezember", "dez", "dicembre", "dic",
        "dezembro", "ديسمبر",
    }

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
                        logger.warning("Gemini function call is missing a name or arguments")
                        continue

                    return FunctionCallResult(
                        function_name=fc.name,
                        args=dict(fc.args),
                    )
        logger.warning("Unable to extract a function call from Gemini response")
        return None

    async def get_function_call(
        self, query: str, email: bool = False, language: str | None = None
    ) -> ResolvedFunctionCall | None:
        query = self.normalize_query(query)
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
                        "Gemini produced an invalid response email_mode=%s attempt=%s",
                        email,
                        attempt,
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
                        "Gemini produced an invalid function call function=%s email_mode=%s attempt=%s",
                        extracted_function_call.function_name,
                        email,
                        attempt,
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
                    "Error retrieving Gemini function call email_mode=%s attempt=%s",
                    email,
                    attempt,
                )

        logger.warning(
            "Gemini unable to extract a function call email_mode=%s attempts=%s",
            email,
            attempts,
        )
        return None

    @staticmethod
    def normalize_query(query: str) -> str:
        normalized = unicodedata.normalize("NFKC", query)
        normalized = re.sub(r"[\u200e\u200f\u202a-\u202e\u2066-\u2069]", "", normalized)
        normalized = normalized.replace("–", "-").replace("—", "-")
        return re.sub(r"\s+", " ", normalized).strip()

    @classmethod
    def preflight_recovery(
        cls,
        query: str,
        language: str | None = None,
    ) -> SearchRecoveryRead | None:
        normalized = cls.normalize_query(query)

        if cls.aircraft_type_from_query(normalized):
            return SearchRecoveryRead(
                reason="unsupported_aircraft_type",
                detected_query_type="aircraft_type",
                normalized_query=normalized.upper(),
                suggestions=[
                    SearchSuggestionRead(
                        label="Search by flight number",
                        query="",
                        kind="search_help",
                    ),
                ],
            )

        number_without_airline = cls.flight_number_without_airline(normalized)
        if number_without_airline:
            return SearchRecoveryRead(
                reason="missing_airline",
                detected_query_type="flight_number",
                normalized_query=number_without_airline,
                suggestions=[
                    SearchSuggestionRead(
                        label="Search by flight number",
                        query="",
                        kind="search_help",
                    ),
                ],
            )

        registration = cls.aircraft_registration_from_query(normalized)

        if registration:
            return SearchRecoveryRead(
                reason="aircraft_registration",
                detected_query_type="aircraft_registration",
                normalized_query=registration,
                suggestions=[
                    SearchSuggestionRead(
                        label="Search by flight number",
                        query="",
                        kind="search_help",
                    ),
                ],
            )

        # When two known locations form a clear route, let the deterministic
        # parser execute it instead of intercepting a country word as an
        # ambiguous broad-location search.
        if cls._route_from_aliases(normalized.casefold()):
            return None

        # Queries such as "Flair Canada a Cancun Mexico" contain broad country
        # names, but the airline and directional airport make the requested
        # search actionable. Let the deterministic parser call the provider
        # instead of replacing it with generic country-route suggestions.
        if cls._airline_airport_query(normalized.casefold()):
            return None

        countries = cls._countries_in_query(normalized)
        if len(countries) >= 2:
            departure, arrival = countries[0], countries[1]
            suggestions = cls._route_suggestions(departure, arrival)
            return SearchRecoveryRead(
                reason="choose_airport_route",
                detected_query_type="country_route",
                normalized_query=normalized,
                suggestions=suggestions,
            )

        if len(countries) == 1:
            country_airport_route = cls._country_and_airport_route(
                normalized,
                countries[0],
            )
            if country_airport_route:
                country, airport_iata, country_first = country_airport_route
                suggestions = cls._country_airport_route_suggestions(
                    country,
                    airport_iata,
                    country_first=country_first,
                )
                return SearchRecoveryRead(
                    reason="choose_airport_route",
                    detected_query_type="country_route",
                    normalized_query=normalized,
                    suggestions=suggestions,
                )

        if len(countries) == 1 and cls._is_broad_location_query(normalized, countries[0]):
            country = countries[0]
            suggestions = [
                SearchSuggestionRead(
                    label=f"{iata} departures",
                    query=f"Departures from {iata} Today",
                    kind="airport",
                )
                for iata in cls._country_airports[country]
            ]
            return SearchRecoveryRead(
                reason="choose_airport",
                detected_query_type="broad_location",
                normalized_query=normalized,
                suggestions=suggestions,
            )

        if "rockport" in normalized.casefold():
            return SearchRecoveryRead(
                reason="nearby_commercial_airport",
                detected_query_type="city",
                normalized_query=normalized,
                suggestions=[
                    SearchSuggestionRead(
                        label="CRP departures",
                        query="Departures from CRP Today",
                        kind="airport",
                    ),
                    SearchSuggestionRead(
                        label="CRP arrivals",
                        query="Arrivals at CRP Today",
                        kind="airport",
                    ),
                ],
            )

        return None

    @classmethod
    def aircraft_type_from_query(cls, query: str) -> str | None:
        normalized = cls._fold_text(cls.normalize_query(query)).strip()
        if normalized in cls._aircraft_type_aliases:
            return normalized
        return normalized if cls._aircraft_type_pattern.fullmatch(normalized) else None

    @classmethod
    def flight_number_without_airline(cls, query: str) -> str | None:
        normalized = cls.normalize_query(query)
        match = re.fullmatch(r"(?P<number>[0-9]{1,4})(?:\s+(?P<rest>.+))?", normalized)
        if not match:
            return None

        rest = (match.group("rest") or "").strip()
        if rest and not cls.query_has_explicit_date(rest):
            return None
        return match.group("number")

    @classmethod
    def aircraft_registration_from_query(cls, query: str) -> str | None:
        normalized = cls.normalize_query(query).upper().strip()
        normalized = re.sub(
            r"^(?:AIRCRAFT\s+)?(?:REGISTRATION|TAIL(?:\s+NUMBER)?)\s*[:#-]?\s*",
            "",
            normalized,
        ).strip()
        compact = re.sub(r"\s+", "", normalized)

        if re.fullmatch(r"N[0-9]{1,5}[A-Z]{0,2}", compact):
            return compact

        # Common non-US forms retain a country prefix and hyphen, for example
        # G-STBA, D-ABCD, C-FABC, or VH-ABC.
        if re.fullmatch(r"[A-Z]{1,2}-[A-Z0-9]{3,5}", compact):
            return compact

        return None

    @staticmethod
    def registration_recovery(
        registration: str,
        failure_reason: str,
    ) -> SearchRecoveryRead:
        reason_by_failure = {
            "registration_not_live": "aircraft_registration_not_live",
            "registration_live_unresolved": "aircraft_registration_live_unresolved",
            "provider_unavailable": "aircraft_registration_provider_unavailable",
            "provider_rate_limited": "aircraft_registration_provider_rate_limited",
        }
        return SearchRecoveryRead(
            reason=reason_by_failure.get(
                failure_reason,
                "aircraft_registration",
            ),
            detected_query_type="aircraft_registration",
            normalized_query=registration,
            suggestions=[
                SearchSuggestionRead(
                    label="Search by flight number",
                    query="",
                    kind="search_help",
                ),
            ],
        )

    @classmethod
    def recovery_for_empty_result(
        cls,
        query: str,
        resolved_call: ResolvedFunctionCall | None,
    ) -> SearchRecoveryRead:
        normalized = cls.normalize_query(query)
        if resolved_call and resolved_call.function_name == "extract_flight_info":
            airline = str(resolved_call.args.get("airline_iata", "")).upper()
            number = str(resolved_call.args.get("flight_number", "")).upper()
            corrected_airline = cls._flight_code_corrections.get(airline)
            if corrected_airline:
                corrected_query = f"{corrected_airline}{number}"
                resolved_date = str(
                    resolved_call.args.get("departure_date", "")
                ).strip()
                if resolved_date and cls.query_has_explicit_date(normalized):
                    corrected_query = f"{corrected_query} {resolved_date}"
                return SearchRecoveryRead(
                    reason="possible_flight_number_typo",
                    detected_query_type="flight_number",
                    normalized_query=f"{airline}{number}",
                    suggestions=[
                        SearchSuggestionRead(
                            label=f"{corrected_airline} {number}",
                            query=corrected_query,
                            kind="corrected_flight_number",
                        )
                    ],
                )

            route_suggestion = cls._embedded_airport_route_suggestion(normalized)
            if not cls.query_has_explicit_date(normalized):
                suggestions = [
                    SearchSuggestionRead(
                        label="Add a date",
                        query="",
                        kind="add_date",
                    )
                ]
                if route_suggestion:
                    suggestions.append(route_suggestion)
                return SearchRecoveryRead(
                    reason="missing_date",
                    detected_query_type="flight_number",
                    normalized_query=f"{airline}{number}",
                    suggestions=suggestions,
                )

            canonical_query = f"{airline}{number}" if airline and number else normalized
            return SearchRecoveryRead(
                reason="flight_not_found",
                detected_query_type="flight_number",
                normalized_query=canonical_query,
                suggestions=[route_suggestion] if route_suggestion else [],
            )

        if re.fullmatch(
            r"[A-Z]{2,3}[\s-]*[0-9]{1,4}[A-Z]?",
            normalized.upper(),
        ):
            return SearchRecoveryRead(
                reason="unrecognized_flight_number",
                detected_query_type="flight_number",
                normalized_query=normalized.upper().replace(" ", ""),
                suggestions=[],
            )

        if resolved_call:
            reason_by_function = {
                "extract_flight_info_via_airport": "route_not_found",
                "extract_flight_info_via_airport_single_derection": "airport_no_flights",
                "extract_airline_live_flights": "airline_no_flights",
                "extract_random_flight": "random_flight_unavailable",
            }
            return SearchRecoveryRead(
                reason=reason_by_function.get(
                    resolved_call.function_name,
                    "provider_no_match",
                ),
                detected_query_type=cls.query_type_for_call(resolved_call),
                normalized_query=normalized,
                suggestions=[],
            )

        return SearchRecoveryRead(
            reason="no_results",
            detected_query_type="natural_language",
            normalized_query=normalized,
            suggestions=[],
        )

    @classmethod
    def _embedded_airport_route_suggestion(
        cls,
        query: str,
    ) -> SearchSuggestionRead | None:
        known_airports = set(cls._airport_aliases().values())
        for compact_pair in re.findall(r"(?<![A-Z])([A-Z]{6})(?![A-Z])", query.upper()):
            departure, arrival = compact_pair[:3], compact_pair[3:]
            if departure not in known_airports or arrival not in known_airports:
                continue
            return SearchSuggestionRead(
                label=f"{departure} → {arrival}",
                query=f"{departure} to {arrival} Today",
                kind="airport_route",
            )
        return None

    @classmethod
    def query_has_explicit_date(cls, query: str) -> bool:
        normalized = cls.normalize_query(query).casefold()
        for word in cls._explicit_date_words:
            if re.search(rf"(?<!\w){re.escape(word)}(?!\w)", normalized):
                return True

        return bool(
            re.search(
                r"(?<!\d)(?:(?:19|20)\d{2}[\s./-]+\d{1,2}[\s./-]+\d{1,2}|"
                r"\d{1,2}[\s./-]+\d{1,2}(?:[\s,./-]+(?:19|20)?\d{2})?)(?!\d)",
                normalized,
            )
        )

    @classmethod
    def upcoming_search_days(cls, query: str) -> int:
        """Return a small bounded fallback window for date-ambiguous searches."""
        normalized = cls.normalize_query(query).casefold()
        if cls._contains_date_word(normalized, cls._yesterday_words):
            return 0
        if cls._contains_date_word(
            normalized,
            cls._today_words | cls._tomorrow_words,
        ):
            # This one-day fallback bridges users whose local calendar date is
            # already ahead of UTC without guessing their timezone.
            return 1
        if cls.query_has_explicit_date(normalized):
            return 0
        return 3

    @staticmethod
    def _contains_date_word(query: str, words: set[str]) -> bool:
        return any(
            re.search(rf"(?<!\w){re.escape(word)}(?!\w)", query)
            for word in words
        )

    @staticmethod
    def query_type_for_call(resolved_call: ResolvedFunctionCall | None) -> str:
        if not resolved_call:
            return "unknown"
        if (
            resolved_call.function_name
            == "extract_flight_info_via_airport_single_derection"
            and resolved_call.args.get("airline_iata")
        ):
            return "airline_airport"
        return {
            "extract_flight_info": "flight_number",
            "extract_flight_info_via_airport": "airport_route",
            "extract_flight_info_via_airport_single_derection": "airport",
            "extract_airline_live_flights": "airline",
            "extract_random_flight": "random",
        }.get(resolved_call.function_name, "natural_language")

    @staticmethod
    def failure_reason_for_recovery(recovery: SearchRecoveryRead) -> str:
        return {
            "possible_flight_number_typo": "unrecognized_flight_number",
            "unrecognized_flight_number": "unrecognized_flight_number",
            "missing_date": "missing_date",
            "missing_airline": "incomplete_query",
            "choose_airport_route": "ambiguous_location",
            "choose_airport": "ambiguous_location",
            "nearby_commercial_airport": "unsupported_location",
            "unsupported_aircraft_type": "unsupported_aircraft_type",
            "no_results": "unsupported_or_incomplete_query",
            "flight_not_found": "provider_no_match",
            "route_not_found": "provider_no_match",
            "airport_no_flights": "provider_no_match",
            "airline_no_flights": "provider_no_match",
            "random_flight_unavailable": "provider_no_match",
            "provider_no_match": "provider_no_match",
            "landed_only": "landed_only",
            "results_filtered_out": "results_filtered_out",
            "no_upcoming_results": "results_filtered_out",
            "provider_unavailable": "provider_unavailable",
            "provider_rate_limited": "provider_rate_limited",
            "aircraft_registration_not_live": "registration_not_live",
            "aircraft_registration_live_unresolved": "registration_live_unresolved",
            "aircraft_registration_provider_unavailable": "provider_unavailable",
            "aircraft_registration_provider_rate_limited": "provider_rate_limited",
        }.get(recovery.reason, "unsupported_or_incomplete_query")

    @classmethod
    def _countries_in_query(cls, query: str) -> list[str]:
        lowered = query.casefold()
        matches: list[tuple[int, str]] = []
        for country, aliases in cls._country_aliases.items():
            indexes = [lowered.find(alias.casefold()) for alias in aliases]
            valid_indexes = [index for index in indexes if index >= 0]
            if valid_indexes:
                matches.append((min(valid_indexes), country))
        return [country for _, country in sorted(matches)]

    @classmethod
    def _route_suggestions(
        cls,
        departure_country: str,
        arrival_country: str,
    ) -> list[SearchSuggestionRead]:
        departure_airports = cls._country_airports[departure_country]
        arrival_airports = cls._country_airports[arrival_country]
        pairs = [(departure_airports[0], arrival_airports[0])]
        if len(departure_airports) > 1:
            pairs.append((departure_airports[1], arrival_airports[0]))
        if len(arrival_airports) > 1:
            pairs.append((departure_airports[0], arrival_airports[1]))
        return [
            SearchSuggestionRead(
                label=f"{departure} → {arrival}",
                query=f"{departure} to {arrival} Today",
                kind="airport_route",
            )
            for departure, arrival in pairs
        ]

    @classmethod
    def _country_and_airport_route(
        cls,
        query: str,
        country: str,
    ) -> tuple[str, str, bool] | None:
        normalized = cls._normalized_route_text(query.casefold())
        country_aliases = cls._country_aliases[country]
        country_positions = [
            index
            for alias in country_aliases
            if (index := cls._alias_index(normalized, cls._fold_text(alias))) >= 0
        ]
        if not country_positions:
            return None
        country_index = min(country_positions)

        for airport_alias, airport_iata in cls._airport_aliases_by_length():
            airport_index = cls._alias_index(
                normalized,
                cls._fold_text(airport_alias),
            )
            if airport_index < 0:
                continue
            return country, airport_iata, country_index < airport_index
        return None

    @classmethod
    def _country_airport_route_suggestions(
        cls,
        country: str,
        airport_iata: str,
        *,
        country_first: bool,
    ) -> list[SearchSuggestionRead]:
        suggestions: list[SearchSuggestionRead] = []
        for country_airport in cls._country_airports[country][:3]:
            departure, arrival = (
                (country_airport, airport_iata)
                if country_first
                else (airport_iata, country_airport)
            )
            suggestions.append(
                SearchSuggestionRead(
                    label=f"{departure} → {arrival}",
                    query=f"{departure} to {arrival} Today",
                    kind="airport_route",
                )
            )
        return suggestions

    @classmethod
    def _is_broad_location_query(cls, query: str, country: str) -> bool:
        lowered = query.casefold().strip()
        aliases = cls._country_aliases[country]
        stripped = re.sub(
            r"\b(to|from|in|flights?|departures?|arrivals?|today|tomorrow)\b",
            "",
            lowered,
        ).strip()
        return any(stripped == alias.casefold() for alias in aliases)

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

        airline_airport = self._airline_airport_query(lowered)
        if airline_airport:
            airline_iata, airport_iata, direction = airline_airport
            return self._resolved_function_call(
                function_name="extract_flight_info_via_airport_single_derection",
                args={
                    "airport_iata": airport_iata,
                    "direction": direction,
                    "departure_date": self._date_from_query(lowered),
                    "airline_iata": airline_iata,
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

    @classmethod
    def _date_from_query(cls, lowered_query: str) -> str:
        lowered_query = lowered_query.casefold()
        today = datetime.now(timezone.utc).date()
        month_aliases = {
            "january": 1, "jan": 1, "enero": 1, "ene": 1,
            "janvier": 1, "janv": 1,
            "januar": 1, "gennaio": 1, "janeiro": 1, "يناير": 1,
            "february": 2, "feb": 2, "febrero": 2, "février": 2,
            "févr": 2, "fevrier": 2, "fevr": 2, "februar": 2,
            "febbraio": 2, "fevereiro": 2, "fev": 2,
            "فبراير": 2,
            "march": 3, "mar": 3, "marzo": 3, "mars": 3, "märz": 3,
            "marz": 3, "março": 3, "marco": 3, "مارس": 3,
            "april": 4, "apr": 4, "abril": 4, "abr": 4,
            "avril": 4, "avr": 4, "aprile": 4,
            "أبريل": 4, "ابريل": 4,
            "may": 5, "mayo": 5, "mai": 5, "maggio": 5, "maio": 5,
            "mag": 5,
            "مايو": 5,
            "june": 6, "jun": 6, "junio": 6, "juin": 6, "juni": 6,
            "giugno": 6, "giu": 6, "junho": 6, "يونيو": 6,
            "july": 7, "jul": 7, "julio": 7, "juillet": 7, "juli": 7,
            "juil": 7, "luglio": 7, "lug": 7, "julho": 7, "يوليو": 7,
            "august": 8, "aug": 8, "agosto": 8, "août": 8, "aout": 8,
            "ago": 8,
            "أغسطس": 8, "اغسطس": 8,
            "september": 9, "sep": 9, "septiembre": 9,
            "septembre": 9, "sept": 9, "settembre": 9, "setembro": 9,
            "set": 9, "سبتمبر": 9,
            "october": 10, "oct": 10, "octubre": 10, "octobre": 10,
            "oktober": 10, "okt": 10, "ottobre": 10, "ott": 10,
            "outubro": 10, "out": 10,
            "أكتوبر": 10, "اكتوبر": 10,
            "november": 11, "nov": 11, "noviembre": 11,
            "novembre": 11, "نوفمبر": 11,
            "december": 12, "dec": 12, "diciembre": 12,
            "décembre": 12, "déc": 12, "decembre": 12,
            "dezember": 12, "dez": 12, "dicembre": 12, "dic": 12,
            "dezembro": 12, "ديسمبر": 12,
        }

        def resolved_year(raw_year: str | None) -> int:
            if not raw_year:
                return today.year
            value = int(raw_year)
            return 2000 + value if value < 100 else value

        iso_match = re.search(
            r"(?<!\d)((?:19|20)\d{2})[-/.](\d{1,2})[-/.](\d{1,2})(?!\d)",
            lowered_query,
        )
        if iso_match:
            try:
                return datetime(
                    int(iso_match.group(1)),
                    int(iso_match.group(2)),
                    int(iso_match.group(3)),
                    tzinfo=timezone.utc,
                ).date().isoformat()
            except ValueError:
                pass

        month_pattern = "|".join(
            re.escape(alias)
            for alias in sorted(month_aliases, key=len, reverse=True)
        )
        named_date_patterns = (
            rf"(?<!\w)(?P<month>{month_pattern})\s+"
            r"(?P<day>\d{1,2})(?:st|nd|rd|th)?"
            r"(?:,?\s+(?P<year>\d{4}|\d{2}))?(?!\w)",
            r"(?<!\w)(?P<day>\d{1,2})(?:st|nd|rd|th)?\s+(?:de\s+)?"
            rf"(?P<month>{month_pattern})"
            r"(?:\s+(?P<year>\d{4}|\d{2}))?(?!\w)",
        )
        for pattern in named_date_patterns:
            match = re.search(pattern, lowered_query)
            if not match:
                continue

            year = resolved_year(match.group("year"))
            month = month_aliases[match.group("month")]
            day = int(match.group("day"))
            try:
                parsed_date = datetime(
                    year,
                    month,
                    day,
                    tzinfo=timezone.utc,
                ).date()
            except ValueError:
                continue

            return parsed_date.isoformat()

        numeric_date = re.search(
            r"(?<!\d)(?P<first>\d{1,2})[\s./-]+(?P<second>\d{1,2})"
            r"(?:[\s,./-]+(?P<year>\d{4}|\d{2}))?(?!\d)",
            lowered_query,
        )
        if numeric_date:
            first = int(numeric_date.group("first"))
            second = int(numeric_date.group("second"))

            # Prefer day-first for ambiguous forms because that is the common
            # format across Sofly's supported locales. Still accept an
            # unambiguous US month-first form such as 08/17.
            if second > 12 and first <= 12:
                month, day = first, second
            else:
                day, month = first, second

            try:
                return datetime(
                    resolved_year(numeric_date.group("year")),
                    month,
                    day,
                    tzinfo=timezone.utc,
                ).date().isoformat()
            except ValueError:
                pass

        if cls._contains_date_word(lowered_query, cls._tomorrow_words):
            return (today + timedelta(days=1)).isoformat()

        if cls._contains_date_word(lowered_query, cls._yesterday_words):
            return (today - timedelta(days=1)).isoformat()

        if cls._contains_date_word(lowered_query, cls._today_words):
            return today.isoformat()

        return today.isoformat()

    @classmethod
    def _flight_from_query(cls, query: str) -> tuple[str, str] | None:
        lowered = query.casefold()
        for alias, airline_iata in cls._airline_aliases_by_length():
            match = re.search(
                rf"(?<![a-z0-9]){re.escape(alias)}"
                r"(?:\s+flight)?[\s./-]*([0-9]{1,4}[a-z]?)(?![a-z0-9])",
                lowered,
            )
            if match:
                return airline_iata, match.group(1).upper()

        upper = query.upper()
        for pattern in (
            r"(?<![A-Z0-9])([A-Z]{3})[\s./-]*([0-9]{1,4}[A-Z]?)(?![A-Z0-9])",
            r"(?<![A-Z0-9])([A-Z0-9]{2})[\s./-]*([0-9]{1,4}[A-Z]?)(?![A-Z0-9])",
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

        candidate = cls._single_location_candidate(normalized)
        if len(candidate) >= 5:
            aliases = [
                alias
                for alias, _ in cls._airport_aliases_by_length()
                if len(alias) >= 5 and not re.fullmatch(r"[a-z]{3}", alias)
            ]
            close_matches = difflib.get_close_matches(
                candidate,
                aliases,
                n=1,
                cutoff=0.84,
            )
            if close_matches:
                return (
                    cls._airport_aliases()[close_matches[0]],
                    cls._single_airport_direction(normalized),
                )

        return None

    @classmethod
    def _airline_airport_query(
        cls,
        lowered_query: str,
    ) -> tuple[str, str, str] | None:
        normalized = cls._normalized_route_text(lowered_query).strip()

        airline_iata = None
        for airline_alias, candidate_iata in cls._airline_aliases_by_length():
            if cls._alias_index(normalized, cls._fold_text(airline_alias)) >= 0:
                airline_iata = candidate_iata
                break
        if not airline_iata:
            return None

        for airport_alias, airport_iata in cls._airport_aliases_by_length():
            airport_index = cls._alias_index(
                normalized,
                cls._fold_text(airport_alias),
            )
            if airport_index < 0:
                continue

            before_airport = normalized[:airport_index].rstrip()
            if re.search(
                r"(?:\bto|\binto|\ba|\bà|\bpara|\bnach|\bvers)\s*$",
                before_airport,
            ):
                return airline_iata, airport_iata, "Arrival"
            if re.search(r"\bfrom\s*$", before_airport):
                return airline_iata, airport_iata, "Departure"

        return None

    @staticmethod
    def _alias_index(value: str, alias: str) -> int:
        match = re.search(
            rf"(?<!\w){re.escape(alias)}(?!\w)",
            value,
        )
        return match.start() if match else -1

    @classmethod
    def _single_location_candidate(cls, normalized_query: str) -> str:
        candidate = normalized_query.strip()
        removable_words = (
            cls._today_words
            | cls._tomorrow_words
            | cls._yesterday_words
            | {
                "to", "from", "in", "into", "flights", "flight",
                "departures", "departure", "arrivals", "arrival",
            }
        )
        for word in sorted(removable_words, key=len, reverse=True):
            candidate = re.sub(
                rf"(?<!\w){re.escape(cls._fold_text(word))}(?!\w)",
                " ",
                candidate,
            )
        return re.sub(r"\s+", " ", candidate).strip()

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

    @classmethod
    def _normalized_route_text(cls, value: str) -> str:
        normalized = re.sub(
            r"\s+",
            " ",
            cls._fold_text(value).replace("→", " to ").replace("-", " to "),
        ).strip()
        return f" {normalized} "

    @staticmethod
    def _fold_text(value: str) -> str:
        decomposed = unicodedata.normalize("NFKD", value.casefold())
        return "".join(
            character
            for character in decomposed
            if not unicodedata.combining(character)
        )

    @classmethod
    def _ordered_route_match(
        cls,
        normalized_query: str,
        departure_name: str,
        arrival_name: str,
    ) -> bool:
        departure_index = cls._alias_index(normalized_query, departure_name)
        arrival_index = cls._alias_index(normalized_query, arrival_name)
        if departure_index < 0 or arrival_index < 0 or departure_index >= arrival_index:
            return False

        between = normalized_query[
            departure_index + len(departure_name):arrival_index
        ]
        route_words = (" to ", " from ", " a ", " à ", " para ", " nach ", " vers ")
        return not between.strip() or any(word in between for word in route_words)

    @staticmethod
    def _airport_aliases() -> dict[str, str]:
        return {
            "amritsar": "ATQ",
            "atq": "ATQ",
            "kannur": "CNN",
            "cnn": "CNN",
            "abha": "ABH",
            "abh": "ABH",
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
            "اسطنبول": "IST",
            "الاسطنبول": "IST",
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
            "lisbon": "LIS",
            "lisbonne": "LIS",
            "lisboa": "LIS",
            "lisbona": "LIS",
            "libonne": "LIS",
            "lis": "LIS",
            "abu dhabi": "AUH",
            "abou dabi": "AUH",
            "auh": "AUH",
            "kuwait city": "KWI",
            "kuwait": "KWI",
            "koweit": "KWI",
            "kwi": "KWI",
            "riyadh": "RUH",
            "riyad": "RUH",
            "الرياض": "RUH",
            "ruh": "RUH",
            "jeddah": "JED",
            "jeddah airport": "JED",
            "جدة": "JED",
            "جده": "JED",
            "jed": "JED",
            "bucharest": "OTP",
            "bukarest": "OTP",
            "bucarest": "OTP",
            "bucuresti": "OTP",
            "bucurești": "OTP",
            "otp": "OTP",
            "memmingen": "FMM",
            "fmm": "FMM",
            "lyon": "LYS",
            "lys": "LYS",
            "dubrovnik": "DBV",
            "dbv": "DBV",
            "marbella": "AGP",
            "malaga": "AGP",
            "málaga": "AGP",
            "agp": "AGP",
            "oran": "ORN",
            "orn": "ORN",
            "kathmandu": "KTM",
            "ktm": "KTM",
            "bali": "DPS",
            "denpasar": "DPS",
            "dps": "DPS",
            "hong kong": "HKG",
            "hongkong": "HKG",
            "hkg": "HKG",
            "munich": "MUC",
            "münchen": "MUC",
            "muenchen": "MUC",
            "muc": "MUC",
            "milan": "MXP",
            "milano": "MXP",
            "mxp": "MXP",
            "nuremberg": "NUE",
            "nürnberg": "NUE",
            "nurnberg": "NUE",
            "nuriberg": "NUE",
            "nue": "NUE",
            "tripoli": "TIP",
            "طرابلس": "TIP",
            "tip": "TIP",
            "mitiga": "MJI",
            "معيتيقة": "MJI",
            "امعتيقه": "MJI",
            "امعايقه": "MJI",
            "mji": "MJI",
            "cairo": "CAI",
            "القاهرة": "CAI",
            "cai": "CAI",
            "cancun": "CUN",
            "cancún": "CUN",
            "cun": "CUN",
            "manila": "MNL",
            "مانيلا": "MNL",
            "mnl": "MNL",
            "bahrain": "BAH",
            "البحرين": "BAH",
            "bah": "BAH",
            "dusseldorf": "DUS",
            "düsseldorf": "DUS",
            "düsseldorfer": "DUS",
            "dus": "DUS",
            "toronto": "YYZ",
            "yyz": "YYZ",
            "yellowknife": "YZF",
            "yellownknife": "YZF",
            "yzf": "YZF",
            "halifax": "YHZ",
            "yhz": "YHZ",
            "washington dc": "IAD",
            "washington d.c.": "IAD",
            "iad": "IAD",
            "zurich": "ZRH",
            "zürich": "ZRH",
            "zrh": "ZRH",
            "skopje": "SKP",
            "skp": "SKP",
            "dortmund": "DTM",
            "dtm": "DTM",
            "basel": "BSL",
            "mlh": "BSL",
            "bsl": "BSL",
            "reno": "RNO",
            "rno": "RNO",
            "new orleans": "MSY",
            "msy": "MSY",
            "dhaka": "DAC",
            "dac": "DAC",
            "medina": "MED",
            "med": "MED",
            "nis": "INI",
            "niš": "INI",
            "ini": "INI",
            "nova lima": "CNF",
            "cnf": "CNF",
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
            "condor": "DE",
            "con": "DE",
            "corendon": "XC",
            "easyjet": "U2",
            "emirates": "EK",
            "etihad": "EY",
            "flynas": "XY",
            "flynass": "XY",
            "flyadeal": "F3",
            "frontier": "F9",
            "flair": "F8",
            "flair airlines": "F8",
            "indigo": "6E",
            "jet blue": "B6",
            "jetblue": "B6",
            "jazeera": "J9",
            "jazeera airways": "J9",
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
            "ibo": "I2",
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
        if (
            re.fullmatch(r"[A-Z0-9]{2}", normalized)
            and normalized in cls._known_iata_airlines()
        ):
            return normalized

        if re.fullmatch(r"[A-Z]{3}", normalized):
            return cls._iata_by_icao().get(normalized)

        return None

    @classmethod
    def _known_iata_airlines(cls) -> set[str]:
        if cls._known_iata_airlines_cache is None:
            cls._known_iata_airlines_cache = {
                iata.upper()
                for iata in cls._iata_by_icao().values()
                if iata
            }
            cls._known_iata_airlines_cache.update(cls._airline_aliases().values())
        return cls._known_iata_airlines_cache

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
