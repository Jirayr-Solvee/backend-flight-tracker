import logging
import os

from fastapi import APIRouter, Depends, HTTPException, status
from fastapi.responses import FileResponse

from ..dependency import get_current_user
import re
from ..config import settings
import json

router = APIRouter()
logger = logging.getLogger(__name__)

FLAGS_DIR = "country_flags"


@router.get(
    "/country/{country_code}",
    dependencies=[Depends(get_current_user)],
    summary="Get country flag from flags dir based on provided country code in two letter format eg: MA",
)
async def get_flag(country_code: str):
    """
    Get country flag from flags dir based on provided country code in two letter format eg: MA
    """
    try:
        COUNTRY_CODE_PATTERN = re.compile(r"^[a-zA-Z]{2}$")

        if not COUNTRY_CODE_PATTERN.match(country_code):
            raise HTTPException(status_code=400, detail="Invalid code")

        filename = f"{country_code.lower()}.png"
        file_path = os.path.join(FLAGS_DIR, filename)

        if os.path.exists(file_path):
            return FileResponse(file_path, media_type="image/png")

        logger.warning(f"Unable to serve flag code={country_code}")
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Flag not found"
        )
    except HTTPException:
        raise
    except Exception:
        logger.exception(f"Error while Unable to serving flag code={country_code}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Internal server error",
        )

AIRLINE_FLAGS_DIR = "airline_flags"

@router.get(
    "/airline/{airline_iata_code}",
    dependencies=[Depends(get_current_user)],
    summary="Get airline flag from flags dir based on provided country code in two letter format eg: MA",
)
async def get_airline_flag(airline_iata_code: str):
    try:
        AIRLINE_CODE_PATTERN = re.compile(r"^[a-zA-Z]{2}$")

        if not AIRLINE_CODE_PATTERN.match(airline_iata_code):
            raise HTTPException(status_code=400, detail="Invalid code")

        with open(settings.AIRLINE_MAP_JSON, "r") as f:
            IATA_TO_ICAO = json.load(f)

        airline_icao_code: str | None = IATA_TO_ICAO.get(airline_iata_code.upper())
        if not airline_icao_code:
            raise HTTPException(status_code=400, detail="Unable to find airline code")

        filename = f"{airline_icao_code.upper()}.png"
        file_path = os.path.join(AIRLINE_FLAGS_DIR, filename)

        if os.path.exists(file_path):
            return FileResponse(file_path, media_type="image/png")

        logger.warning(f"Unable to serve airline flag for code={airline_iata_code}")
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Flag not found"
        )
    except HTTPException:
        raise
    except Exception:
        logger.exception(f"Error while Unable to serving airline flag for code={airline_iata_code}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Internal server error",
        )
