import json
from pathlib import Path

from data_asset_agents.text2sql.models import HistoricalSQLExample


class HistoricalSQLRepository:
    """Small local certified-SQL repository; pgvector can replace its ranking adapter later."""

    def __init__(self, path: Path | str = "data/historical_sql/examples.json") -> None:
        self.path = Path(path)

    def search(self, question: str, limit: int = 3) -> list[HistoricalSQLExample]:
        if not self.path.exists():
            return []
        records = json.loads(self.path.read_text(encoding="utf-8"))
        question_tokens = set(question.lower())
        examples: list[HistoricalSQLExample] = []
        for record in records:
            overlap = len(question_tokens & set(record["question"].lower()))
            denominator = max(len(question_tokens), 1)
            examples.append(
                HistoricalSQLExample(**record, similarity=round(overlap / denominator, 4))
            )
        return sorted(examples, key=lambda item: item.similarity, reverse=True)[:limit]

