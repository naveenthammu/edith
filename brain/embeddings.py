"""Optional local semantic retrieval. No downloads, cloud calls or filesystem crawling.

Nomic v1.5 needs task prefixes; vectors are bound to its installed manifest digest.
The trusted local Ollama server must also run with OLLAMA_NO_CLOUD=1.
"""
import math
import os
import re
from dataclasses import dataclass

from brain.hub import HubError
from brain.llm import LLMUnavailable, OllamaLocal


class EmbeddingError(RuntimeError):
    pass


@dataclass(frozen=True)
class EmbeddingBatch:
    model_id: str
    vectors: list[list[float]]


class OllamaEmbeddings:
    MODEL = 'nomic-embed-text:v1.5'
    BATCH_SIZE = 8

    def __init__(self, url='http://127.0.0.1:11434', model=MODEL, timeout=45):
        # Prefixes and dimensionality are model-specific; do not silently apply
        # Nomic's protocol to an arbitrary embedding or chat model.
        if model != self.MODEL:
            raise EmbeddingError('This embedding adapter requires nomic-embed-text:v1.5.')
        try:
            self._local = OllamaLocal(url, model, timeout)
        except ValueError:
            raise EmbeddingError('Configure a literal loopback Ollama URL and valid timeout.') from None

    @classmethod
    def from_env(cls):
        model = os.getenv('EDITH_EMBEDDING_MODEL', '').strip()
        if not model:
            return None
        return cls(os.getenv('EDITH_OLLAMA_URL', 'http://127.0.0.1:11434'), model)

    def _model_id(self):
        installed = self._local._require_local_model()
        digest = installed.get('digest', '')
        if not isinstance(digest, str) or not re.fullmatch(r'(?:sha256:)?[0-9a-f]{64}', digest):
            raise EmbeddingError('The installed embedding model has no valid manifest digest.')
        return f'ollama:{self.MODEL}@sha256:{digest.removeprefix("sha256:")}'

    def embed(self, texts: list[str], *, purpose: str) -> EmbeddingBatch:
        if (purpose not in {'document', 'query'} or not isinstance(texts, list)
                or not 1 <= len(texts) <= self.BATCH_SIZE
                or any(not isinstance(t, str) or not t.strip() or len(t.encode('utf-8')) > 8192 for t in texts)):
            raise EmbeddingError('Embedding input must be 1 to 8 nonempty, bounded text chunks with a task purpose.')
        try:
            model_id = self._model_id()  # Before any private input crosses the local socket.
            value = self._local._request('/api/embed', {
                'model': self.MODEL, 'input': [f'search_{purpose}: {t}' for t in texts],
                'truncate': False, 'keep_alive': '5m',
            })
            if value.get('model') != self.MODEL or value.get('remote_host') or value.get('remote_model'):
                raise EmbeddingError('The local runtime returned an unexpected embedding model.')
            vectors = value.get('embeddings')
            if not isinstance(vectors, list) or len(vectors) != len(texts):
                raise EmbeddingError('The local runtime returned an incomplete embedding batch.')
            for vector in vectors:
                if (not isinstance(vector, list) or len(vector) != 768
                        or any(type(v) not in (int, float) or not math.isfinite(v) or abs(v) > 1e6 for v in vector)
                        or math.fsum(v * v for v in vector) <= 1e-20):
                    raise EmbeddingError('The local runtime returned an invalid embedding vector.')
            if self._model_id() != model_id:
                raise EmbeddingError('The embedding model changed during inference; the vectors were discarded.')
            return EmbeddingBatch(model_id, vectors)
        except LLMUnavailable:
            raise EmbeddingError('Local embeddings are unavailable; check the installed model and local runtime.') from None


def index_document(hub, embeddings: OllamaEmbeddings, document_id: str):
    """Only the owner can fetch chunks. Commit the complete version or nothing."""
    source = hub.document_chunks(document_id)
    document, chunks = source.get('document'), source.get('chunks')
    if (not isinstance(document, dict) or document.get('id') != document_id
            or not isinstance(document.get('source_version'), str)
            or not isinstance(chunks, list) or not 1 <= len(chunks) <= 2000):
        raise HubError('The hub returned an invalid source for indexing.')
    version = document['source_version']
    if any(not isinstance(c, dict) or not isinstance(c.get('chunk_id'), str)
           or not c['chunk_id'] or c.get('source_version') != version
           or not isinstance(c.get('content'), str) for c in chunks):
        raise HubError('The hub returned inconsistent source chunks.')
    if len({c['chunk_id'] for c in chunks}) != len(chunks):
        raise HubError('The hub returned duplicate source chunks.')
    model_id, indexed = None, []
    for start in range(0, len(chunks), embeddings.BATCH_SIZE):
        group = chunks[start:start + embeddings.BATCH_SIZE]
        batch = embeddings.embed([chunk['content'] for chunk in group], purpose='document')
        if model_id is not None and batch.model_id != model_id:
            raise EmbeddingError('The embedding model changed between batches; nothing was indexed.')
        model_id = batch.model_id
        indexed.extend({'chunk_id': chunk['chunk_id'], 'vector': vector}
                       for chunk, vector in zip(group, batch.vectors, strict=True))
    receipt = hub.store_embeddings(document_id, version, model_id, indexed)
    if (receipt.get('indexed') is not True or receipt.get('document_id') != document_id
            or receipt.get('source_version') != version or receipt.get('model_id') != model_id
            or receipt.get('chunks') != len(chunks) or receipt.get('dimensions') != 768):
        raise HubError('The hub did not return a matching index receipt. Inspect its state before retrying.')
    return receipt


def search_documents(hub, query: str, embeddings=None, limit=5):
    """A local embedding outage degrades explicitly to permission-scoped keywords."""
    if embeddings is None:
        return {**hub.search(query, limit), 'semantic_status': 'not_configured'}
    try:
        batch = embeddings.embed([query], purpose='query')
    except EmbeddingError:
        return {**hub.search(query, limit), 'semantic_status': 'local_embedding_unavailable'}
    return {**hub.search(query, limit, query_vector={'model_id': batch.model_id, 'vector': batch.vectors[0]}),
            'semantic_status': 'query_embedded_locally'}
