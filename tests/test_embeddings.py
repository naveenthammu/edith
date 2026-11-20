import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from brain.embeddings import EmbeddingError, OllamaEmbeddings, index_document, search_documents


MODEL = 'nomic-embed-text:v1.5'
DIGEST = 'a' * 64


@pytest.fixture
def embedding_server():
    seen = []
    state = {'digest': DIGEST, 'remote': False, 'vectors': None, 'change_digest': False}

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            seen.append((self.path, None))
            self.respond({'models': [{'name': MODEL, 'digest': state['digest'],
                                      'remote_host': 'https://example.com' if state['remote'] else ''}]})

        def do_POST(self):
            body = json.loads(self.rfile.read(int(self.headers['Content-Length'])))
            seen.append((self.path, body))
            if self.path == '/api/show':
                self.respond({'details': {'format': 'gguf'}, 'capabilities': ['embedding']})
            elif self.path == '/api/embed':
                if state['change_digest']:
                    state['digest'] = 'b' * 64
                vectors = state['vectors']
                self.respond({'model': MODEL, 'embeddings': vectors if vectors is not None else
                              [[1.0] + [0.0] * 767 for _ in body['input']]})
            else:
                self.send_error(404)

        def respond(self, value):
            self.send_response(200)
            self.end_headers()
            self.wfile.write(json.dumps(value).encode())

        def log_message(self, *args):
            pass

    server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
    worker = threading.Thread(target=server.serve_forever, daemon=True)
    worker.start()
    yield f'http://127.0.0.1:{server.server_port}', seen, state
    server.shutdown()
    server.server_close()
    worker.join()


def test_embeddings_bind_installed_digest_prefix_tasks_and_never_truncate(embedding_server, monkeypatch):
    url, seen, state = embedding_server
    monkeypatch.setenv('HTTP_PROXY', 'http://127.0.0.1:1')
    monkeypatch.setenv('NO_PROXY', '')
    model = OllamaEmbeddings(url)
    documents = model.embed(['Water every Thursday.'], purpose='document')
    query = model.embed(['When should I irrigate?'], purpose='query')
    assert documents.model_id == query.model_id == f'ollama:{MODEL}@sha256:{DIGEST}'
    assert len(documents.vectors[0]) == 768
    sent = [body for path, body in seen if path == '/api/embed']
    assert sent[0]['input'] == ['search_document: Water every Thursday.']
    assert sent[1]['input'] == ['search_query: When should I irrigate?']
    assert all(body['truncate'] is False for body in sent)
    assert not any('pull' in path or 'chat' in path for path, _ in seen)


def test_cloud_model_rejected_before_any_private_input(embedding_server):
    url, seen, state = embedding_server
    state['remote'] = True
    with pytest.raises(EmbeddingError):
        OllamaEmbeddings(url).embed(['private marker'], purpose='document')
    assert all('private marker' not in str(body) for _, body in seen)


@pytest.mark.parametrize('vectors', [[], [[0.0] * 768], [[True] * 768], [[float('nan')] * 768],
                                     [[1.0] * 15], [[1.0] * 768, [1.0] * 768]])
def test_bad_vector_responses_are_not_indexed(embedding_server, vectors):
    url, seen, state = embedding_server
    state['vectors'] = vectors
    with pytest.raises(EmbeddingError):
        OllamaEmbeddings(url).embed(['private marker'], purpose='document')


def test_alias_change_during_embedding_discards_vectors(embedding_server):
    url, seen, state = embedding_server
    state['change_digest'] = True
    with pytest.raises(EmbeddingError, match='changed'):
        OllamaEmbeddings(url).embed(['private marker'], purpose='document')


def test_index_is_full_atomic_upload_only_after_all_batches(embedding_server):
    url, _, _ = embedding_server
    uploads = []

    class Hub:
        def document_chunks(self, document_id):
            assert document_id == 'd1'
            return {'document': {'id': 'd1', 'source_version': 'v1'},
                    'chunks': [{'chunk_id': f'c{i}', 'source_version': 'v1', 'content': f'Note {i}'}
                               for i in range(10)]}

        def store_embeddings(self, *args):
            uploads.append(args)
            return {'indexed': True, 'document_id': 'd1', 'source_version': 'v1',
                    'model_id': args[2], 'chunks': 10, 'dimensions': 768}

    result = index_document(Hub(), OllamaEmbeddings(url), 'd1')
    assert result['indexed'] is True and len(uploads) == 1
    assert uploads[0][:2] == ('d1', 'v1')
    assert [v['chunk_id'] for v in uploads[0][3]] == [f'c{i}' for i in range(10)]


def test_local_failure_falls_back_to_labelled_lexical_search():
    class Offline:
        def embed(self, *args, **kwargs):
            raise EmbeddingError('offline')

    class Hub:
        def search(self, query, limit=5, **kwargs):
            assert not kwargs
            return {'citations': [], 'retrieval_mode': 'lexical', 'semantic_available': False}

    result = search_documents(Hub(), 'watering', Offline())
    assert result['retrieval_mode'] == 'lexical'
    assert result['semantic_status'] == 'local_embedding_unavailable'


def test_invalid_input_does_not_reach_local_runtime(embedding_server):
    url, seen, _ = embedding_server
    model = OllamaEmbeddings(url)
    for texts, purpose in [([], 'document'), (['x'] * 9, 'document'), (['x' * 8193], 'query'), (['x'], 'bad')]:
        with pytest.raises(EmbeddingError):
            model.embed(texts, purpose=purpose)
    assert seen == []
