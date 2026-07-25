"""知识库摄入包 — 数据抓取后的向量化摄入流水线.

运行方式: python -m knowledge.ingest
"""

from knowledge.ingest.formatters import ChampionFormatter, ItemFormatter
from knowledge.ingest.guide_generator import GuideGenerator
from knowledge.ingest.pipeline import Ingestor

__all__ = ["Ingestor", "ItemFormatter", "ChampionFormatter", "GuideGenerator"]
