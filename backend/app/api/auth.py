from fastapi import FastAPI, APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, EmailStr
from sqlmodel import Session, select
from backend.app.models.user import User
from passlib.context import CryptContext

router = APIRouter()
password_context = CryptContext(schemes=['bcrypt'], deprecated="auto")

class RegisterIn(BaseModel):
    email: EmailStr
    full_name: str
    password: str
    
class LoginIn(BaseModel):
    email: EmailStr
    password: str

@router.post("/register", status_code=201)
def register(payload: RegisterIn, session: Session = Depends()):
    existing = session.exec(select(User).where(User.email == payload.email))
    if existing:
       raise HTTPException(status_code=400, detail="Email already registered")
    hashed = password_context.hash(payload.password)
    user = User(email=payload.email, full_name=payload.full_name, hashed_password=hashed)
    session.add(user)
    session.commit()
    session.refresh(user)
    return {"id": user.id, "email": user.email, "full_name": user.full_name}