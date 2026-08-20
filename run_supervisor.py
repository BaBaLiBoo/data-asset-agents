"""命令行联调入口。"""

import asyncio
import json

from asset_supervisor import build_supervisor
from asset_supervisor.models import ChatRequest


EXIT_COMMANDS = {"exit", "quit", "q", "退出"}


async def run() -> None:
    supervisor = build_supervisor()
    print("数据资产总控智能体控制台已启动。")
    print("请输入需求；输入 exit/退出结束。")

    while True:
        try:
            user_query = input("\n用户 > ").strip()
        except (EOFError, KeyboardInterrupt):
            print("\n控制台已退出。")
            break
        if not user_query:
            continue
        if user_query.lower() in EXIT_COMMANDS:
            break

        response = await supervisor.handle(
            ChatRequest(
                conversation_id="cli_conversation",
                message=user_query,
            )
        )
        print(
            json.dumps(
                response.model_dump(mode="json", by_alias=True),
                ensure_ascii=False,
                indent=2,
            )
        )


if __name__ == "__main__":
    asyncio.run(run())
