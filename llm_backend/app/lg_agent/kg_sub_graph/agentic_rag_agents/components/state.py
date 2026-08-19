from operator import add
from typing import Annotated, Any, Dict, List

from typing_extensions import TypedDict

from ..components.models import Task


class SearchHistoryRecord(TypedDict):
    """向量检索结果的历史记录（简化表示）"""

    task: str
    records: Dict[str, Any]


class HistoryRecord(TypedDict):
    """Information that may be relevant to future user questions."""

    question: str
    answer: str
    searches: List[SearchHistoryRecord]


def update_history(
    history: List[HistoryRecord], new: List[HistoryRecord]
) -> List[HistoryRecord]:
    """
    Update the history record. Allow only a max number of records to be stored at any time.

    Parameters
    ----------
    history : List[HistoryRecord]
        The current history list.
    new : List[HistoryRecord]
        The new record to add. Should be a single entry list.

    Returns
    -------
    List[HistoryRecord]
        A new List with the record added and old records removed to maintain size.
    """

    SIZE: int = 5

    history.extend(new)
    return history[-SIZE:]


class InputState(TypedDict):
    """The input state for multi agent workflows."""

    question: str
    data: List[Dict[str, Any]]
    history: Annotated[List[HistoryRecord], update_history]


class OverallState(TypedDict):
    """The main state in multi agent workflows."""

    question: str
    tasks: Annotated[List[Task], add]
    searches: Annotated[List[Any], add]   # 向量检索结果（VectorSearchOutputState 列表）
    summary: str
    steps: Annotated[List[str], add]
    history: Annotated[List[HistoryRecord], update_history]


class OutputState(TypedDict):
    """The final output for multi agent workflows."""

    answer: str
    question: str
    steps: List[str]
    searches: List[Any]
    history: Annotated[List[HistoryRecord], update_history]
