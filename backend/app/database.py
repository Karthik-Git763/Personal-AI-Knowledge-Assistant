from sqlmodel import SQLModel, create_engine
from app.config import settings

def init_db():
    engine = create_engine(settings.get_database_url(), echo=True)
    SQLModel.metadata.create_all(engine)
    return engine
