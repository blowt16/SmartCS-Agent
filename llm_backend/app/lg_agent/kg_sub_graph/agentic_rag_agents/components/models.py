from typing import Any, Optional

from pydantic import BaseModel, Field


class Task(BaseModel):
    question: str = Field(..., description="The question to be addressed.")
    parent_task: str = Field(
        ..., description="The parent task this task is derived from."
    )
    data: Optional[Any] = Field(
        default=None, description="The search result details."
    )
