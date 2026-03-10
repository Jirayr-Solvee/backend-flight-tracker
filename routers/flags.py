import logging
import os

from fastapi import APIRouter, Depends, HTTPException, status
from fastapi.responses import FileResponse

from ..dependency import get_current_user

router = APIRouter()
logger = logging.getLogger(__name__)

FLAGS_DIR = "flags"


@router.get(
    "/{country_code}",
    dependencies=[Depends(get_current_user)],
    summary="Get country flag from flags dir based on provided country code in two letter format eg: MA",
)
async def get_flag(country_code: str):
    """
    Get country flag from flags dir based on provided country code in two letter format eg: MA
    """
    try:
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
