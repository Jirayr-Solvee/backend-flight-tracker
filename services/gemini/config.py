from typing import Any, Callable

from google.genai.types import (FunctionDeclaration, GenerateContentConfig,
                                Schema, Tool, Type)
from pydantic import BaseModel

from ..flight import AirportSearchDirection, FlightQueryHandler


class FunctionDefinition(BaseModel):
    required_fields: list[str]
    handler: Callable[..., Any]


REQUIRED_FIELDS: dict[str, FunctionDefinition] = {
    "extract_flight_info": FunctionDefinition(
        required_fields=["flight_number", "airline_iata", "departure_date"],
        handler=FlightQueryHandler.extract_flight_info,
    ),
    "extract_flight_from_email": FunctionDefinition(
        required_fields=["flight_number", "airline_iata", "departure_date"],
        handler=FlightQueryHandler.extract_flight_from_email,
    ),
    "extract_flight_info_via_airport": FunctionDefinition(
        required_fields=[
            "departure_airport_iata",
            "arrival_airport_iata",
            "departure_date",
        ],
        handler=FlightQueryHandler.extract_flight_info_via_airport,
    ),
    "extract_flight_info_via_airport_single_derection": FunctionDefinition(
        required_fields=[
            "airport_iata",
            "departure_date",
            "direction",
        ],
        handler=FlightQueryHandler.extract_flight_info_via_airport_single_derection,
    ),
}

# a single config to see where a flight is going into
# we need a single one that would extract flight going into or from only

extract_flight_from_email_function = FunctionDeclaration(
    name="extract_flight_from_email",
    description="Extract a flight number + airline IATA code + departure date from query.",
    parameters=Schema(
        type=Type.OBJECT,
        properties={
            "flight_number": Schema(
                type=Type.STRING,
                description="Numeric part of the flight number, e.g. '1237'.",
                nullable=False,
            ),
            "airline_iata": Schema(
                type=Type.STRING,
                description="Two-letter airline IATA code, e.g. 'U2'.",
                nullable=False,
            ),
            "departure_date": Schema(
                type=Type.STRING,
                description="Date of the flight in YYYY-MM-DD format.",
                nullable=False,
            ),
        },
        required=["flight_number", "airline_iata", "departure_date"],
    ),
)

email_tool = Tool(function_declarations=[extract_flight_from_email_function])
email_config = GenerateContentConfig(tools=[email_tool])

# Tool 1. extract spisific flight
extract_flight_function = FunctionDeclaration(
    name="extract_flight_info",
    description="Extract a flight number + airline IATA code + departure date from query.",
    parameters=Schema(
        type=Type.OBJECT,
        properties={
            "flight_number": Schema(
                type=Type.STRING,
                description="Numeric part of the flight number, e.g. '1237'.",
                nullable=False,
            ),
            "airline_iata": Schema(
                type=Type.STRING,
                description="Two-letter airline IATA code, e.g. 'U2'.",
                nullable=False,
            ),
            "departure_date": Schema(
                type=Type.STRING,
                description="Date of the flight in YYYY-MM-DD format.",
                nullable=False,
            ),
        },
        required=REQUIRED_FIELDS["extract_flight_info"].required_fields,
    ),
)

# Tool 2: extract airport routes
extract_airport_route_function = FunctionDeclaration(
    name="extract_flight_info_via_airport",
    description=(
        "Extract exactly three fields from the user query: "
        "`departure_airport_iata`, `arrival_airport_iata`, and `departure_date` (YYYY-MM-DD). "
        "If the user mentions a city instead of an airport, use the most relevant/busiest airport in that city. "
        "Always return valid IATA codes. "
        "Return only JSON — do not include extra text. "
        "Example: "
        '{"departure_airport_iata": "JFK", "arrival_airport_iata": "CDG", "departure_date": "2025-10-13"}'
        "If no date found in the query use Current date (UTC) provided at the start of the query "
        "Remmeber to use the most relevent airport if no actual airport was provided for departure or arrival, example if a city name was provided use the most relevent airport iata from that city"
    ),
    parameters=Schema(
        type=Type.OBJECT,
        properties={
            "departure_airport_iata": Schema(
                type=Type.STRING,
                description="IATA code of the departure airport.",
                nullable=False,
            ),
            "arrival_airport_iata": Schema(
                type=Type.STRING,
                description="IATA code of the arrival airport.",
                nullable=False,
            ),
            "departure_date": Schema(
                type=Type.STRING,
                description="Date of travel in YYYY-MM-DD format.",
                nullable=False,
            ),
        },
        required=REQUIRED_FIELDS["extract_flight_info_via_airport"].required_fields,
    ),
)

# Tool 3: extract airport routes ( single route )
extract_airport_route_single_derection_function = FunctionDeclaration(
    name="extract_flight_info_via_airport_single_derection",
    description=(
        "Use this function when the user asks for flights from or into a single airport "
        "without specifying a destination. "
        "Infer direction as follows: "
        "- 'from <airport>' → departure "
        "- 'to <airport>' or 'into <airport>' → arrival. "
        "If no date is mentioned, use the current UTC date. "
        "Return only JSON."
    ),
    parameters=Schema(
        type=Type.OBJECT,
        properties={
            "airport_iata": Schema(
                type=Type.STRING,
                description="IATA code of the airport (e.g. JFK, CDG).",
                nullable=False,
            ),
            "departure_date": Schema(
                type=Type.STRING,
                description="Date in YYYY-MM-DD format.",
                nullable=False,
            ),
            "direction": Schema(
                type=Type.STRING,
                description="Direction of the flight relative to the airport.",
                enum=[
                    AirportSearchDirection.DEPARTURE.value,
                    AirportSearchDirection.ARRIVAL.value,
                ],
                nullable=False,
            ),
        },
        example=(
            "User example: 'flights from jfk'"
            "Result:"
            "{'airport_iata':'JFK','direction':'departure','departure_date':'2026-01-27'}"
        ),
        required=REQUIRED_FIELDS[
            "extract_flight_info_via_airport_single_derection"
        ].required_fields,
    ),
)

query_tools = Tool(
    function_declarations=[
        extract_flight_function,
        extract_airport_route_function,
        extract_airport_route_single_derection_function,
    ]
)
query_config = GenerateContentConfig(tools=[query_tools])
