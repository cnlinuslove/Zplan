"""机器人概念核心标的深度研究：产品竞争力 + 潜力分析。"""
import json, sys
sys.path.insert(0, "/Users/richard/my_stock_ai/zplan-选股/src")
sys.path.insert(0, "/Users/richard/my_stock_ai/zplan-共享/src")

from pick_agent.llm_research import research_with_llm, format_llm_report_markdown

TARGETS = [
    ("300124", "汇川技术", "伺服/变频器/PLC，工业自动化龙头，人形机器人核心供应链"),
    ("002126", "银轮股份", "热管理龙头，机器人散热+新能源车双驱动"),
    ("603416", "信捷电气", "PLC/伺服/机器视觉，小型自动化方案商"),
    ("603915", "国茂股份", "减速机龙头，机器人关节核心传动部件"),
    ("603166", "福达股份", "精密锻件+新能源电驱齿轮，机器人传动供应链"),
]

def main():
    for ts_code, name, highlight in TARGETS:
        print(f"\n{'='*80}")
        print(f"  {ts_code} {name} — {highlight}")
        print(f"{'='*80}")
        try:
            report = research_with_llm(ts_code)
            # 只打印关键模块
            llm = report.get("llm", {})
            advice = report.get("投资建议", {})

            print(f"\n📊 综合评分: LLM {advice.get('LLM综合分')} | 规则引擎 {advice.get('规则引擎综合分')}")
            print(f"💰 建议: {advice.get('操作建议')} | 买入 {advice.get('建议买入价')} | 目标 {advice.get('目标价')} | 止损 {advice.get('止损参考')}")

            print(f"\n🏢 公司定位与核心业务:")
            print(f"   {llm.get('company_summary', '-')}")

            print(f"\n📈 投资总结:")
            print(f"   {advice.get('总结', '-')}")

            print(f"\n🚀 机遇:")
            for opp in llm.get('opportunities', [])[:3]:
                print(f"   + {opp}")

            print(f"\n⚠️ 风险:")
            for risk in llm.get('risks', [])[:3]:
                print(f"   - {risk}")

            print(f"\n🎯 走势应对策略:")
            for s in advice.get('走势应对', [])[:3]:
                print(f"   → {s}")

            print(f"\n📋 数据盲区:")
            for g in report.get('data_gaps_for_other_agents', [])[:3]:
                print(f"   ? {g}")

        except Exception as e:
            print(f"  ❌ 失败: {e}")

if __name__ == '__main__':
    main()
