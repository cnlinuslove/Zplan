"""
按 Topic 浏览已入库的 news_runs 与 news_items_raw。

启动：在项目根目录执行
  ./.venv/bin/streamlit run viewer_app.py
或：make ui
"""
from __future__ import annotations

from datetime import datetime, timedelta

import pandas as pd
import streamlit as st
from sqlalchemy import desc, func, select

from models import NewsItemRaw, NewsRun, SessionLocal, TopicConfig, init_db

st.set_page_config(page_title="Z-Plan 资讯库", layout="wide", initial_sidebar_state="expanded")


@st.cache_data(ttl=30)
def load_topics() -> list[dict]:
    with SessionLocal() as session:
        rows = session.execute(select(TopicConfig).order_by(TopicConfig.topic_key.asc())).scalars().all()
        return [
            {"topic_key": r.topic_key, "display_name": r.display_name, "enabled": r.enabled, "query": r.query}
            for r in rows
        ]


def load_runs(topic_key: str, limit: int, days: int | None) -> pd.DataFrame:
    with SessionLocal() as session:
        stmt = select(NewsRun).where(NewsRun.topic_key == topic_key).order_by(desc(NewsRun.created_at))
        if days is not None and days > 0:
            since = datetime.utcnow() - timedelta(days=days)
            stmt = stmt.where(NewsRun.created_at >= since)
        stmt = stmt.limit(limit)
        rows = list(session.execute(stmt).scalars().all())
    if not rows:
        return pd.DataFrame()
    return pd.DataFrame(
        [
            {
                "id": r.id,
                "window_start": r.window_start,
                "window_end": r.window_end,
                "sentiment": r.sentiment or "",
                "created_at": r.created_at,
                "summary_preview": (r.summary[:200] + "…") if len(r.summary) > 200 else r.summary,
                "full_summary": r.summary,
            }
            for r in rows
        ]
    )


def load_items_for_run(run_id: int, source_filter: str) -> pd.DataFrame:
    with SessionLocal() as session:
        stmt = (
            select(NewsItemRaw)
            .where(NewsItemRaw.run_id == run_id)
            .order_by(desc(NewsItemRaw.published_at))
        )
        if source_filter != "全部":
            stmt = stmt.where(NewsItemRaw.source == source_filter)
        rows = list(session.execute(stmt).scalars().all())
    if not rows:
        return pd.DataFrame()
    return pd.DataFrame(
        [
            {
                "id": r.id,
                "source": r.source,
                "post_id": r.post_id,
                "author": r.author or "",
                "published_at": r.published_at,
                "text": r.text,
                "url": r.url or "",
            }
            for r in rows
        ]
    )


def count_stats(topic_key: str) -> tuple[int, int]:
    with SessionLocal() as session:
        n_runs = session.execute(
            select(func.count()).select_from(NewsRun).where(NewsRun.topic_key == topic_key)
        ).scalar_one()
        n_items = session.execute(
            select(func.count())
            .select_from(NewsItemRaw)
            .join(NewsRun, NewsItemRaw.run_id == NewsRun.id)
            .where(NewsRun.topic_key == topic_key)
        ).scalar_one()
    return int(n_runs or 0), int(n_items or 0)


def main() -> None:
    init_db()
    st.title("Z-Plan 资讯库")
    st.caption("按 Topic 查看已抓取并入库的摘要与原始推文（数据来自本地 DB_URL / PostgreSQL）。")

    topics = load_topics()
    if not topics:
        st.warning("数据库中暂无 topic，请先运行 `openclaw_bridge.py run-once` 或 `daily_update.py`。")
        return

    sidebar = st.sidebar
    sidebar.header("筛选")
    labels = [f"{t['display_name']} ({t['topic_key']})" for t in topics]
    keys = [t["topic_key"] for t in topics]
    idx = sidebar.selectbox("Topic", range(len(keys)), format_func=lambda i: labels[i])
    topic_key = keys[idx]
    topic_meta = topics[idx]

    days = sidebar.selectbox("时间范围", [None, 1, 3, 7, 30, 90], format_func=lambda x: "全部" if x is None else f"最近 {x} 天")
    limit = sidebar.slider("最多展示轮次", min_value=20, max_value=500, value=100, step=20)
    source_filter = sidebar.selectbox("原始推文来源", ["全部", "x_api", "x_placeholder"])

    n_runs, n_items = count_stats(topic_key)
    sidebar.metric("该 Topic 摘要轮次", n_runs)
    sidebar.metric("该 Topic 原始条数", n_items)
    if sidebar.button("刷新列表缓存"):
        st.cache_data.clear()
        st.rerun()
    with sidebar.expander("当前 Topic 查询串"):
        st.code(topic_meta.get("query", ""), language=None)

    df = load_runs(topic_key, limit=limit, days=days)
    if df.empty:
        st.info("该条件下暂无 news_runs 记录。")
        return

    st.subheader("摘要轮次（news_runs）")
    st.dataframe(
        df[["id", "created_at", "window_start", "window_end", "sentiment", "summary_preview"]],
        use_container_width=True,
        hide_index=True,
    )

    run_ids = [int(x) for x in df["id"].tolist()]
    pick = st.selectbox("选择一轮查看原始推文", run_ids, format_func=lambda rid: f"run_id={rid} · {df.loc[df['id']==rid,'created_at'].iloc[0]}")

    st.subheader(f"原始推文（run_id={pick}）")
    items_df = load_items_for_run(pick, source_filter=source_filter)
    if items_df.empty:
        st.info("该轮下无原始推文，或来源筛选过严。")
    else:
        st.dataframe(items_df, use_container_width=True, hide_index=True)
        sel = st.selectbox("展开单条正文", items_df["id"].tolist(), format_func=lambda i: f"id={i}")
        row = items_df[items_df["id"] == sel].iloc[0]
        st.text_area("全文", value=str(row["text"]), height=220)
        if row.get("url"):
            st.link_button("在 X 打开", str(row["url"]))


if __name__ == "__main__":
    main()
