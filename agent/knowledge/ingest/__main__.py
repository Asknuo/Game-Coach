"""CLI 入口：python -m knowledge.ingest"""

import logging

from knowledge.ingest.pipeline import Ingestor

logger = logging.getLogger(__name__)

if __name__ == "__main__":
    ingestor = Ingestor()
    ingestor.ingest_all()
    logger.info("ingest complete")
