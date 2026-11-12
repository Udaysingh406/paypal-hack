from pydantic import BaseModel, ConfigDict, Field, field_validator

from app.core.currencies import normalize_currency


class UserCreate(BaseModel):
    email: str
    display_name: str = Field(min_length=1, max_length=120)
    password: str = Field(min_length=8, max_length=128)


class LoginRequest(BaseModel):
    email: str
    password: str


class GroupCreate(BaseModel):
    name: str = Field(min_length=1, max_length=160)


class MemberCreate(BaseModel):
    user_id: str
    role: str = Field(default="member", pattern="^member$")


class ExpenseCreate(BaseModel):
    description: str = Field(min_length=1, max_length=240)
    amount_minor: int = Field(gt=0)
    currency: str = Field(min_length=3, max_length=3)
    paid_by_id: str
    splits: dict[str, int]

    @field_validator("currency")
    @classmethod
    def normalize_currency(cls, value: str) -> str:
        return normalize_currency(value)


class ChatRequest(BaseModel):
    message: str = Field(min_length=1)
    context: dict = {}


class ModelOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
