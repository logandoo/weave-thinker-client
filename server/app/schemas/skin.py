from pydantic import BaseModel


class UiPreferencesResponse(BaseModel):
    skin_id: str


class UiPreferencesUpdate(BaseModel):
    skin_id: str
