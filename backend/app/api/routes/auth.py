from fastapi import FastAPI, APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, EmailStr
from sqlmodel import Session, select, col, delete, func
from backend.app.models.user import User
from passlib.context import CryptContext
from datetime import datetime, timedelta
from typing import Optional, Any
from fastapi.security import OAuth2PasswordBearer, OAuth2PasswordRequestForm
from jose import JWTError, jwt

SECRET_KEY = "b40c2b07bda1ff49aaf7faa27caceafc129a67f7ea72a8927ee6cef1cec5faca"
ALGORITHM = "HS256"
ACCESS_TOKEN_EXPIRE_MINUTES = 30

class Token(BaseModel):
    access_token: str
    token_type: str
    
class TokenData(BaseModel):
    username: Optional[str] = None
    
password_context = CryptContext(schemes=['bcrypt'], deprecated="auto")
oauth2_scheme = OAuth2PasswordBearer(tokenUrl="token")

router = APIRouter()

class RegisterIn(BaseModel):
    email: EmailStr
    full_name: str
    password: str
    
class LoginIn(BaseModel):
    email: EmailStr
    password: str

def verify_password(plain_password, hashed_password):
    return password_context.verify(plain_password, hashed_password)
    
def get_password_hash(password):
    return password_context.hash(password)
    
async def get_user(username: str):
    query = User.select().where(User.c)

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