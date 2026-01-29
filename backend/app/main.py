from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from backend.app.config import settings
from backend.app.database import create_db_and_tables
from backend.app.api import auth, chat, documents, notes, search

# Create FastAPI app
app = FastAPI(
    title=settings.PROJECT_NAME,
    description=settings.PROJECT_DESCRIPTION,
    version=settings.VERSION,
    debug=settings.DEBUG
)

# Configure CORS
app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.ALLOWED_ORIGINS,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Create database tables on startup
@app.on_event("startup")
def on_startup():
    create_db_and_tables()

# Root endpoint
@app.get("/")
def read_root():
    return {
        "message": "Welcome to Personal Knowledge Assistant API",
        "version": settings.VERSION,
        "docs": "/docs"
    }

# Health check endpoint
@app.get("/health")
def health_check():
    return {"status": "healthy"}

# app.include_router(auth.router, prefix="/api/auth", tags=["auth"])
# app.include_router(chat.router, prefix="/api/chat", tags=["chat"])
# app.include_router(documents.router, prefix="/api/documents", tags=["documents"])
# app.include_router(notes.router, prefix="/api/notes", tags=["notes"])
# app.include_router(search.router, prefix="/api/search", tags=["search"])
