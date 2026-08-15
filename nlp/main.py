from __future__ import annotations

from typing import Annotated

from fastapi import FastAPI
from pydantic import BaseModel
from pydantic import Field

from nlp.embeddings import embed_texts
from nlp.sentiment import classify_sentiment
from nlp.toxicity import toxicity_scores

app = FastAPI(title="Yuvi Bot v2 NLP", version="0.1.0")


class BatchRequest(BaseModel):
    # NLP выполняется на CPU; ограничиваем размер одного запроса, чтобы
    # публично доступный endpoint не мог занять процесс гигантским батчем.
    texts: list[Annotated[str, Field(max_length=4096)]] = Field(max_length=128)


@app.get("/health")
async def health() -> dict[str, str]:
    return {"status": "ok", "service": "nlp"}


@app.post("/classify/batch")
async def classify_batch(request: BatchRequest) -> dict[str, list[dict]]:
    sentiments = classify_sentiment(request.texts)
    toxicities = toxicity_scores(request.texts)
    results = [
        {
            "sentiment_label": sentiment["sentiment_label"],
            "sentiment_score": sentiment["sentiment_score"],
            "toxicity_score": toxicity_score,
        }
        for sentiment, toxicity_score in zip(sentiments, toxicities)
    ]
    return {"results": results}


@app.post("/embed/batch")
async def embed_batch(request: BatchRequest) -> dict[str, list[list[float]]]:
    return {"embeddings": embed_texts(request.texts)}
