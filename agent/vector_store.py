"""Exact cosine search over an atomic, named knowledge collection."""

import asyncio
import math
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Protocol

import psycopg


@dataclass(frozen=True)
class CollectionSpec:
    model: str
    dimension: int
    generation: str


class VectorStoreError(RuntimeError):
    pass


def checked_vector(values, dimension: int) -> list[float]:
    if not isinstance(values, (list, tuple)) or len(values) != dimension:
        raise VectorStoreError("INVALID_VECTOR")
    if any(type(value) not in (float, int) for value in values):
        raise VectorStoreError("INVALID_VECTOR")
    try:
        result = [float(value) for value in values]
    except OverflowError:
        raise VectorStoreError("INVALID_VECTOR") from None
    norm = math.hypot(*result)
    if not math.isfinite(norm) or norm == 0:
        raise VectorStoreError("INVALID_VECTOR")
    return result


def _unit(values, dimension):
    values = checked_vector(values, dimension)
    norm = math.hypot(*values)
    return tuple(v / norm for v in values)


def _validate_publication(spec, vectors):
    if (not spec.model or not spec.generation or type(spec.dimension) is not int
            or not 1 <= spec.dimension <= 4096 or not vectors
            or any(not isinstance(k, str) or not k or len(k) > 128 for k in vectors)):
        raise VectorStoreError("INVALID_COLLECTION")
    return {key: _unit(vector, spec.dimension) for key, vector in vectors.items()}


class VectorStore(Protocol):
    async def publish(self, spec: CollectionSpec, vectors: dict[str, list[float]]) -> None: ...
    async def search(self, spec: CollectionSpec, query: Sequence[float]) -> list[tuple[str, float]]: ...


class MemoryVectorStore:
    """Explicit process-local component adapter, never a pgvector fallback."""

    def __init__(self):
        self._snapshot: tuple[CollectionSpec, dict[str, tuple[float, ...]]] | None = None

    async def publish(self, spec: CollectionSpec, vectors: dict[str, list[float]]) -> None:
        self._snapshot = spec, _validate_publication(spec, vectors)

    async def search(self, spec: CollectionSpec, query: Sequence[float]) -> list[tuple[str, float]]:
        snapshot = self._snapshot
        if snapshot is None:
            raise VectorStoreError("COLLECTION_NOT_PUBLISHED")
        if snapshot[0] != spec:
            raise VectorStoreError("COLLECTION_MISMATCH")
        norm_query = _unit(query, spec.dimension)
        scores = [(key, math.fsum(a * b for a, b in zip(vector, norm_query)))
                  for key, vector in snapshot[1].items()]
        return sorted(scores, key=lambda result: (-result[1], result[0]))


def _pg_vector(unit_vector):
    return "[" + ",".join(format(value, ".17g") for value in unit_vector) + "]"


class PgVectorStore:
    """Persisted exact cosine; initialize and replace via one publish transaction."""

    def __init__(self, dsn: str, collection: str = "after_sales_policy"):
        if not collection or len(collection) > 128:
            raise ValueError("invalid collection name")
        self._dsn = dsn
        self._collection = collection

    def _connect(self):
        # Synchronous libpq in a worker works under both Windows' Proactor loop
        # and the container's selector loop. All SQL has a server-side timeout.
        return psycopg.connect(
            self._dsn, connect_timeout=3, options="-c statement_timeout=5000",
        )

    async def publish(self, spec: CollectionSpec, vectors: dict[str, list[float]]) -> None:
        validated = _validate_publication(spec, vectors)
        await asyncio.to_thread(self._publish, spec, validated)

    def _publish(self, spec, validated):
        try:
            with self._connect() as conn:
                with conn.cursor() as cur:
                    cur.execute("CREATE EXTENSION IF NOT EXISTS vector")
                    cur.execute("""
                        CREATE TABLE IF NOT EXISTS rag_collections (
                          name text PRIMARY KEY, model text NOT NULL, dimension integer NOT NULL,
                          generation text NOT NULL
                        )
                    """)
                    cur.execute("""
                        CREATE TABLE IF NOT EXISTS rag_vectors (
                          collection text REFERENCES rag_collections(name) ON DELETE CASCADE,
                          chunk_id text, embedding vector NOT NULL,
                          PRIMARY KEY (collection, chunk_id)
                        )
                    """)
                    # Acquire the collection row lock before writing any vectors.
                    cur.execute("""
                        INSERT INTO rag_collections (name, model, dimension, generation)
                        VALUES (%s, %s, %s, %s)
                        ON CONFLICT (name) DO UPDATE SET model = excluded.model,
                            dimension = excluded.dimension, generation = excluded.generation
                        """, (self._collection, spec.model, spec.dimension, spec.generation))
                    cur.execute("DELETE FROM rag_vectors WHERE collection = %s", (self._collection,))
                    cur.executemany("""
                        INSERT INTO rag_vectors (collection, chunk_id, embedding)
                        VALUES (%s, %s, %s::vector)
                        """, [(self._collection, key, _pg_vector(vector))
                              for key, vector in validated.items()])
        except (psycopg.Error, OSError, ValueError):
            raise VectorStoreError("PGVECTOR_UNAVAILABLE") from None

    async def search(self, spec: CollectionSpec, query: Sequence[float]) -> list[tuple[str, float]]:
        unit_query = _pg_vector(_unit(query, spec.dimension))
        return await asyncio.to_thread(self._search, spec, unit_query)

    def _search(self, spec, unit_query):
        try:
            with self._connect() as conn:
                with conn.cursor() as cur:
                    # Read metadata and vectors from the same committed snapshot.
                    cur.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY")
                    cur.execute("""
                        SELECT model, dimension, generation FROM rag_collections WHERE name = %s
                        """, (self._collection,))
                    metadata = cur.fetchone()
                    if metadata is None:
                        raise VectorStoreError("COLLECTION_NOT_PUBLISHED")
                    if metadata != (spec.model, spec.dimension, spec.generation):
                        raise VectorStoreError("COLLECTION_MISMATCH")
                    cur.execute("""
                        SELECT chunk_id, 1 - (embedding <=> %s::vector) AS score
                        FROM rag_vectors WHERE collection = %s ORDER BY score DESC, chunk_id
                        """, (unit_query, self._collection))
                    return [(key, float(score)) for key, score in cur.fetchall()]
        except (psycopg.Error, OSError, ValueError):
            raise VectorStoreError("PGVECTOR_UNAVAILABLE") from None
