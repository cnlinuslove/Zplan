import sys

if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "watch":
        from pick_agent.watch_cli import main as watch_main

        watch_main(sys.argv[2:])
    elif len(sys.argv) > 1 and sys.argv[1] == "screen":
        from pick_agent.screen_cli import main as screen_main

        rest = sys.argv[2:]
        if rest and not rest[0].startswith("-") and rest[0] in (
            "run",
            "concepts",
            "sync-concept",
        ):
            screen_main(rest)
        else:
            screen_main(["run", *rest])
    elif len(sys.argv) > 1 and sys.argv[1] == "export-top":
        import argparse
        from pick_agent.export_run import export_llm_top_excel

        p = argparse.ArgumentParser(description="导出 LLM Top300 简评 Excel")
        p.add_argument("--run-id", type=int, default=None)
        p.add_argument("-o", "--output", type=str, default=None)
        args = p.parse_args(sys.argv[2:])
        path = export_llm_top_excel(args.run_id, output=args.output)
        print(f"已导出：{path}")
    elif len(sys.argv) > 1 and sys.argv[1] in (
        "init-rule",
        "llm-top",
        "pipeline",
        "pipeline-full",
        "deep-top",
    ):
        from pick_agent.init_cli import main as init_main

        init_main([sys.argv[1], *sys.argv[2:]])
    else:
        from pick_agent.main import main as pick_main

        pick_main()
