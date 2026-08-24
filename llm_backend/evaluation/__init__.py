"""RAGAS 评测模块（独立 CLI 包，不进生产链路）。

评测目标：graphrag-query 的 RAG 子图（create_multi_tool_workflow），
产出 ragas 四指标（faithfulness / answer relevancy / context precision / context recall）。

详见 spec_plan/SPEC_RAGAS_EVAL.md。
"""
