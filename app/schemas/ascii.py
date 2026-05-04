from pydantic import BaseModel


class AsciiArtResponse(BaseModel):
    subject: str
    art: str
