"""选股分析 PDF 报告：整合 K 线图、技术指标解读、相似形态、LLM 分析、操作建议。

使用 fpdf2 生成，支持中文，A4 纵向，适合手机和桌面阅读。
"""
from __future__ import annotations

import logging
import os
import textwrap
from datetime import date, datetime
from typing import Any

from fpdf import FPDF

logger = logging.getLogger(__name__)

# ── macOS 中文字体路径 ─────────────────────────────────────────
_FONT_PATH = "/System/Library/Fonts/STHeiti Medium.ttc"
_FONT_NAME = "STHeiti"

# ── 配色 ───────────────────────────────────────────────────────
CLR_HEADER = (26, 26, 46)        # 深蓝黑
CLR_BG_DARK = (26, 26, 46)
CLR_BG_LIGHT = (245, 247, 250)
CLR_TEXT = (44, 62, 80)
CLR_TEXT_LIGHT = (127, 140, 141)
CLR_ACCENT_RED = (231, 76, 60)
CLR_ACCENT_GREEN = (39, 174, 96)
CLR_ACCENT_BLUE = (52, 152, 219)
CLR_ACCENT_ORANGE = (230, 126, 34)
CLR_WHITE = (255, 255, 255)
CLR_DIVIDER = (189, 195, 199)
CLR_BULL = (39, 174, 96)
CLR_BEAR = (231, 76, 60)
CLR_WARN = (241, 196, 15)

# ── 常量 ───────────────────────────────────────────────────────
PAGE_W = 210
PAGE_H = 297
MARGIN = 15
CONTENT_W = PAGE_W - 2 * MARGIN


class ReportPDF(FPDF):
    """选股分析报告 PDF 生成器。"""

    def __init__(self, ts_code: str, name: str | None, as_of: str):
        super().__init__(orientation="P", unit="mm", format="A4")
        self.ts_code = ts_code
        self.name = name or ts_code
        self.as_of = as_of
        self._y = MARGIN

        # 注册中文字体
        if os.path.exists(_FONT_PATH):
            self.add_font(_FONT_NAME, "", _FONT_PATH, uni=True)
            self.add_font(_FONT_NAME, "B", _FONT_PATH, uni=True)
        else:
            logger.warning("中文字体未找到: %s，PDF 中文可能乱码", _FONT_PATH)

        self.set_auto_page_break(True, MARGIN)

    # ── 绘制辅助 ───────────────────────────────────────────────

    def _font(self, style: str = "", size: int = 10) -> None:
        self.set_font(_FONT_NAME, style, size)

    def _hrule(self, color: tuple = CLR_DIVIDER) -> None:
        self.set_draw_color(*color)
        self.set_line_width(0.3)
        y = self.get_y()
        self.line(MARGIN, y, PAGE_W - MARGIN, y)

    def _section_title(self, title: str, color: tuple = CLR_TEXT) -> None:
        self.ln(4)
        self._font("B", 13)
        self.set_text_color(*color)
        self.cell(0, 8, title, new_x="LMARGIN", new_y="NEXT")
        self._hrule(CLR_DIVIDER)
        self.ln(3)

    def _body_text(self, text: str, size: int = 9, color: tuple = CLR_TEXT) -> None:
        self._font("", size)
        self.set_text_color(*color)
        self.multi_cell(CONTENT_W, 5.5, text, align="L")
        self.ln(1)

    def _key_value(self, key: str, value: str, key_w: int = 25,
                   color: tuple = CLR_TEXT) -> None:
        self._font("B", 9)
        self.set_text_color(*CLR_TEXT_LIGHT)
        self.cell(key_w, 6, key)
        self._font("", 9)
        self.set_text_color(*color)
        self.cell(0, 6, str(value), new_x="LMARGIN", new_y="NEXT")

    def _badge(self, text: str, color: tuple) -> str:
        """返回带色标的文本标记。"""
        return text  # 简化版，PDF 中通过颜色区分

    def _verdict_color(self, verdict: str) -> tuple:
        if verdict in ("看多", "偏多"):
            return CLR_ACCENT_GREEN
        elif verdict in ("看空", "偏空"):
            return CLR_ACCENT_RED
        return CLR_ACCENT_ORANGE

    # ── 页面构建 ───────────────────────────────────────────────

    def cover_header(self, verdict: str, verdict_detail: str) -> None:
        """封面头部：股票名称、综合研判。"""
        # 深色头部背景
        self.set_fill_color(*CLR_HEADER)
        self.rect(MARGIN, MARGIN, CONTENT_W, 28, "F")

        # 股票名 + 代码
        self._font("B", 18)
        self.set_text_color(*CLR_WHITE)
        self.set_xy(MARGIN + 3, MARGIN + 3)
        self.cell(CONTENT_W - 6, 12, f"{self.name}  ({self.ts_code})")

        # 日期
        self._font("", 9)
        self.set_xy(MARGIN + 3, MARGIN + 16)
        self.cell(CONTENT_W - 6, 6, f"分析日期: {self.as_of}  |  Z-Plan 选股系统", new_x="LMARGIN", new_y="NEXT")

        # 综合研判大标签
        self.ln(5)
        vc = self._verdict_color(verdict)
        self.set_fill_color(*vc)
        w = self.get_string_width(f"  综合研判: {verdict}  ") + 4
        self.rect(MARGIN, self.get_y(), w, 12, "F")
        self._font("B", 14)
        self.set_text_color(*CLR_WHITE)
        self.set_xy(MARGIN + 2, self.get_y() + 2)
        self.cell(w, 8, f"  综合研判: {verdict}  ")

        # 详情
        self.set_xy(MARGIN + w + 5, self.get_y())
        self._font("", 9)
        self.set_text_color(*CLR_TEXT_LIGHT)
        self.cell(0, 8, verdict_detail, new_x="LMARGIN", new_y="NEXT")
        self.ln(5)

    def chart_section(self, chart_path: str, width: int = 180) -> None:
        """嵌入 K 线全景图。"""
        if not os.path.exists(chart_path):
            self._body_text("[走势图未生成]", color=CLR_TEXT_LIGHT)
            return

        self._section_title("走势全景图")
        # 计算图片适合宽度
        img_w = min(width, CONTENT_W)
        x = MARGIN + (CONTENT_W - img_w) / 2
        self.image(chart_path, x=x, w=img_w)
        self.ln(3)

    def key_prices_section(self, price_levels: dict[str, float | None],
                           verdict: str) -> None:
        """关键价位卡片。"""
        self._section_title("关键价位")
        close = price_levels.get("close")
        buy = price_levels.get("suggested_buy")
        target = price_levels.get("target_price")
        stop = price_levels.get("stop_loss")
        support = price_levels.get("support_20d")
        resistance = price_levels.get("resistance_20d")

        # 三列布局
        col_w = CONTENT_W / 3
        items = [
            ("当前价", close, CLR_ACCENT_BLUE),
            ("建议买入", buy, CLR_ACCENT_GREEN),
            ("目标价", target, CLR_ACCENT_RED),
            ("止损价", stop, CLR_TEXT_LIGHT),
            ("20日支撑", support, CLR_ACCENT_BLUE),
            ("20日阻力", resistance, CLR_ACCENT_ORANGE),
        ]

        row_y = self.get_y()
        for i, (label, val, clr) in enumerate(items):
            col = i % 3
            row = i // 3
            x = MARGIN + col * col_w
            y = row_y + row * 14

            # 背景卡片
            self.set_fill_color(*CLR_BG_LIGHT)
            self.set_draw_color(*CLR_DIVIDER)
            self.rect(x, y, col_w - 3, 12, "DF")

            # 标签
            self._font("", 7)
            self.set_text_color(*CLR_TEXT_LIGHT)
            self.set_xy(x + 2, y + 1)
            self.cell(col_w - 5, 4, label)

            # 数值
            self._font("B", 10)
            val_str = f"¥{val:.2f}" if val is not None else "--"
            self.set_text_color(*clr)
            self.set_xy(x + 2, y + 4)
            self.cell(col_w - 5, 7, val_str)

        self.set_y(row_y + 2 * 14 + 5)

    def similar_patterns_section(self, similar: dict[str, Any] | None) -> None:
        """相似历史形态。"""
        if not similar or not similar.get("matches"):
            return

        matches = similar["matches"]
        summary = similar.get("summary") or {}
        total = summary.get("total", 0)
        win = summary.get("win_count", 0)
        avg_ret = summary.get("avg_return_20d", 0)
        sim_verdict = summary.get("verdict", "")

        vc = self._verdict_color(sim_verdict)
        self._section_title(f"历史相似形态: {win}/{total} 上涨, 20日平均 {avg_ret:+.1f}% — {sim_verdict}", vc)

        # 表格头
        col_widths = [38, 38, 25, 30, 25, 24]  # 名称, 代码, 日期, 相似度, 收益, 最大涨幅
        headers = ["名称", "代码", "日期", "相似度", "20日收益", "最大涨幅"]
        self._font("B", 7)
        self.set_fill_color(*CLR_HEADER)
        self.set_text_color(*CLR_WHITE)
        for h, w in zip(headers, col_widths):
            self.cell(w, 6, h, border=0, fill=True, align="C")
        self.ln()

        for m in matches:
            self._font("", 7)
            fwd = m.get("forward_return_20d", 0) or 0
            gain = m.get("forward_max_gain", 0) or 0
            row_color = CLR_BULL if fwd > 0 else CLR_BEAR

            vals = [
                (m.get("name", "") or "")[:6],
                m["ts_code"],
                (m.get("match_date", "") or "")[5:],
                f'{m.get("similarity", 0):.0%}',
                f"{fwd:+.1f}%",
                f"{gain:+.1f}%",
            ]
            # 交替行背景
            if matches.index(m) % 2 == 0:
                self.set_fill_color(*CLR_BG_LIGHT)
            else:
                self.set_fill_color(*CLR_WHITE)

            for v, w in zip(vals, col_widths):
                self.set_text_color(*row_color)
                self.cell(w, 5.5, v, border=0, fill=True, align="C")
            self.ln()

        self.ln(2)
        self._body_text("提示: 发送「选股 代码」到 Z-Plan 机器人可查看匹配股的详细分析。",
                        size=7, color=CLR_TEXT_LIGHT)

    def llm_analysis_section(self, report: dict[str, Any],
                             llm_brief: dict[str, Any] | None) -> None:
        """LLM 分析详情。"""
        advice = report.get("投资建议") or {}
        has_full = bool(report.get("llm"))

        if has_full:
            self._section_title("LLM 深度分析")
            llm = report["llm"]

            sections = [
                ("[走势] 走势分析", advice.get("LLM股价分析") or llm.get("price_trend_analysis", "")),
                ("[技术] 技术分析", advice.get("LLM技术面分析") or llm.get("technical_analysis", "")),
                ("[财务] 财务分析", advice.get("LLM财务分析") or llm.get("financial_analysis", "")),
                ("[舆情] 舆情分析", llm.get("news_analysis", "")),
            ]
            for title, content in sections:
                if content and str(content).strip():
                    self._font("B", 10)
                    self.set_text_color(*CLR_TEXT)
                    self.cell(0, 7, title, new_x="LMARGIN", new_y="NEXT")
                    self._body_text(str(content)[:400])
        elif llm_brief:
            self._section_title("LLM 简评")
            brief = llm_brief.get("llm_brief") or {}
            trend = brief.get("trend", "")
            vs_rule = brief.get("vs_rule_engine", "")
            if trend:
                self._body_text(f"[走势] {trend}")
            if vs_rule:
                self._body_text(f"💡 {vs_rule}")
        else:
            summary = advice.get("总结", "")
            if summary:
                self._section_title("分析摘要")
                self._body_text(str(summary)[:400])

    def risks_opportunities_section(self, report: dict[str, Any],
                                    risk_flags: list[str] | None) -> None:
        """风险与机遇。"""
        risks = []
        opps = []
        if report.get("llm"):
            llm = report["llm"]
            risks = (report.get("modules", {}).get("7_公司风险", {}).get("风险要点")
                     or llm.get("risks") or [])
            opps = llm.get("opportunities") or []
        if risk_flags:
            risks = list(set((risks if isinstance(risks, list) else []) + risk_flags))

        if not risks and not opps:
            return

        self._section_title("风险与机遇")

        if risks:
            self._font("B", 9)
            self.set_text_color(*CLR_ACCENT_RED)
            self.cell(0, 6, "[!] 风险提示", new_x="LMARGIN", new_y="NEXT")
            for r in (risks if isinstance(risks, list) else [risks])[:5]:
                self._body_text(f"  • {r}", size=8, color=CLR_ACCENT_RED)

        if opps:
            self._font("B", 9)
            self.set_text_color(*CLR_ACCENT_GREEN)
            self.cell(0, 6, "[+] 机遇", new_x="LMARGIN", new_y="NEXT")
            for o in (opps if isinstance(opps, list) else [opps])[:3]:
                self._body_text(f"  • {o}", size=8, color=CLR_ACCENT_GREEN)

    def recommendation_section(self, report: dict[str, Any],
                               verdict: str) -> None:
        """操作建议 + 走势应对。"""
        advice = report.get("投资建议") or {}
        rec = advice.get("操作建议", "观望")
        scenarios = advice.get("走势应对") or []
        m4 = report.get("modules", {}).get("4_股价分析", {})
        signals = m4.get("关键信号") or []

        self._section_title("操作建议与走势应对")

        # 操作建议标签
        vc = self._verdict_color(verdict)
        self.set_fill_color(*vc)
        rec_w = self.get_string_width(f"  {rec}  ") + 4
        self.rect(MARGIN, self.get_y(), rec_w, 8, "F")
        self._font("B", 11)
        self.set_text_color(*CLR_WHITE)
        self.set_xy(MARGIN + 2, self.get_y() + 1)
        self.cell(rec_w, 6, f"  {rec}  ")
        self.ln(10)

        if signals:
            self._body_text(f"关键信号: {'；'.join(str(s) for s in signals[:4])}",
                            size=8, color=CLR_TEXT)

        if scenarios:
            self.ln(2)
            self._font("B", 10)
            self.set_text_color(*CLR_TEXT)
            self.cell(0, 6, "走势应对:", new_x="LMARGIN", new_y="NEXT")
            for s in scenarios[:3]:
                self._body_text(f"  {s}", size=8)

    def footer(self) -> None:
        """页脚。"""
        self.set_y(-20)
        self._font("", 7)
        self.set_text_color(*CLR_TEXT_LIGHT)
        self.cell(0, 5, f"Z-Plan 选股系统  ·  {self.ts_code}  ·  {self.as_of}",
                  align="C")
        self.ln(4)
        self.cell(0, 4, "本报告仅供参考，不构成投资建议。投资有风险，入市需谨慎。",
                  align="C")


# ── 主入口 ──────────────────────────────────────────────────────


def generate_pdf_report(
    ts_code: str,
    *,
    report: dict[str, Any] | None = None,
    llm_brief: dict[str, Any] | None = None,
    chart_path: str | None = None,
    price_levels: dict[str, float | None] | None = None,
    similar_patterns: dict[str, Any] | None = None,
    risk_flags: list[str] | None = None,
    output_dir: str | None = None,
) -> str:
    """生成选股分析 PDF 报告，返回文件路径。

    Args:
        ts_code: 股票代码
        report: 完整的规则引擎 + LLM 报告 dict
        llm_brief: LLM 简评结果
        chart_path: K 线全景图 PNG 路径（嵌入 PDF）
        price_levels: 价位信息
        similar_patterns: 相似形态搜索结果
        risk_flags: LLM 风险标签
        output_dir: 输出目录（默认 ``ZPLAN_ROOT/reports/``）

    Returns:
        PDF 文件绝对路径
    """
    if report is None:
        raise ValueError("report 不能为空")

    meta = report.get("meta") or {}
    name = meta.get("name") or ts_code
    as_of = report.get("as_of") or str(date.today())

    # ── 综合研判 ──
    # 需要从 wechat_pick 导入判定逻辑，这里用简化版
    advice = report.get("投资建议") or {}
    m4 = report.get("modules", {}).get("4_股价分析", {})
    tech_v = m4.get("技术面结论", "中性")
    rec = advice.get("操作建议", "观望")
    rule_s = advice.get("综合推荐分", 50)

    # 简化判定
    verdict = "观望"
    if tech_v == "偏多" and rec in ("强烈关注", "关注"):
        verdict = "看多"
    elif tech_v == "偏多":
        verdict = "偏多"
    elif tech_v == "偏空" and rec in ("谨慎", "回避"):
        verdict = "看空"
    elif tech_v == "偏空":
        verdict = "偏空"

    verdict_detail_parts = [f"规则{rule_s}分", f"技术面{tech_v}"]
    if rec:
        verdict_detail_parts.append(f"建议{rec}")
    verdict_detail = " · ".join(verdict_detail_parts)

    # ── 构建 PDF ──
    pdf = ReportPDF(ts_code, name, as_of)
    pdf.set_margin(MARGIN)
    pdf.add_page()

    # 封面头部
    pdf.cover_header(verdict, verdict_detail)

    # 走势图（优先，视觉冲击力最强）
    if chart_path and os.path.exists(chart_path):
        pdf.chart_section(chart_path)

    # 关键价位
    if price_levels:
        pdf.key_prices_section(price_levels, verdict)

    # LLM 分析
    pdf.llm_analysis_section(report, llm_brief)

    # 风险与机遇
    pdf.risks_opportunities_section(report, risk_flags)

    # 相似历史形态
    pdf.similar_patterns_section(similar_patterns)

    # 操作建议 + 走势应对
    pdf.recommendation_section(report, verdict)

    # ── 保存 ──
    if output_dir is None:
        from zplan_shared.config import ZPLAN_ROOT
        output_dir = os.path.join(ZPLAN_ROOT, "reports")
    os.makedirs(output_dir, exist_ok=True)
    as_of_str = as_of.replace("-", "")[:8]
    fname = f"{ts_code}_{as_of_str}.pdf"
    output_path = os.path.join(output_dir, fname)

    pdf.output(output_path)
    logger.info("PDF 报告已生成: %s", output_path)
    return output_path
