"""Tool 注册表。"""

from collections.abc import Iterable

from .task_tools import TaskTool


class ToolRegistry:
    """维护模型可见的 Tool Schema 与服务端执行对象之间的唯一映射。"""

    def __init__(self, tools: Iterable[TaskTool]) -> None:
        self._tools: dict[str, TaskTool] = {}
        for tool in tools:
            # 启动阶段立即拒绝重名，避免运行时路由到错误实现。
            if tool.name in self._tools:
                raise ValueError(f"Tool 重复注册: {tool.name}")
            self._tools[tool.name] = tool

    def get(self, name: str) -> TaskTool:
        """按模型返回的名称取得唯一的 Tool 执行对象。"""

        try:
            return self._tools[name]
        except KeyError as exc:
            raise KeyError(f"未注册的 Tool: {name}") from exc

    def llm_definitions(self) -> list[dict]:
        """导出可直接传给 ChatModel.bind_tools 的函数定义。"""

        return [tool.llm_definition() for tool in self._tools.values()]
