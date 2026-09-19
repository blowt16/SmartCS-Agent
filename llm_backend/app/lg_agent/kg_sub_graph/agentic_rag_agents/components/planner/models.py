from typing import List

from pydantic import BaseModel, Field


class EntitySubQuery(BaseModel):
    """LLM 输出的单条任务（与内部 Task 分离：LLM 不再感知 parent_task）"""

    name: str = Field(
        default="",
        description=(
            "本条任务的主题词：商品全名或品类词（如'米家智能晾衣机2'/'智能电动沙发'）。"
            "拆成多条任务（tasks≥2）时必须给出；无法确定实体时留空。"
            "（仅用于日志与拆解质量观测，不流入检索侧——见 SPEC §4.1 说明）"
        ),
    )
    sub_query: str = Field(
        default="",
        description="可直接独立检索的子问文本（自含主题词，禁止指代）",
    )


class PlannerOutput(BaseModel):
    entity_count: int = Field(
        default=0, ge=0,
        description="识别到的商品/品类实体数（决策辅助与日志，不作数量校验锚）",
    )
    tasks: List[EntitySubQuery] = Field(
        default_factory=list,
        description="1..3 条；无法拆分时恰 1 条且=原 query 逐字复制；拆分时每条为可独立检索的子问",
    )
