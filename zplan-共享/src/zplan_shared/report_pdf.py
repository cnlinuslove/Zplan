"""选股分析 PDF 报告：整合 K 线图、指标解读、相似形态、LLM 分析、操作建议。

使用 fpdf2 生成，A4 纵向，严格控制分页避免空白页。
"""
from __future__ import annotations

import logging
import os
from datetime import date
from typing import Any

from fpdf import FPDF

logger = logging.getLogger(__name__)

_FONT_PATH = "/System/Library/Fonts/STHeiti Medium.ttc"
_FONT_NAME = "STHeiti"

# ── 配色 ───────────────────────────────────────────────────────
CLR_DARK = (26, 26, 46)
CLR_BG_LIGHT = (245, 247, 250)
CLR_TEXT = (44, 62, 80)
CLR_MUTED = (127, 140, 141)
CLR_RED = (231, 76, 60)
CLR_GREEN = (39, 174, 96)
CLR_BLUE = (52, 152, 219)
CLR_ORANGE = (230, 126, 34)
CLR_WHITE = (255, 255, 255)
CLR_DIVIDER = (200, 200, 205)

PAGE_W = 210
PAGE_H = 297
MARGIN = 14
CW = PAGE_W - 2 * MARGIN  # content width


class ReportPDF(FPDF):
    def __init__(self, ts_code: str, name: str, as_of: str):
        super().__init__("P", "mm", "A4")
        self.ts_code = ts_code
        self.name = name or ts_code
        self.as_of = as_of
        self.set_margin(MARGIN)

        if os.path.exists(_FONT_PATH):
            self.add_font(_FONT_NAME, "", _FONT_PATH)
            self.add_font(_FONT_NAME, "B", _FONT_PATH)
        else:
            logger.warning("中文字体缺失: %s", _FONT_PATH)

        self.set_auto_page_break(False)  # 手动控制分页

    # ── helpers ────────────────────────────────────────────────

    def _use(self, style: str = "", size: int = 10) -> None:
        self.set_font(_FONT_NAME, style, size)

    def _space_left(self) -> float:
        return PAGE_H - MARGIN - self.get_y()

    def _need(self, mm: float) -> bool:
        """需要多少空间，不够则新开一页。"""
        if self._space_left() < mm:
            self.add_page()
            return True
        return False

    def _hdr(self, title: str, color: tuple = CLR_DARK) -> None:
        """章节标题 + 分隔线。"""
        self._need(12)
        self.ln(3)
        self._use("B", 12)
        self.set_text_color(*color)
        self.cell(0, 7, title, new_x="LMARGIN", new_y="NEXT")
        self.set_draw_color(*CLR_DIVIDER)
        self.line(MARGIN, self.get_y() + 1, PAGE_W - MARGIN, self.get_y() + 1)
        self.ln(4)

    def _p(self, text: str, size: int = 9, color: tuple = CLR_TEXT) -> None:
        """正文段落。"""
        if not text or not str(text).strip():
            return
        self._use("", size)
        self.set_text_color(*color)
        self._need(6)
        self.multi_cell(CW, 5.2, str(text)[:600], align="L")
        self.ln(1)

    def _badge(self, label: str, color: tuple, x: float | None = None,
               y: float | None = None, w: float | None = None) -> None:
        """色块标签。"""
        if x is None:
            x = self.get_x()
        if y is None:
            y = self.get_y()
        txt = f" {label} "
        if w is None:
            w = self.get_string_width(txt) + 3
        self.set_fill_color(*color)
        self.rect(x, y, w, 7, "F")
        self._use("B", 10)
        self.set_text_color(*CLR_WHITE)
        self.set_xy(x + 1.5, y + 0.5)
        self.cell(w - 1, 5, label)

    def _row_kv(self, key: str, val: str, color: tuple = CLR_TEXT) -> None:
        self._use("", 8)
        self.set_text_color(*CLR_MUTED)
        self.cell(22, 5, key)
        self.set_text_color(*color)
        self.cell(0, 5, str(val), new_x="LMARGIN", new_y="NEXT")

    # ── pages ──────────────────────────────────────────────────

    def page_cover(self, verdict: str, verdict_detail: str) -> None:
        self._need(20)
        # 标题栏
        self.set_fill_color(*CLR_DARK)
        self.rect(MARGIN, self.get_y(), CW, 24, "F")
        self._use("B", 16)
        self.set_text_color(*CLR_WHITE)
        self.set_xy(MARGIN + 3, self.get_y() + 3)
        self.cell(CW - 6, 8, f"{self.name}  ({self.ts_code})")
        self._use("", 8)
        self.set_xy(MARGIN + 3, self.get_y() + 12)
        self.cell(CW - 6, 5, f"{self.as_of}  |  Z-Plan 选股系统")
        self.ln(28)

        # 综合研判标签
        vc = CLR_GREEN if verdict in ("看多", "偏多") else (CLR_RED if verdict in ("看空", "偏空") else CLR_ORANGE)
        self._badge(f"综合研判: {verdict}", vc)
        self.ln(10)

        # 详情
        self._use("", 9)
        self.set_text_color(*CLR_MUTED)
        self.cell(0, 5, verdict_detail, new_x="LMARGIN", new_y="NEXT")
        self.ln(4)

    def page_chart(self, chart_path: str) -> None:
        """走势图：如果当前页剩余空间 > 图高 60%，就放当前页否则新页。"""
        if not chart_path or not os.path.exists(chart_path):
            return

        self._hdr("走势全景图")

        # 图片按内容宽度缩放
        img_h = CW * 0.55  # 估算高度
        if self._space_left() < img_h:
            self.add_page()
            self._hdr("走势全景图")

        self.image(chart_path, x=MARGIN, w=CW)
        self.ln(4)

    def page_prices(self, levels: dict[str, float | None]) -> None:
        self._hdr("关键价位")
        self._need(25)
        items = [
            ("当前价", levels.get("close"), CLR_BLUE),
            ("回调关注", levels.get("suggested_buy"), CLR_GREEN),
            ("目标价", levels.get("target_price"), CLR_RED),
            ("止损价", levels.get("stop_loss"), CLR_MUTED),
            ("20日支撑", levels.get("support_20d"), CLR_BLUE),
            ("20日阻力", levels.get("resistance_20d"), CLR_ORANGE),
        ]
        col_w = CW / 3
        y0 = self.get_y()
        for i, (label, val, clr) in enumerate(items):
            col = i % 3
            row = i // 3
            x = MARGIN + col * col_w
            y = y0 + row * 14
            self.set_fill_color(*CLR_BG_LIGHT)
            self.set_draw_color(220, 220, 225)
            self.rect(x, y, col_w - 2, 12, "DF")
            self._use("", 7)
            self.set_text_color(*CLR_MUTED)
            self.set_xy(x + 2, y + 1)
            self.cell(col_w - 4, 4, label)
            self._use("B", 10)
            val_s = f"¥{val:.2f}" if val is not None else "--"
            self.set_text_color(*clr)
            self.set_xy(x + 2, y + 5)
            self.cell(col_w - 4, 6, val_s)
        self.set_y(y0 + 2 * 14 + 6)

    def page_llm(self, report: dict[str, Any], llm_brief: dict[str, Any] | None) -> None:
        advice = report.get("投资建议") or {}
        has_full = bool(report.get("llm"))

        if has_full:
            self._need(20)
            self._hdr("LLM 深度分析")
            llm = report["llm"]
            sections = [
                ("走势分析", advice.get("LLM股价分析") or llm.get("price_trend_analysis", "")),
                ("技术分析", advice.get("LLM技术面分析") or llm.get("technical_analysis", "")),
                ("财务分析", advice.get("LLM财务分析") or llm.get("financial_analysis", "")),
                ("舆情分析", llm.get("news_analysis", "")),
            ]
            for i, (title, content) in enumerate(sections):
                if content and str(content).strip():
                    self._need(12)
                    if i > 0:
                        self.ln(2)
                    self._use("B", 10)
                    self.set_text_color(*CLR_DARK)
                    self.cell(0, 6, title, new_x="LMARGIN", new_y="NEXT")
                    self._p(str(content), size=8)
        elif llm_brief:
            self._need(15)
            self._hdr("LLM 简评")
            brief = llm_brief.get("llm_brief") or {}
            for k in ("trend", "vs_rule_engine"):
                v = brief.get(k, "")
                if v:
                    self._p(str(v), size=8)
        else:
            s = advice.get("总结", "")
            if s:
                self._hdr("分析摘要")
                self._p(str(s), size=8)

    def page_risks(self, report: dict[str, Any], risk_flags: list[str] | None) -> None:
        risks = []
        opps = []
        if report.get("llm"):
            llm = report["llm"]
            risks = (report.get("modules", {}).get("7_公司风险", {}).get("风险要点")
                     or llm.get("risks") or [])
            opps = llm.get("opportunities") or []
        if risk_flags:
            risks = list(dict.fromkeys((risks if isinstance(risks, list) else []) + risk_flags))

        if not risks and not opps:
            return

        self._need(20)
        self._hdr("风险与机遇")

        if risks:
            self._use("B", 9)
            self.set_text_color(*CLR_RED)
            self.cell(0, 5, "风险提示", new_x="LMARGIN", new_y="NEXT")
            for r in (risks if isinstance(risks, list) else [risks])[:5]:
                self._p(f"• {r}", size=8, color=CLR_RED)

        if opps:
            self.ln(2)
            self._use("B", 9)
            self.set_text_color(*CLR_GREEN)
            self.cell(0, 5, "机遇", new_x="LMARGIN", new_y="NEXT")
            for o in (opps if isinstance(opps, list) else [opps])[:3]:
                self._p(f"• {o}", size=8, color=CLR_GREEN)

    def page_macd_chart(self, chart_macd_path: str) -> None:
        """MACD + 相似形态画廊图（图片嵌入，与 page_chart 风格一致）。"""
        if not chart_macd_path or not os.path.exists(chart_macd_path):
            return

        img_h = CW * 0.65  # MACD 图通常比 K 线图稍矮
        if self._space_left() < img_h:
            self.add_page()

        self._hdr("MACD 趋势 + 相似历史形态")
        self.image(chart_macd_path, x=MARGIN, w=CW)
        self.ln(4)

    def page_similar(self, similar: dict[str, Any] | None) -> None:
        if not similar or not similar.get("matches"):
            return

        matches = similar["matches"]
        s = similar.get("summary") or {}
        total = s.get("total", 0)
        win = s.get("win_count", 0)
        avg = s.get("avg_return_20d", 0)
        sim_v = s.get("verdict", "")

        self._need(20)
        vc = CLR_GREEN if sim_v == "偏多" else (CLR_RED if sim_v == "偏空" else CLR_ORANGE)
        self._hdr(f"历史相似形态详情: {win}/{total} 上涨, 平均 {avg:+.1f}% — {sim_v}", vc)

        # mini table — 只占少量空间
        self._need(6 * len(matches) + 10)
        cols = [40, 36, 24, 28, 26, 26]
        headers = ["名称", "代码", "日期", "相似度", "20日收益", "最大涨幅"]
        self._use("B", 7)
        self.set_fill_color(*CLR_DARK)
        self.set_text_color(*CLR_WHITE)
        for h, w in zip(headers, cols):
            self.cell(w, 5.5, h, fill=True, align="C")
        self.ln()

        for i, m in enumerate(matches):
            fwd = m.get("forward_return_20d", 0) or 0
            gain = m.get("forward_max_gain", 0) or 0
            rc = CLR_GREEN if fwd > 0 else CLR_RED
            if i % 2 == 0:
                self.set_fill_color(*CLR_BG_LIGHT)
            else:
                self.set_fill_color(*CLR_WHITE)
            self._use("", 7)
            row = [
                (m.get("name", "") or "")[:6],
                m["ts_code"],
                (m.get("match_date", "") or "")[5:],
                f'{m.get("similarity", 0):.0%}',
                f"{fwd:+.1f}%",
                f"{gain:+.1f}%",
            ]
            for v, w in zip(row, cols):
                self.set_text_color(*rc)
                self.cell(w, 5, v, fill=True, align="C")
            self.ln()
        self.ln(2)
        self._p("提示: 发送「选股 代码」到 Z-Plan 可查看匹配股详细分析", size=7, color=CLR_MUTED)

    def page_recommendation(self, report: dict[str, Any], verdict: str) -> None:
        advice = report.get("投资建议") or {}
        rec = advice.get("操作建议", "观望")
        signals = (report.get("modules", {}).get("4_股价分析", {}).get("关键信号") or [])
        scenarios = advice.get("走势应对") or []

        self._need(30)
        self._hdr("操作建议与走势应对")

        # 建议标签
        vc = CLR_GREEN if verdict in ("看多", "偏多") else (CLR_RED if verdict in ("看空", "偏空") else CLR_ORANGE)
        self._badge(rec, vc)
        self.ln(9)

        if signals:
            self._p(f"关键信号: {'; '.join(str(x) for x in signals[:4])}", size=8)

        if scenarios:
            self.ln(2)
            self._use("B", 10)
            self.set_text_color(*CLR_DARK)
            self.cell(0, 5, "走势应对:", new_x="LMARGIN", new_y="NEXT")
            for sc in scenarios[:3]:
                self._p(str(sc)[:200], size=8)

    def page_footer(self) -> None:
        self.set_y(-18)
        self._use("", 6)
        self.set_text_color(*CLR_MUTED)
        self.cell(0, 4, f"Z-Plan 选股系统  |  {self.ts_code}  |  {self.as_of}  |  仅供参考，不构成投资建议", align="C")


# ── 主入口 ──────────────────────────────────────────────────────


def generate_pdf_report(
    ts_code: str,
    *,
    report: dict[str, Any] | None = None,
    llm_brief: dict[str, Any] | None = None,
    chart_path: str | None = None,
    chart_macd_path: str | None = None,
    price_levels: dict[str, float | None] | None = None,
    similar_patterns: dict[str, Any] | None = None,
    risk_flags: list[str] | None = None,
    output_dir: str | None = None,
) -> str:
    if report is None:
        raise ValueError("report 不能为空")

    meta = report.get("meta") or {}
    name = meta.get("name") or ts_code
    as_of = report.get("as_of") or str(date.today())

    # 简易 verdict（与 wechat_pick._synthesize_verdict 保持一致即可）
    advice = report.get("投资建议") or {}
    m4 = report.get("modules", {}).get("4_股价分析", {})
    tech_v = m4.get("技术面结论", "中性")
    rec = advice.get("操作建议", "观望")
    rule_s = advice.get("综合推荐分", 50)
    tech_score = m4.get("技术得分")

    verdict = "观望"
    if tech_v == "偏多" and rec in ("强烈关注", "关注"):
        verdict = "看多"
    elif tech_v == "偏多":
        verdict = "偏多"
    elif tech_v == "偏空" and rec in ("谨慎", "回避"):
        verdict = "看空"
    elif tech_v == "偏空":
        verdict = "偏空"

    parts = [f"规则 {rule_s} 分", f"技术面 {tech_score} ({tech_v})"]
    if rec:
        parts.append(f"操作建议: {rec}")
    detail = "  |  ".join(parts)

    # ── 构建 PDF ──
    pdf = ReportPDF(ts_code, name, as_of)
    pdf.add_page()

    # 第 1 页：封面 + 走势图 + 价位
    pdf.page_cover(verdict, detail)
    pdf.page_chart(chart_path or "")
    if price_levels:
        pdf.page_prices(price_levels)

    # 后续内容自动分页
    pdf.page_llm(report, llm_brief)
    pdf.page_risks(report, risk_flags)
    # MACD + 相似形态画廊图（优先图表，辅助文字详情在后）
    pdf.page_macd_chart(chart_macd_path or "")
    pdf.page_similar(similar_patterns)
    pdf.page_recommendation(report, verdict)

    # 每页页脚
    # fpdf2 的 footer() 对每页自动调用，此处用最后一页也显示的方案
    total = pdf.pages_count
    for n in range(1, total + 1):
        pdf.page = n
        pdf.set_y(-18)
        pdf._use("", 6)
        pdf.set_text_color(*CLR_MUTED)
        pdf.cell(0, 4, f"Z-Plan 选股系统  |  {ts_code}  |  {as_of}  |  仅供参考", align="C")

    # 保存
    if output_dir is None:
        from zplan_shared.config import ZPLAN_ROOT
        output_dir = os.path.join(ZPLAN_ROOT, "reports")
    os.makedirs(output_dir, exist_ok=True)
    as_of_str = as_of.replace("-", "")[:8]
    safe_name = "".join(c for c in name if c not in r'\/:*?"<>|').strip()
    fname = f"{ts_code}_{safe_name}_{as_of_str}.pdf"
    output_path = os.path.join(output_dir, fname)

    pdf.output(output_path)
    logger.info("PDF 报告已生成: %s (%d 页)", output_path, total)
    return output_path
