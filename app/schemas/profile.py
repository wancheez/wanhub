from pydantic import BaseModel


class Profile(BaseModel):
    name: str
    occupation: str
    interests: list[str]
    bio: str
