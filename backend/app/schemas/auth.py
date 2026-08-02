from pydantic import BaseModel


class LoginRequest(BaseModel):
    email: str
    password: str


class UserResponse(BaseModel):
    id: str
    user_id: str
    email: str | None
    username: str
    role: str
    roles: list[str]
    is_active: bool
    status: str
    version: int


class LoginResponse(BaseModel):
    access_token: str
    token_type: str = "bearer"
    user: UserResponse
