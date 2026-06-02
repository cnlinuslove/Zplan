"""Gemini 2.5 Pro 调用成本估算（美元；价格以 Google AI 开发者 API 为准，可能调整）。"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any

# 标准上下文（≤200K tokens）— 见 https://ai.google.dev/gemini-api/docs/pricing
PRICE_INPUT_PER_M = 1.25
PRICE_OUTPUT_PER_M = 10.0
PRICE_INPUT_LONG_PER_M = 2.5
PRICE_OUTPUT_LONG_PER_M = 15.0
LONG_CONTEXT_THRESHOLD = 200_000

# 经验估算（中文 JSON 研报；实际以 API usageMetadata 为准）
EST_FULL_REPORT_INPUT = 4_500
EST_FULL_REPORT_OUTPUT = 2_000
EST_SCAN_BRIEF_BATCH_INPUT = 2_500
EST_SCAN_BRIEF_BATCH_OUTPUT = 1_200
EST_SCAN_BRIEF_PER_STOCK_INPUT = 900
EST_SCAN_BRIEF_PER_STOCK_OUTPUT = 280


@dataclass
class CostEstimate:
    label: str
    input_tokens: int
    output_tokens: int
    usd: float
    cny_approx: float
    model: str = "gemini-2.5-pro"
    note: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "label": self.label,
            "input_tokens": self.input_tokens,
            "output_tokens": self.output_tokens,
            "total_tokens": self.input_tokens + self.output_tokens,
            "usd": round(self.usd, 4),
            "cny_approx": round(self.cny_approx, 3),
            "model": self.model,
            "note": self.note,
        }


def _price(input_tokens: int, output_tokens: int) -> float:
    total_in = input_tokens
    if total_in > LONG_CONTEXT_THRESHOLD:
        return (
            total_in / 1_000_000 * PRICE_INPUT_LONG_PER_M
            + output_tokens / 1_000_000 * PRICE_OUTPUT_LONG_PER_M
        )
    return (
        total_in / 1_000_000 * PRICE_INPUT_PER_M
        + output_tokens / 1_000_000 * PRICE_OUTPUT_PER_M
    )


def estimate_from_usage(usage: dict[str, Any] | None, *, label: str = "实际调用") -> CostEstimate | None:
    if not usage:
        return None
    inp = int(usage.get("prompt_tokens") or 0)
    out = int(usage.get("output_tokens") or 0)
    usd = _price(inp, out)
    return CostEstimate(
        label=label,
        input_tokens=inp,
        output_tokens=out,
        usd=usd,
        cny_approx=usd * 7.2,
        model=str(usage.get("model") or "gemini-2.5-pro"),
        note="来自 API usageMetadata",
    )


def estimate_full_report(*, fx_cny: float = 7.2) -> CostEstimate:
    usd = _price(EST_FULL_REPORT_INPUT, EST_FULL_REPORT_OUTPUT)
    return CostEstimate(
        label="单票深度研报",
        input_tokens=EST_FULL_REPORT_INPUT,
        output_tokens=EST_FULL_REPORT_OUTPUT,
        usd=usd,
        cny_approx=usd * fx_cny,
        note="含近30日K线+指标+资讯上下文；约40–50s/次",
    )


def estimate_scan_brief(top_n: int, *, batch: bool = True, fx_cny: float = 7.2) -> CostEstimate:
    n = max(1, top_n)
    if batch:
        inp = EST_SCAN_BRIEF_BATCH_INPUT + n * 120
        out = EST_SCAN_BRIEF_BATCH_OUTPUT + n * 80
        label = f"扫描 Top{n} 批量简评（1 次 API）"
    else:
        inp = EST_SCAN_BRIEF_PER_STOCK_INPUT * n
        out = EST_SCAN_BRIEF_PER_STOCK_OUTPUT * n
        label = f"扫描 Top{n} 逐只简评（{n} 次 API）"
    usd = _price(inp, out)
    return CostEstimate(
        label=label,
        input_tokens=inp,
        output_tokens=out,
        usd=usd,
        cny_approx=usd * fx_cny,
        note="简评：走势一句话+LLM综合分+操作建议",
    )


def format_cost_table(*items: CostEstimate) -> str:
    lines = [
        "| 场景 | 输入 tok | 输出 tok | 约 USD | 约 CNY |",
        "|------|---------|---------|--------|--------|",
    ]
    for e in items:
        lines.append(
            f"| {e.label} | {e.input_tokens:,} | {e.output_tokens:,} | "
            f"${e.usd:.4f} | ¥{e.cny_approx:.2f} |"
        )
    lines.append("")
    lines.append(
        f"定价基准：{items[0].model if items else 'gemini-2.5-pro'} "
        f"≤200K 上下文 ${PRICE_INPUT_PER_M}/M 输入、${PRICE_OUTPUT_PER_M}/M 输出。"
    )
    lines.append("免费档约 **20 次/天**（按 Google AI 配额），超出需计费或换 Key。")
    return "\n".join(lines)
