from sqlmodel import SQLModel, create_engine
from app.config import settings
from app.models.user import User

engine = create_engine(settings.get_database_url(), echo=True)
def create_db_and_tables():
    SQLModel.metadata.create_all(engine)
    
if __name__ == "__main__":
    create_db_and_tables()