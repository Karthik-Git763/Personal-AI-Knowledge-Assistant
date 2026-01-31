from fastapi import APIRouter
from app.core.config import settings

router = APIRouter()

if settings.ENVIRONMENT == "local":
    router.include_router(router)