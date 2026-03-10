import asyncio
import logging
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
        self.client = genai.Client(api_key=settings.GEMINI_API_KEY)

    async def _generate(
        self, contents: str, config: GenerateContentConfig
    ) -> GenerateContentResponse:
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
        self, query: str, email: bool = False
    ) -> ResolvedFunctionCall | None:
        if not email:
            today_utc = datetime.now(timezone.utc).strftime("%Y-%m-%d")
            query = f"Current date (UTC) is {today_utc}.\n {query}"

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
                assert function_def  # already checked its a valid function in _validate_function_args
                handler = function_def.handler

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
