from fastapi import APIRouter

from app.api.routes import auth, backups, chat, documents, notes, search, user

api_router = APIRouter()
api_router.include_router(auth.router)
api_router.include_router(user.router)
api_router.include_router(documents.router)
api_router.include_router(notes.router)
api_router.include_router(chat.router)
api_router.include_router(search.router)
api_router.include_router(backups.router)
