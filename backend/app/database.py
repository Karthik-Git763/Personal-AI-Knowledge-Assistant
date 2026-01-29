from sqlmodel import SQLModel, create_engine
from backend.app.config import settings
from backend.app.models.user import User, UserSettings
from backend.app.models.document import Document, DocumentChunks
from backend.app.models.chat import ChatMessages, ChatSession
from backend.app.models.note import Notes, NoteFolders, NoteTags, NoteTagRelations, NoteTemplates, NoteCollaborators, NoteLinks

engine = create_engine(settings.get_database_url(), echo=True)
def create_db_and_tables():
    SQLModel.metadata.create_all(engine)
    
if __name__ == "__main__":
    create_db_and_tables()