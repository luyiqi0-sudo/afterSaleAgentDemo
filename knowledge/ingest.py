"""Embed then atomically publish one complete policy generation."""

import argparse
import asyncio
import json

import httpx

from agent.config import Settings
from agent.provider import EMBED_BATCH_SIZE, BudgetCallback, HTTPModelProvider, ProviderError
from agent.retriever import PolicyRetriever, collection_for, parse_documents, retrieval_text
from agent.vector_store import MemoryVectorStore, PgVectorStore, VectorStore, VectorStoreError
from knowledge.source import KnowledgeSourceMismatch, verify_knowledge_source


def _store(settings: Settings, store: VectorStore | None, *, ingest: bool) -> VectorStore:
    if store is not None:
        if isinstance(store, MemoryVectorStore) and settings.vector_backend != "memory":
            raise VectorStoreError("BACKEND_MISMATCH")
        return store
    if settings.vector_backend == "pgvector":
        return PgVectorStore(settings.pg_dsn.get_secret_value())
    if not ingest:
        raise VectorStoreError("MEMORY_REQUIRES_INGEST")
    return MemoryVectorStore()


def _retriever(settings, provider, store, *, ingest):
    active_store = _store(settings, store, ingest=ingest)
    chunks = parse_documents(settings.knowledge_path)
    spec = collection_for(chunks, settings.embedding_model, settings.embedding_dimension)
    retriever = PolicyRetriever(chunks, spec, active_store, provider or HTTPModelProvider(settings),
                                min_score=settings.rag_min_score)
    return retriever, chunks


def open_retriever(settings: Settings, provider=None, *, store: VectorStore | None = None) -> PolicyRetriever:
    """Load the local source metadata; every query checks the configured index generation."""
    retriever, _ = _retriever(settings, provider, store, ingest=False)
    return retriever


async def ingest_knowledge(settings: Settings, provider=None, *, store: VectorStore | None = None,
                           budget: BudgetCallback | None = None) -> PolicyRetriever:
    retriever, chunks = _retriever(settings, provider, store, ingest=True)
    vectors = {}
    batch = []
    byte_count = 0

    async def embed_batch():
        values = await retriever.provider.embed([text for _, text in batch], budget=budget)
        if len(values) != len(batch):
            raise ProviderError("INVALID_OUTPUT")
        vectors.update((chunk_id, vector) for (chunk_id, _), vector in zip(batch, values))

    for c in chunks:
        text = retrieval_text(c)
        units = len(json.dumps(text, ensure_ascii=False).encode("utf-8")) + 1
        if batch and (len(batch) >= EMBED_BATCH_SIZE or byte_count + units > 7_000):
            await embed_batch()
            batch.clear()
            byte_count = 0
        batch.append((c.chunk_id, text))
        byte_count += units
    if batch:
        await embed_batch()
    await retriever.store.publish(retriever.spec, vectors)
    return retriever


async def _run(args) -> int:
    settings = Settings(**({"vector_backend": args.backend} if args.backend else {}))
    events = []
    try:
        verify_knowledge_source(settings.dataset_root, settings.knowledge_path)
        async with httpx.AsyncClient() as client:
            provider = HTTPModelProvider(settings, client, events.append)
            indexed = await ingest_knowledge(settings, provider)
            output = {"backend": settings.vector_backend, "model": indexed.spec.model,
                      "dimension": indexed.spec.dimension, "generation": indexed.spec.generation,
                      "provider_calls": events}
            if args.query:
                conditions = {"channel": "online", "region": "CN"} if args.demo_scope else {}
                result = await indexed.retrieve(args.query, query_date=settings.anchor_date, conditions=conditions)
                output["query"] = {"status": result.status, "error_code": result.error_code,
                                   "missing": result.missing,
                                   "evidence": [{"doc_id": e.doc_id, "chunk_id": e.chunk_id, "score": e.score}
                                                for e in result.evidences]}
        print(json.dumps(output, ensure_ascii=True))
    except (OSError, ValueError, ProviderError, VectorStoreError) as error:
        print(json.dumps({"status": "FAILED", "error_code": (str(error) if isinstance(
            error, (ProviderError, VectorStoreError, KnowledgeSourceMismatch)) else "INVALID_KNOWLEDGE"), "provider_calls": events}))
        return 1
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--backend", choices=("pgvector", "memory"))
    parser.add_argument("--query", help="Optional policy question to verify immediately after publish")
    parser.add_argument("--demo-scope", action="store_true", help="Apply explicit synthetic CN/online conditions")
    return asyncio.run(_run(parser.parse_args()))


if __name__ == "__main__":
    raise SystemExit(main())
