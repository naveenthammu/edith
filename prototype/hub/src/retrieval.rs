use crate::domain::{Citation, Document, Profile};
use std::collections::HashSet;

fn terms(text: &str) -> HashSet<String> {
    text.split(|c:char|!c.is_alphanumeric()).map(str::to_lowercase)
        .filter(|term| term.len()>1 && !["is","do","to","of","in","it","on","at","me","my","am","the","and","are","what","when","how","does","can","you","our","for","with","tell","about","please","this","that","have","from","was","where","will","would","could","should","note","notes"].contains(&term.as_str())).collect()
}

/// Scoped lexical overlap. No embeddings, external index, or model execution is involved.
pub fn search(documents: &[Document], profile: Profile, query: &str) -> Vec<Citation> {
    let query_terms = terms(query);
    if query_terms.is_empty() { return vec![]; }
    let mut hits = Vec::new();
    for document in documents.iter().filter(|d|profile.can_read(d.info.scope)) {
        let title_terms=terms(&document.info.title);
        let content_terms=terms(&document.content);
        let score=query_terms.intersection(&content_terms).count()+query_terms.intersection(&title_terms).count()*2;
        if score==0 { continue; }
        let chars:Vec<char>=document.content.chars().collect();
        let first=chars.iter().enumerate().find_map(|(index,_)| {
            if index>0 && chars[index-1].is_alphanumeric() { return None; }
            let word:String=chars[index..].iter().take_while(|c|c.is_alphanumeric()).collect();
            query_terms.contains(&word.to_lowercase()).then_some(index)
        }).unwrap_or(0);
        let start=first.saturating_sub(100);
        let end=(start+700).min(chars.len());
        let excerpt=format!("{}{}{}",if start>0 {"…"}else{""},chars[start..end].iter().collect::<String>(),if end<chars.len(){"…"}else{""});
        hits.push(Citation {document_id:document.info.id.clone(),title:document.info.title.clone(),excerpt,score});
    }
    hits.sort_by(|a,b|b.score.cmp(&a.score).then(a.document_id.cmp(&b.document_id)));
    hits.truncate(4); hits
}

