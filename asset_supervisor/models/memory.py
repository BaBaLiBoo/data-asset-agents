"""传给需求理解智能体的有限会话记忆。"""

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class ConversationMemory:
    """从同一未删除会话的数据库消息中提取出的受控摘要。"""

    prompt: str = ""
    message_count: int = 0
    char_count: int = 0

    @property
    def is_empty(self) -> bool:
        return not self.prompt
