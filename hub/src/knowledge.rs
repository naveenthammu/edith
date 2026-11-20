use crate::Role;
use rusqlite::{params, Connection};
use serde::{Deserialize, Serialize};
use sha2::{Digest, Sha256};
use std::{
    collections::HashSet,
    path::Path,
    time::{Duration, SystemTime, UNIX_EPOCH},
};
use unicode_segmentation::UnicodeSegmentation;
use uuid::Uuid;

#[derive(Clone, Copy, Deserialize, Serialize)]
#[serde(rename_all = "snake_case")]
pub enum Visibility {
    Private,
    Household,
}
impl Visibility {
    fn value(self) -> &'static str {
        match self {
            Self::Private => "private",
            Self::Household => "household",
        }
    }
}

#[derive(Deserialize)]
#[serde(deny_unknown_fields)]
pub struct CreateDocument {
    pub title: String,
    pub content: String,
    pub visibility: Visibility,
    #[serde(default)]
    pub child_allowed: bool,
}
impl CreateDocument {
    pub fn validate(&self) -> Result<(), &'static str> {
        if self.title.trim().is_empty() || self.title.chars().count() > 200 {
            return Err("Title must contain one to 200 characters");
        }
        if self.content.trim().is_empty() || self.content.len() > 2_000_000 {
            return Err("Document must contain one to 2000000 UTF-8 bytes");
        }
        if self.content.contains('\0') || self.title.contains('\0') {
            return Err("NUL characters are not supported");
        }
        if self.child_allowed && matches!(self.visibility, Visibility::Private) {
            return Err("Child access requires household visibility");
        }
        Ok(())
    }
    pub(crate) fn source_version(&self) -> String {
        // Length-delimited canonical source fields make metadata/visibility
        // updates participate in optimistic concurrency and citation validity.
        let mut hash = Sha256::new();
        hash.update(b"edith-source-v2");
        for field in [
            self.title.trim(),
            self.content.as_str(),
            self.visibility.value(),
            if self.child_allowed { "true" } else { "false" },
        ] {
            hash.update((field.len() as u64).to_le_bytes());
            hash.update(field.as_bytes());
        }
        format!("{:x}", hash.finalize())
    }
}
fn default_limit() -> i64 {
    5
}
#[derive(Deserialize)]
#[serde(deny_unknown_fields)]
pub struct Search {
    pub query: String,
    #[serde(default = "default_limit")]
    pub limit: i64,
    #[serde(default)]
    pub query_vector: Option<crate::semantic::QueryVector>,
    #[serde(default = "minimum_similarity")]
    pub min_similarity: f64,
}
fn minimum_similarity() -> f64 {
    0.2
}
impl Search {
    pub fn validate(&self) -> Result<(), &'static str> {
        if self.query.len() > 2048 || !(1..=10).contains(&self.limit) {
            return Err("Query must be at most 2048 bytes and limit must be 1 through 10");
        }
        if !self.min_similarity.is_finite() || !(-1.0..=1.0).contains(&self.min_similarity) {
            return Err("Minimum similarity must be between -1 and 1");
        }
        if let Some(vector) = &self.query_vector {
            vector.validate()?;
        }
        Ok(())
    }
}
#[derive(Clone, Serialize)]
pub struct Document {
    pub id: String,
    pub owner_id: String,
    pub title: String,
    pub visibility: String,
    pub child_allowed: bool,
    pub source_version: String,
    pub created_at: i64,
}
#[derive(Clone, Serialize)]
pub struct Citation {
    pub document_id: String,
    pub chunk_id: String,
    pub title: String,
    pub excerpt: String,
    pub score: f64,
    pub source_version: String,
}
pub struct Knowledge {
    pub(crate) db: Connection,
}
impl Knowledge {
    pub fn open(path: &Path) -> Result<Self, String> {
        if let Some(parent) = path.parent() {
            std::fs::create_dir_all(parent).map_err(|_| "Cannot prepare database directory")?;
        }
        let db = Connection::open(path).map_err(|_| "Cannot open knowledge database")?;
        db.busy_timeout(Duration::from_secs(5))
            .map_err(|_| "Cannot configure database")?;
        db.execute_batch(
            "PRAGMA journal_mode=WAL; PRAGMA synchronous=FULL; PRAGMA foreign_keys=ON;",
        )
        .map_err(|_| "Cannot configure database durability")?;
        let version: i64 = db
            .pragma_query_value(None, "user_version", |r| r.get(0))
            .map_err(|_| "Cannot read database version")?;
        if version > 8 {
            return Err("Database was created by a newer hub version".into());
        }
        db.execute_batch("BEGIN IMMEDIATE;
            CREATE TABLE IF NOT EXISTS documents(id TEXT PRIMARY KEY,owner_id TEXT NOT NULL,title TEXT NOT NULL,visibility TEXT NOT NULL CHECK(visibility IN ('private','household')),child_allowed INTEGER NOT NULL CHECK(child_allowed IN (0,1)),source_version TEXT NOT NULL,created_at INTEGER NOT NULL);
            CREATE INDEX IF NOT EXISTS documents_owner ON documents(owner_id);
            CREATE VIRTUAL TABLE IF NOT EXISTS document_chunks USING fts5(chunk_id UNINDEXED,document_id UNINDEXED,title,content,tokenize='unicode61'); COMMIT;")
            .map_err(|_|"Cannot initialize knowledge schema")?;
        if version == 1 {
            db.execute_batch("BEGIN IMMEDIATE;
                ALTER TABLE document_chunks RENAME TO document_chunks_v1;
                CREATE VIRTUAL TABLE document_chunks USING fts5(chunk_id UNINDEXED,document_id UNINDEXED,title,content,tokenize='unicode61');
                INSERT INTO document_chunks(chunk_id,document_id,title,content) SELECT c.chunk_id,c.document_id,d.title,c.content FROM document_chunks_v1 c JOIN documents d ON d.id=c.document_id;
                DROP TABLE document_chunks_v1; PRAGMA user_version=2; COMMIT;")
                .map_err(|_|"Cannot migrate knowledge index")?;
        } else if version < 2 {
            db.pragma_update(None, "user_version", 2)
                .map_err(|_| "Cannot update schema version")?;
        }
        crate::actions::initialize(&db).map_err(|_| "Cannot initialize durable action journal")?;
        crate::semantic::initialize(&db).map_err(|_| "Cannot initialize semantic index")?;
        crate::personal::initialize(&db).map_err(|_| "Cannot initialize personal records")?;
        crate::commerce::initialize(&db).map_err(|_| "Cannot initialize commerce journal")?;
        crate::business_messages::initialize(&db)
            .map_err(|_| "Cannot initialize business-message journal")?;
        Ok(Self { db })
    }
    pub fn create(&mut self, owner: &str, input: CreateDocument) -> rusqlite::Result<Document> {
        let id = Uuid::new_v4().to_string();
        let doc = Document {
            id: id.clone(),
            owner_id: owner.into(),
            title: input.title.trim().into(),
            visibility: input.visibility.value().into(),
            child_allowed: input.child_allowed,
            source_version: input.source_version(),
            created_at: SystemTime::now()
                .duration_since(UNIX_EPOCH)
                .unwrap_or_default()
                .as_secs()
                .try_into()
                .unwrap_or(i64::MAX),
        };
        let transaction = self.db.transaction()?;
        transaction.execute("INSERT INTO documents(id,owner_id,title,visibility,child_allowed,source_version,created_at) VALUES (?1,?2,?3,?4,?5,?6,?7)",params![doc.id,doc.owner_id,doc.title,doc.visibility,doc.child_allowed,doc.source_version,doc.created_at])?;
        write_chunks(&transaction, &id, &doc.title, &input.content)?;
        transaction.commit()?;
        Ok(doc)
    }
    pub fn list(&self, owner: &str, role: Role) -> rusqlite::Result<Vec<Document>> {
        let sql=format!("SELECT id,owner_id,title,visibility,child_allowed,source_version,created_at FROM documents WHERE {} ORDER BY created_at DESC,id LIMIT 1000",permission(role,"documents"));
        let mut statement = self.db.prepare(&sql)?;
        let rows = statement.query_map([owner], |r| {
            Ok(Document {
                id: r.get(0)?,
                owner_id: r.get(1)?,
                title: r.get(2)?,
                visibility: r.get(3)?,
                child_allowed: r.get(4)?,
                source_version: r.get(5)?,
                created_at: r.get(6)?,
            })
        })?;
        rows.collect()
    }
    pub fn delete(&mut self, owner: &str, id: &str) -> rusqlite::Result<bool> {
        let transaction = self.db.transaction()?;
        let changed = transaction.execute(
            "DELETE FROM documents WHERE id=?1 AND owner_id=?2",
            params![id, owner],
        )?;
        if changed > 0 {
            transaction.execute("DELETE FROM document_chunks WHERE document_id=?1", [id])?;
        }
        transaction.commit()?;
        Ok(changed > 0)
    }
    pub(crate) fn lexical_search(
        &self,
        owner: &str,
        role: Role,
        input: &Search,
        candidate_limit: usize,
    ) -> rusqlite::Result<Vec<Citation>> {
        let tokens: Vec<String> = input
            .query
            .unicode_words()
            .map(str::to_lowercase)
            .take(32)
            .collect();
        if tokens.is_empty() {
            return Ok(vec![]);
        }
        let expression = tokens
            .iter()
            .map(|t| format!("\"{}\"", t.replace('"', "\"\"")))
            .collect::<Vec<_>>()
            .join(" OR ");
        // Authorize candidates before exposing text to scoring/citations. Global
        // FTS bm25 statistics would include other principals' documents.
        let sql=format!("SELECT d.id,c.chunk_id,d.title,c.content,d.source_version FROM document_chunks c JOIN documents d ON d.id=c.document_id WHERE document_chunks MATCH ?2 AND {}",permission(role,"d"));
        let mut statement = self.db.prepare(&sql)?;
        let rows = statement.query_map(params![owner, expression], |r| {
            Ok(Citation {
                document_id: r.get(0)?,
                chunk_id: r.get(1)?,
                title: r.get(2)?,
                excerpt: r.get(3)?,
                score: 0.0,
                source_version: r.get(4)?,
            })
        })?;
        // Stream every authorized match and retain only top-k. Truncating SQL
        // candidates by row/chunk ID can hide a relevant later document section.
        let mut hits = Vec::with_capacity(candidate_limit + 1);
        let query: HashSet<_> = tokens.iter().collect();
        for row in rows {
            let mut hit = row?;
            hit.score = (hit
                .excerpt
                .unicode_words()
                .map(str::to_lowercase)
                .filter(|w| query.contains(w))
                .count()
                + 2 * hit
                    .title
                    .unicode_words()
                    .map(str::to_lowercase)
                    .filter(|w| query.contains(w))
                    .count()) as f64;
            hit.score = hit.score.max(1.0);
            hits.push(hit);
            hits.sort_by(|a, b| {
                b.score
                    .total_cmp(&a.score)
                    .then(a.document_id.cmp(&b.document_id))
                    .then(a.chunk_id.cmp(&b.chunk_id))
            });
            hits.truncate(candidate_limit);
        }
        Ok(hits)
    }
}
pub(crate) fn permission(role: Role, alias: &str) -> String {
    if role.is_adult() {
        format!("({alias}.owner_id=?1 OR {alias}.visibility='household')")
    } else {
        format!("({alias}.visibility='household' AND {alias}.child_allowed=1 AND ?1 IS NOT NULL)")
    }
}

pub(crate) fn write_chunks(
    db: &Connection,
    id: &str,
    title: &str,
    content: &str,
) -> rusqlite::Result<()> {
    let chars: Vec<char> = content.chars().collect();
    let mut offset = 0;
    let mut index = 0;
    while offset < chars.len() {
        let end = (offset + 1600).min(chars.len());
        let text: String = chars[offset..end].iter().collect();
        db.execute(
            "INSERT INTO document_chunks(chunk_id,document_id,title,content) VALUES (?1,?2,?3,?4)",
            params![format!("{id}:{index}"), id, title, text],
        )?;
        if end == chars.len() {
            break;
        }
        offset = end - 200;
        index += 1;
    }
    Ok(())
}
