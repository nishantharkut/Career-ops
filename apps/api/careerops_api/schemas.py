from typing import Any

from pydantic import BaseModel, Field


class CreateApplicationRequest(BaseModel):
    company: str = Field(min_length=1)
    role_title: str = Field(min_length=1)
    location: str = "Unknown"
    source_url: str = ""
    jd_text: str = Field(min_length=1)
    deadline: str | None = None
    notes: str = ""


class AnalyzeRequest(BaseModel):
    claims: list[dict[str, Any]] = Field(default_factory=list)


class ApproveRequest(BaseModel):
    report_id: str
    note: str = ""


class MarkSubmittedRequest(BaseModel):
    note: str = ""
