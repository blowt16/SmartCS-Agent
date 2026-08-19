"""
精排服务（bge-reranker-v2-m3 CrossEncoder）

作用：RRF 融合后对候选文档做精确排序，替代原 LLM 相关性评分（grade_relevance）。
    CrossEncoder 将 (query, doc) 拼接输入模型输出相关性分数，比双塔向量更精确。

设备支持：
    - cuda：GPU 推理（fp16 半精度，显存占用减半）
    - cpu：CPU 推理（fp32，自动关闭半精度）
    - auto：torch.cuda.is_available() 自动选择

降级策略：
    - 模型加载失败 / 评分异常 → 返回 None，由调用方跳过精排直接使用融合结果，不阻塞主链路
"""

import threading
from typing import Any, Dict, List, Optional

from app.core.config import settings
from app.core.logger import get_logger

logger = get_logger(service="reranker")


class RerankerService:
    """bge-reranker CrossEncoder 精排服务（懒加载单例，首次调用触发模型下载+加载）"""

    def __init__(self):
        self._model = None
        self._device: Optional[str] = None
        self._lock = threading.Lock()

    def _resolve_device(self) -> str:
        """解析精排设备：auto / cuda / cpu"""
        configured = settings.RERANKER_DEVICE
        if configured == "auto":
            import torch

            return "cuda" if torch.cuda.is_available() else "cpu"
        if configured in ("cuda", "cpu"):
            return configured
        logger.warning("未知 RERANKER_DEVICE: {}，回退 cpu", configured)
        return "cpu"

    def _load_model(self):
        """懒加载 CrossEncoder（线程安全）"""
        if self._model is None:
            with self._lock:
                if self._model is None:
                    from sentence_transformers import CrossEncoder

                    device = self._resolve_device()
                    use_half = settings.RERANKER_HALF_PRECISION and device.startswith("cuda")
                    logger.info(
                        "加载精排模型 {} @ {}（fp16={}，max_length={}）",
                        settings.RERANKER_MODEL, device, use_half, settings.RERANKER_MAX_LENGTH,
                    )
                    self._model = CrossEncoder(
                        settings.RERANKER_MODEL,
                        device=device,
                        max_length=settings.RERANKER_MAX_LENGTH,
                    )
                    if use_half:
                        # ST 5.7 CrossEncoder 无 half_precision 参数，加载后手动转 fp16（仅 GPU）
                        self._model.model.half()
                    self._device = device
                    logger.info("精排模型加载完成 @ {}", device)

    def rerank(
        self,
        query: str,
        candidates: List[Dict[str, Any]],
        top_k: int,
        text_key: str = "text",
    ) -> Optional[List[Dict[str, Any]]]:
        """
        对候选文档精排，返回 top_k 条（按相关性降序，附加 rerank_score）。

        Args:
            query: 用户查询
            candidates: RRF 融合后的候选文档列表
            top_k: 精排返回数
            text_key: 文档中用于评分的文本字段名

        Returns:
            精排后的文档列表；失败返回 None（调用方降级为跳过精排）
        """
        if not candidates:
            return []

        try:
            self._load_model()
        except Exception as e:
            logger.error("精排模型加载失败，跳过精排: {}", e)
            return None

        try:
            pairs = [(query, doc.get(text_key, "")) for doc in candidates]
            scores = self._model.predict(pairs, batch_size=settings.RERANKER_BATCH_SIZE)

            ranked = sorted(
                zip(candidates, scores),
                key=lambda x: float(x[1]),
                reverse=True,
            )
            results = []
            for doc, score in ranked[:top_k]:
                out = dict(doc)
                out["rerank_score"] = float(score)
                results.append(out)

            logger.info(
                "精排完成: {} 条候选 → {} 条（device={}）",
                len(candidates), len(results), self._device,
            )
            return results
        except Exception as e:
            logger.error("精排评分异常，跳过精排: {}", e)
            return None


# ==================== 单例 ====================

_service: Optional[RerankerService] = None
_service_lock = threading.Lock()


def get_reranker_service() -> RerankerService:
    """模块级懒加载单例（沿用 lock + double-check 模式）"""
    global _service
    if _service is None:
        with _service_lock:
            if _service is None:
                _service = RerankerService()
    return _service
