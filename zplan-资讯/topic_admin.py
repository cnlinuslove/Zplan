from __future__ import annotations

import argparse
from typing import Any

from sqlalchemy import select

from models import SessionLocal, TopicConfig, init_db


def list_topics(echo: bool = True) -> list[dict[str, Any]]:
    with SessionLocal() as session:
        rows = session.execute(select(TopicConfig).order_by(TopicConfig.id.asc())).scalars()
        result: list[dict[str, Any]] = []
        for row in rows:
            flag = "on" if row.enabled else "off"
            if echo:
                print(f"{row.topic_key}\t{row.display_name}\t{flag}\t{row.query}")
            result.append(
                {
                    "topic_key": row.topic_key,
                    "display_name": row.display_name,
                    "query": row.query,
                    "enabled": row.enabled,
                }
            )
        return result


def add_topic(
    topic_key: str, display_name: str, query: str, enabled: bool, echo: bool = True
) -> dict[str, Any]:
    with SessionLocal() as session:
        obj = TopicConfig(
            topic_key=topic_key,
            display_name=display_name,
            query=query,
            enabled=enabled,
        )
        session.add(obj)
        session.commit()
    if echo:
        print(f"added: {topic_key}")
    return {
        "topic_key": topic_key,
        "display_name": display_name,
        "query": query,
        "enabled": enabled,
    }


def update_topic(
    topic_key: str,
    display_name: str | None,
    query: str | None,
    enabled: bool | None,
    echo: bool = True,
) -> dict[str, Any]:
    with SessionLocal() as session:
        topic = session.execute(
            select(TopicConfig).where(TopicConfig.topic_key == topic_key)
        ).scalar_one_or_none()
        if topic is None:
            raise ValueError(f"topic not found: {topic_key}")
        if display_name is not None:
            topic.display_name = display_name
        if query is not None:
            topic.query = query
        if enabled is not None:
            topic.enabled = enabled
        session.commit()
        payload = {
            "topic_key": topic.topic_key,
            "display_name": topic.display_name,
            "query": topic.query,
            "enabled": topic.enabled,
        }
    if echo:
        print(f"updated: {topic_key}")
    return payload


def delete_topic(topic_key: str, echo: bool = True) -> dict[str, Any]:
    with SessionLocal() as session:
        topic = session.execute(
            select(TopicConfig).where(TopicConfig.topic_key == topic_key)
        ).scalar_one_or_none()
        if topic is None:
            if echo:
                print(f"not found: {topic_key}")
            return {"topic_key": topic_key, "deleted": False}
        session.delete(topic)
        session.commit()
    if echo:
        print(f"deleted: {topic_key}")
    return {"topic_key": topic_key, "deleted": True}


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Topic dynamic config management")
    sub = parser.add_subparsers(dest="cmd", required=True)

    sub.add_parser("list")

    p_add = sub.add_parser("add")
    p_add.add_argument("--topic-key", required=True)
    p_add.add_argument("--display-name", required=True)
    p_add.add_argument("--query", required=True)
    p_add.add_argument("--enabled", choices=["true", "false"], default="true")

    p_upd = sub.add_parser("update")
    p_upd.add_argument("--topic-key", required=True)
    p_upd.add_argument("--display-name")
    p_upd.add_argument("--query")
    p_upd.add_argument("--enabled", choices=["true", "false"])

    p_del = sub.add_parser("delete")
    p_del.add_argument("--topic-key", required=True)
    return parser.parse_args()


def main() -> None:
    init_db()
    args = parse_args()
    if args.cmd == "list":
        list_topics()
        return
    if args.cmd == "add":
        add_topic(
            topic_key=args.topic_key,
            display_name=args.display_name,
            query=args.query,
            enabled=args.enabled == "true",
        )
        return
    if args.cmd == "update":
        enabled = None if args.enabled is None else args.enabled == "true"
        update_topic(
            topic_key=args.topic_key,
            display_name=args.display_name,
            query=args.query,
            enabled=enabled,
        )
        return
    if args.cmd == "delete":
        delete_topic(args.topic_key)


if __name__ == "__main__":
    main()
