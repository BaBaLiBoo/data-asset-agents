from __future__ import annotations

from .embedding import OpenAICompatibleEmbeddingClient
from .models import AssetInput, LayerScore
from .similarity_utils import weighted_available


COMPONENT_WEIGHTS = {
    "name": 0.30,
    "business": 0.35,
    "schema": 0.25,
    "grain": 0.10,
}


class SemanticComparator:
    def __init__(self, embedding_client: OpenAICompatibleEmbeddingClient) -> None:
        self.embedding_client = embedding_client

    @staticmethod
    def text_blocks(asset: AssetInput) -> dict[str, str]:
        business = " ".join(
            value
            for value in (
                asset.description,
                asset.metric_name,
                asset.metric_definition,
            )
            if value
        )
        schema_items: list[str] = []
        for table in asset.tables:
            schema_items.extend(value for value in (table.name, table.cn_name, table.comment) if value)
            for column in table.columns:
                schema_items.extend(
                    value
                    for value in (
                        column.name,
                        column.cn_name,
                        column.comment,
                        column.role,
                    )
                    if value
                )
        return {
            "name": asset.asset_name,
            "business": business,
            "schema": " ".join(schema_items),
            "grain": " ".join([asset.business_domain, *asset.declared_grain]),
        }

    def compare(self, left: AssetInput, right: AssetInput) -> LayerScore:
        left_blocks = self.text_blocks(left)
        right_blocks = self.text_blocks(right)
        all_texts = [
            text
            for key in COMPONENT_WEIGHTS
            for text in (left_blocks[key], right_blocks[key])
            if text.strip()
        ]
        vectors = self.embedding_client.embed_many(all_texts)
        components: dict[str, float | None] = {}
        for key in COMPONENT_WEIGHTS:
            left_text = self.embedding_client._clean(left_blocks[key])
            right_text = self.embedding_client._clean(right_blocks[key])
            if not left_text or not right_text:
                components[key] = None
                continue
            components[key] = max(
                0.0,
                min(1.0, float(vectors[left_text] @ vectors[right_text])),
            )
        score, quality = weighted_available(components, COMPONENT_WEIGHTS)
        if score is None:
            return LayerScore(
                available=False,
                quality=0.0,
                components=components,
                warnings=["语义文本全部缺失"],
            )
        strongest = max(
            ((key, value) for key, value in components.items() if value is not None),
            key=lambda item: item[1],
        )
        return LayerScore(
            score=score,
            quality=quality,
            available=True,
            components=components,
            evidence=[
                f"在线Embedding模型最高语义项：{strongest[0]}={strongest[1]:.3f}"
            ],
        )
