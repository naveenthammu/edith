"""Trusted source metadata travels beside tool text, never parsed from model output."""
from collections import defaultdict


class EvidenceChanged(RuntimeError):
    pass


class EvidenceText(str):
    def __new__(cls, content, hub, sources):
        result = super().__new__(cls, content)
        result.hub = hub
        result.sources = tuple((s['document_id'], s['source_version']) for s in sources)
        return result


class SourceLedger:
    def __init__(self, context_guard=None):
        self._sources = {}
        self._context_guard = context_guard

    def clear(self):
        self._sources.clear()

    def record(self, result):
        if not isinstance(result, EvidenceText):
            return
        for document_id, version in result.sources:
            if (not isinstance(document_id, str) or not 1 <= len(document_id) <= 128
                    or not isinstance(version, str) or not 1 <= len(version) <= 128):
                raise EvidenceChanged('Invalid source reference.')
            self._sources[(id(result.hub), document_id, version)] = result.hub
        if len(self._sources) > 1024:
            raise EvidenceChanged('This source context is too large; start a fresh question.')

    def validate(self):
        grouped = defaultdict(list)
        for (_, document_id, version), hub in self._sources.items():
            grouped[id(hub)].append((hub, document_id, version))
        try:
            if self._context_guard is not None and self._context_guard() is not True:
                raise EvidenceChanged('Household identity or access changed.')
            for references in grouped.values():
                for start in range(0, len(references), 100):
                    group = references[start:start + 100]
                    wanted = {(doc, version) for _, doc, version in group}
                    response = group[0][0].validate_sources([
                        {'document_id': doc, 'source_version': version} for _, doc, version in group])
                    rows = response.get('sources')
                    if not isinstance(rows, list) or len(rows) != len(wanted):
                        raise EvidenceChanged('Source validation was incomplete.')
                    actual = set()
                    for row in rows:
                        if not isinstance(row, dict) or row.get('valid') is not True:
                            raise EvidenceChanged('Source access or revision changed.')
                        actual.add((row.get('document_id'), row.get('source_version')))
                    if actual != wanted:
                        raise EvidenceChanged('Source validation did not match.')
        except EvidenceChanged:
            raise
        except Exception:
            raise EvidenceChanged('Source access could not be checked.') from None
