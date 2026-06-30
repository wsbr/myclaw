from pydantic import BaseModel, EmailStr


class LoginRequest(BaseModel):
    email: str  # plain str to allow any domain (including .local, .internal)
    password: str


class TokenResponse(BaseModel):
    access_token: str
    token_type: str = "bearer"


class UserResponse(BaseModel):
    id: str
    email: str
    username: str
    role: str
    is_active: bool

    model_config = {"from_attributes": True}


class LoginResponse(BaseModel):
    access_token: str
    token_type: str = "bearer"
    user: UserResponse
