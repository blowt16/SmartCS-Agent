"""评测报告：四指标 mean±std + 逐题明细 + 失败列表 → JSON 归档 + 控制台摘要。

MVP 不做 HTML/趋势曲线（spec §11 决策），JSON 按时间归档供调优前后对比。
"""
import importlib.metadata as _md
import json
from datetime import datetime
from pathlib import Path
from statistics import mean, stdev
from typing import Any, Dict, List

from app.core.config import settings
from app.core.logger import get_logger
from evaluation.runner import SubgraphResult

logger = get_logger(service="evaluation.report")


def _metric_stats(scores: List[float], name: str) -> Dict[str, Any]:
    """四指标 mean ± std（样本 < 2 时 std 记 None；NaN/无值样本不计入）。"""
    valid = [float(s) for s in scores if s is not None and not (isinstance(s, float) and s != s)]
    if not valid:
        return {"mean": None, "std": None, "n": 0}
    std = stdev(valid) if len(valid) >= 2 else None
    return {"mean": round(mean(valid), 4), "std": round(std, 4) if std else None, "n": len(valid)}


def summarize(
    raw_result: Any,
    results: List[SubgraphResult],
    metric_names: List[str],
    meta: Dict[str, Any],
) -> Dict[str, Any]:
    """汇总结果为统一 report dict（meta / metrics / samples / failures）。"""
    try:
        df = raw_result.to_pandas()
    except Exception:  # noqa: BLE001 —— 兼容不同版本 Result 形态
        df = None

    metrics_out = {}
    samples_out: List[Dict[str, Any]] = []
    if df is not None:
        for name in metric_names:
            # 指标可能以全名/短名两种列出现（ResponseRelevancy / answer_relevancy 之类）
            col = next((c for c in df.columns if name in str(c).lower() or str(c).lower() in name), name)
            metrics_out[name] = _metric_stats(list(df[col]) if col in df.columns else [], name)
        for row in df.to_dict(orient="records"):
            samples_out.append({k: v for k, v in row.items()})

    failures = []
    for r in results:
        if not r.ok or r.timed_out or r.empty_context:
            failures.append(
                {
                    "question": r.question,
                    "resolved_question": r.resolved_question if r.was_resolved else r.question,
                    "status": "empty_context" if r.empty_context else ("timed_out" if r.timed_out else "error"),
                    "error": r.error,
                    "elapsed": round(r.elapsed, 2),
                }
            )

    return {
        "meta": {
            "created_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "ragas_version": _md.version("ragas"),
            **meta,
        },
        "metrics": metrics_out,
        "samples": samples_out,
        "failures": failures,
    }


def dump_report(report: Dict[str, Any], results_dir: Path, results_dir_override: str = "") -> Path:
    """报告落盘：evaluation/results/ragas_YYYYMMDD_HHMMSS.json（UTF-8）。"""
    out_dir = Path(results_dir_override) if results_dir_override else (
        Path(__file__).resolve().parent.parent / results_dir
    )
    out_dir.mkdir(parents=True, exist_ok=True)
    path = out_dir / f"ragas_{datetime.now().strftime('%Y%m%d_%H%M%S')}.json"
    path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    logger.info("报告已归档: {}", path)
    return path


def print_summary(report: Dict[str, Any]) -> None:
    """控制台中文摘要：四指标 mean±std + 失败明细 + 消解标记。"""
    out = [_eq() + " RAGAS 评测结果 " + _eq()]
    m = report["metrics"]
    for name, stat in m.items():
        if stat["mean"] is None:
            out.append(f"  {name:20s} 无有效样本")
        else:
            std = f"±{stat['std']}" if stat.get("std") else ""
            out.append(f"  {name:20s} {stat['mean']:.4f} {std} (n={stat['n']})")
    out.append(f"  完成题: {len(report['samples'])}  失败/跳过: {len(report['failures'])}")
    for f in report["failures"]:
        out.append(f"    ✗ [{f['status']}] {f['question'][:60]}{' → ' + f['resolved_question'][:60] if f['status'] == 'resolved' else ''}")
    out.append(_eq() + " END " + _eq())
    print("\n".join(out))


def _eq() -> str:
    return "=" * 16
