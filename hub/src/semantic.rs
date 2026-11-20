use crate::{
    knowledge::{
        permission, write_chunks, Citation, CreateDocument, Document, Knowledge, Search, Visibility,
    },
    Role,
};
use rusqlite::{params, Connection, OptionalExtension, Row};
use serde::{Deserialize, Serialize};
use std::collections::{HashMap, HashSet};

pub enum Error {
    BadRequest(&'static str),
    NotFound,
    Conflict(&'static str),
    Storage,
}
impl From<rusqlite::Error> for Error {
    fn from(_: rusqlite::Error) -> Self {
        Self::Storage
    }
}
fn valid_model(model: &str) -> bool {
    !model.is_empty() && model.len() <= 256 && model.bytes().all(|b| b.is_ascii_graphic())
}
fn norm(vector: &[f32]) -> Result<f64, &'static str> {
    if !(16..=4096).contains(&vector.len()) || vector.iter().any(|v| !v.is_finite()) {
        return Err("Vectors must contain 16 through 4096 finite numbers");
    }
    let magnitude = vector
        .iter()
        .map(|&v| (v as f64) * (v as f64))
        .sum::<f64>()
        .sqrt();
    if magnitude == 0.0 {
        return Err("A zero vector cannot be indexed or searched");
    }
    Ok(magnitude)
}
#[derive(Deserialize)]
#[serde(deny_unknown_fields)]
pub struct QueryVector {
    pub model_id: String,
    pub vector: Vec<f32>,
}
impl QueryVector {
    pub fn validate(&self) -> Result<(), &'static str> {
        if !valid_model(&self.model_id) {
            return Err(
                "Model identifier must contain one to 256 printable non-space ASCII characters",
            );
        }
        norm(&self.vector)?;
        Ok(())
    }
}
#[derive(Deserialize)]
#[serde(deny_unknown_fields)]
pub struct Embedding {
    pub chunk_id: String,
    pub vector: Vec<f32>,
}
#[derive(Deserialize)]
#[serde(deny_unknown_fields)]
pub struct Upload {
    pub source_version: String,
    pub model_id: String,
    pub embeddings: Vec<Embedding>,
}
#[derive(Serialize)]
pub struct Chunk {
    pub chunk_id: String,
    pub content: String,
    pub source_version: String,
    #[serde(skip)]
    row_id: i64,
}
#[derive(Serialize)]
pub struct Chunks {
    pub document: Document,
    pub chunks: Vec<Chunk>,
}
#[derive(Serialize)]
pub struct IndexReceipt {
    pub indexed: bool,
    pub document_id: String,
    pub source_version: String,
    pub model_id: String,
    pub chunks: usize,
    pub dimensions: usize,
}
#[derive(Deserialize)]
#[serde(deny_unknown_fields)]
pub struct UpdateDocument {
    pub expected_source_version: String,
    pub title: String,
    pub content: String,
    pub visibility: Visibility,
    #[serde(default)]
    pub child_allowed: bool,
}
impl UpdateDocument {
    fn replacement(self) -> CreateDocument {
        CreateDocument {
            title: self.title,
            content: self.content,
            visibility: self.visibility,
            child_allowed: self.child_allowed,
        }
    }
}
#[derive(Deserialize)]
#[serde(deny_unknown_fields)]
pub struct Source {
    pub document_id: String,
    pub source_version: String,
}
#[derive(Deserialize)]
#[serde(deny_unknown_fields)]
pub struct ValidateSources {
    pub sources: Vec<Source>,
}
#[derive(Serialize)]
pub struct SourceValidation {
    pub document_id: String,
    pub source_version: String,
    pub valid: bool,
}
#[derive(Serialize)]
pub struct SearchResult {
    pub citations: Vec<Citation>,
    pub retrieval_mode: &'static str,
    pub semantic_available: bool,
}

pub fn initialize(db: &Connection) -> rusqlite::Result<()> {
    db.execute_batch("BEGIN IMMEDIATE;
        CREATE TABLE IF NOT EXISTS embedding_models(model_id TEXT PRIMARY KEY,dimensions INTEGER NOT NULL CHECK(dimensions BETWEEN 16 AND 4096));
        CREATE TABLE IF NOT EXISTS document_embeddings(document_id TEXT NOT NULL REFERENCES documents(id) ON DELETE CASCADE,chunk_id TEXT NOT NULL,chunk_rowid INTEGER NOT NULL,source_version TEXT NOT NULL,model_id TEXT NOT NULL REFERENCES embedding_models(model_id),dimensions INTEGER NOT NULL,vector_json TEXT NOT NULL,norm REAL NOT NULL CHECK(norm>0),PRIMARY KEY(document_id,chunk_id,model_id));
        CREATE INDEX IF NOT EXISTS embeddings_model ON document_embeddings(model_id,document_id);
        PRAGMA user_version=4; COMMIT;")
}
fn document_row(r: &Row<'_>) -> rusqlite::Result<Document> {
    Ok(Document {
        id: r.get(0)?,
        owner_id: r.get(1)?,
        title: r.get(2)?,
        visibility: r.get(3)?,
        child_allowed: r.get(4)?,
        source_version: r.get(5)?,
        created_at: r.get(6)?,
    })
}
fn owned(db: &Connection, owner: &str, id: &str) -> Result<Document, Error> {
    db.query_row("SELECT id,owner_id,title,visibility,child_allowed,source_version,created_at FROM documents WHERE id=?1 AND owner_id=?2",params![id,owner],document_row).optional()?.ok_or(Error::NotFound)
}
fn document_chunks(db: &Connection, document: &Document) -> Result<Vec<Chunk>, Error> {
    let mut statement=db.prepare("SELECT rowid,chunk_id,content FROM document_chunks WHERE document_id=?1 ORDER BY CAST(substr(chunk_id,instr(chunk_id,':')+1) AS INTEGER)")?;
    let rows = statement.query_map([&document.id], |r| {
        Ok(Chunk {
            row_id: r.get(0)?,
            chunk_id: r.get(1)?,
            content: r.get(2)?,
            source_version: document.source_version.clone(),
        })
    })?;
    Ok(rows.collect::<rusqlite::Result<Vec<_>>>()?)
}
impl Knowledge {
    pub fn owned_document(&self, owner: &str, id: &str) -> Result<Document, Error> {
        owned(&self.db, owner, id)
    }
    pub fn chunks(&self, owner: &str, id: &str) -> Result<Chunks, Error> {
        let document = owned(&self.db, owner, id)?;
        let chunks = document_chunks(&self.db, &document)?;
        Ok(Chunks { document, chunks })
    }
    pub fn index_embeddings(
        &mut self,
        owner: &str,
        id: &str,
        input: Upload,
    ) -> Result<IndexReceipt, Error> {
        let transaction = self.db.transaction()?;
        let document = owned(&transaction, owner, id)?;
        if input.source_version != document.source_version {
            return Err(Error::Conflict(
                "Source changed; read its current chunks before indexing",
            ));
        }
        if !valid_model(&input.model_id) {
            return Err(Error::BadRequest(
                "Model identifier must contain one to 256 printable non-space ASCII characters",
            ));
        }
        let chunks = document_chunks(&transaction, &document)?;
        if input.embeddings.len() != chunks.len() || input.embeddings.is_empty() {
            return Err(Error::BadRequest(
                "An atomic embedding replacement must include every document chunk exactly once",
            ));
        }
        let chunk_rows: HashMap<_, _> = chunks
            .iter()
            .map(|c| (c.chunk_id.as_str(), c.row_id))
            .collect();
        let mut seen = HashSet::new();
        let dimensions = input.embeddings[0].vector.len();
        let mut magnitudes = Vec::with_capacity(input.embeddings.len());
        for e in &input.embeddings {
            if !chunk_rows.contains_key(e.chunk_id.as_str()) || !seen.insert(&e.chunk_id) {
                return Err(Error::BadRequest(
                    "Embedding chunks must be unique and belong to the selected document",
                ));
            }
            if e.vector.len() != dimensions {
                return Err(Error::BadRequest(
                    "Embedding dimensions must be consistent across document chunks",
                ));
            }
            magnitudes.push(norm(&e.vector).map_err(Error::BadRequest)?);
        }
        let existing: Option<i64> = transaction
            .query_row(
                "SELECT dimensions FROM embedding_models WHERE model_id=?1",
                [&input.model_id],
                |r| r.get(0),
            )
            .optional()?;
        if existing.is_some_and(|expected| expected != dimensions as i64) {
            return Err(Error::Conflict(
                "Model identifier is already bound to a different vector dimension",
            ));
        }
        transaction.execute(
            "INSERT OR IGNORE INTO embedding_models(model_id,dimensions) VALUES (?1,?2)",
            params![input.model_id, dimensions as i64],
        )?;
        transaction.execute(
            "DELETE FROM document_embeddings WHERE document_id=?1 AND model_id=?2",
            params![id, input.model_id],
        )?;
        for (embedding, magnitude) in input.embeddings.iter().zip(magnitudes) {
            let vector = serde_json::to_string(&embedding.vector).map_err(|_| Error::Storage)?;
            transaction.execute("INSERT INTO document_embeddings(document_id,chunk_id,chunk_rowid,source_version,model_id,dimensions,vector_json,norm) VALUES(?1,?2,?3,?4,?5,?6,?7,?8)",params![id,embedding.chunk_id,chunk_rows[embedding.chunk_id.as_str()],input.source_version,input.model_id,dimensions as i64,vector,magnitude])?;
        }
        transaction.commit()?;
        Ok(IndexReceipt {
            indexed: true,
            document_id: id.into(),
            source_version: input.source_version,
            model_id: input.model_id,
            chunks: input.embeddings.len(),
            dimensions,
        })
    }
    pub fn update_document(
        &mut self,
        owner: &str,
        id: &str,
        input: UpdateDocument,
    ) -> Result<Document, Error> {
        let transaction = self.db.transaction()?;
        let old = owned(&transaction, owner, id)?;
        if input.expected_source_version != old.source_version {
            return Err(Error::Conflict(
                "Source changed; reload its version before replacing it",
            ));
        }
        let replacement = input.replacement();
        replacement.validate().map_err(Error::BadRequest)?;
        let version = replacement.source_version();
        let visibility = match replacement.visibility {
            Visibility::Private => "private",
            Visibility::Household => "household",
        };
        transaction.execute("UPDATE documents SET title=?1,visibility=?2,child_allowed=?3,source_version=?4 WHERE id=?5 AND owner_id=?6",params![replacement.title.trim(),visibility,replacement.child_allowed,version,id,owner])?;
        transaction.execute("DELETE FROM document_embeddings WHERE document_id=?1", [id])?;
        transaction.execute("DELETE FROM document_chunks WHERE document_id=?1", [id])?;
        write_chunks(
            &transaction,
            id,
            replacement.title.trim(),
            &replacement.content,
        )?;
        let updated = owned(&transaction, owner, id)?;
        transaction.commit()?;
        Ok(updated)
    }
    pub fn validate_sources(
        &self,
        owner: &str,
        role: Role,
        input: ValidateSources,
    ) -> Result<Vec<SourceValidation>, Error> {
        if input.sources.len() > 100
            || input.sources.iter().any(|s| {
                s.document_id.is_empty()
                    || s.document_id.len() > 128
                    || s.source_version.is_empty()
                    || s.source_version.len() > 128
            })
        {
            return Err(Error::BadRequest(
                "Validate at most 100 bounded document IDs and source versions",
            ));
        }
        let sql = format!(
            "SELECT EXISTS(SELECT 1 FROM documents d WHERE {} AND d.id=?2 AND d.source_version=?3)",
            permission(role, "d")
        );
        let mut statement = self.db.prepare(&sql)?;
        let mut checked = Vec::with_capacity(input.sources.len());
        for source in input.sources {
            let valid: bool = statement.query_row(
                params![owner, source.document_id, source.source_version],
                |r| r.get(0),
            )?;
            checked.push(SourceValidation {
                document_id: source.document_id,
                source_version: source.source_version,
                valid,
            });
        }
        Ok(checked)
    }
    pub fn search(&self, owner: &str, role: Role, input: &Search) -> Result<SearchResult, Error> {
        let window = if input.query_vector.is_some() {
            100
        } else {
            input.limit as usize
        };
        let lexical = self.lexical_search(owner, role, input, window)?;
        let Some(query) = &input.query_vector else {
            return Ok(SearchResult {
                citations: lexical,
                retrieval_mode: "lexical",
                semantic_available: false,
            });
        };
        let (semantic, available) =
            self.semantic_matches(owner, role, query, input.min_similarity, window)?;
        if !available {
            return Ok(SearchResult {
                citations: lexical.into_iter().take(input.limit as usize).collect(),
                retrieval_mode: "lexical",
                semantic_available: false,
            });
        }
        // Reciprocal-rank fusion combines independently ranked authorized lists.
        // The window is a documented top-100 per-channel retrieval cutoff, not a
        // pre-ranking candidate cutoff: both channels scan all allowed matches.
        let mut merged: HashMap<String, Citation> = HashMap::new();
        for list in [lexical, semantic] {
            for (rank, mut citation) in list.into_iter().enumerate() {
                let contribution = 1.0 / (60.0 + rank as f64 + 1.0);
                if let Some(existing) = merged.get_mut(&citation.chunk_id) {
                    existing.score += contribution;
                } else {
                    citation.score = contribution;
                    merged.insert(citation.chunk_id.clone(), citation);
                }
            }
        }
        let mut citations: Vec<_> = merged.into_values().collect();
        sort_citations(&mut citations);
        citations.truncate(input.limit as usize);
        Ok(SearchResult {
            citations,
            retrieval_mode: "hybrid",
            semantic_available: true,
        })
    }
    fn semantic_matches(
        &self,
        owner: &str,
        role: Role,
        query: &QueryVector,
        min_similarity: f64,
        limit: usize,
    ) -> Result<(Vec<Citation>, bool), Error> {
        let query_norm = norm(&query.vector).map_err(Error::BadRequest)?;
        let sql=format!("SELECT d.id,e.chunk_id,d.title,c.content,d.source_version,e.vector_json,e.dimensions,e.norm FROM document_embeddings e JOIN documents d ON d.id=e.document_id AND d.source_version=e.source_version JOIN document_chunks c ON c.rowid=e.chunk_rowid AND c.document_id=d.id AND c.chunk_id=e.chunk_id WHERE {} AND e.model_id=?2",permission(role,"d"));
        let mut statement = self.db.prepare(&sql)?;
        let mut rows = statement.query(params![owner, query.model_id])?;
        let mut hits = Vec::with_capacity(limit + 1);
        let mut available = false;
        while let Some(row) = rows.next()? {
            let dimensions: i64 = row.get(6)?;
            if !(16..=4096).contains(&dimensions) {
                return Err(Error::Storage);
            }
            let dimensions = dimensions as usize;
            if dimensions != query.vector.len() {
                return Err(Error::BadRequest(
                    "Query vector dimensions do not match the permitted model index",
                ));
            }
            let encoded: String = row.get(5)?;
            let vector: Vec<f32> = serde_json::from_str(&encoded).map_err(|_| Error::Storage)?;
            if vector.len() != dimensions {
                return Err(Error::Storage);
            }
            let magnitude = norm(&vector).map_err(|_| Error::Storage)?;
            let stored_norm: f64 = row.get(7)?;
            if !stored_norm.is_finite() || stored_norm <= 0.0 {
                return Err(Error::Storage);
            }
            available = true;
            let similarity = (vector
                .iter()
                .zip(&query.vector)
                .map(|(&a, &b)| (a as f64) * (b as f64))
                .sum::<f64>()
                / (magnitude * query_norm))
                .clamp(-1.0, 1.0);
            if similarity < min_similarity {
                continue;
            }
            hits.push(Citation {
                document_id: row.get(0)?,
                chunk_id: row.get(1)?,
                title: row.get(2)?,
                excerpt: row.get(3)?,
                source_version: row.get(4)?,
                score: similarity,
            });
            sort_citations(&mut hits);
            hits.truncate(limit);
        }
        Ok((hits, available))
    }
}
fn sort_citations(citations: &mut [Citation]) {
    citations.sort_by(|a, b| {
        b.score
            .total_cmp(&a.score)
            .then(a.document_id.cmp(&b.document_id))
            .then(a.chunk_id.cmp(&b.chunk_id))
    });
}
