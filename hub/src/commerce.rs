//! Owner-scoped cart and checkout-opening journal. Never performs network or browser I/O.
use crate::{
    actions::{Decide, Decision, Error},
    knowledge::Knowledge,
};
use rusqlite::{params, Connection, OptionalExtension, TransactionBehavior};
use serde::{Deserialize, Serialize};
use serde_json::{json, Value};
use sha2::{Digest, Sha256};
use std::collections::HashSet;
use uuid::Uuid;

const MAX_PRICE: i64 = 99_999_999_999_999;
const MAX_TOTAL: i64 = MAX_PRICE * 50;
fn encode<T: Serialize>(v: &T) -> Result<String, Error> {
    serde_json::to_string(v).map_err(|_| Error::Storage)
}
fn digest(v: &Value) -> String {
    format!("{:x}", Sha256::digest(v.to_string().as_bytes()))
}
fn bounded(s: &str, max: usize) -> bool {
    !s.trim().is_empty() && s.chars().count() <= max && !s.chars().any(char::is_control)
}
fn identifier(s: &str, max: usize) -> bool {
    !s.is_empty()
        && s.len() <= max
        && s.bytes()
            .all(|b| b.is_ascii_alphanumeric() || matches!(b, b'_' | b'-'))
}
fn hex(s: &str) -> bool {
    s.len() == 64
        && s.bytes()
            .all(|b| b.is_ascii_digit() || (b'a'..=b'f').contains(&b))
}
fn host(s: &str) -> bool {
    s.len() <= 253
        && s.contains('.')
        && !s.bytes().all(|b| b.is_ascii_digit() || b == b'.')
        && s.split('.').all(|label| {
            !label.is_empty()
                && label.len() <= 63
                && !label.starts_with('-')
                && !label.ends_with('-')
                && label
                    .bytes()
                    .all(|b| b.is_ascii_lowercase() || b.is_ascii_digit() || b == b'-')
        })
}
#[derive(Clone, Deserialize, Serialize)]
#[serde(deny_unknown_fields)]
pub struct MerchantConfig {
    pub principal_id: String,
    pub id: String,
    pub display_name: String,
    pub shop_domain: String,
    pub checkout_hosts: Vec<String>,
}
#[derive(Clone, Serialize)]
pub struct Merchant {
    pub id: String,
    pub display_name: String,
    pub shop_domain: String,
    pub checkout_hosts: Vec<String>,
    pub version: String,
    #[serde(skip)]
    pub principal_id: String,
}
impl MerchantConfig {
    pub fn valid(&self) -> bool {
        identifier(&self.id, 64)
            && bounded(&self.display_name, 100)
            && host(&self.shop_domain)
            && self
                .shop_domain
                .strip_suffix(".myshopify.com")
                .is_some_and(|s| !s.is_empty() && !s.contains('.'))
            && (1..=10).contains(&self.checkout_hosts.len())
            && self.checkout_hosts.iter().all(|s| host(s))
            && self.checkout_hosts.iter().collect::<HashSet<_>>().len() == self.checkout_hosts.len()
    }
    pub fn record(&self) -> Merchant {
        let mut c = self.clone();
        c.checkout_hosts.sort();
        let version = digest(&json!(c));
        Merchant {
            id: c.id,
            display_name: c.display_name,
            shop_domain: c.shop_domain,
            checkout_hosts: c.checkout_hosts,
            version,
            principal_id: c.principal_id,
        }
    }
}
pub fn merchant<'a>(
    merchants: &'a [Merchant],
    owner: &str,
    id: &str,
) -> Result<&'a Merchant, Error> {
    merchants
        .iter()
        .find(|m| m.principal_id == owner && m.id == id)
        .ok_or(Error::NotFound)
}
#[derive(Clone, Deserialize, Serialize, PartialEq, Eq)]
#[serde(deny_unknown_fields)]
pub struct Line {
    pub variant_id: String,
    pub title: String,
    pub quantity: i64,
    pub unit_price_minor: i64,
}
fn lines_total(lines: &[Line]) -> Result<i64, Error> {
    let mut ids = HashSet::new();
    let mut units = 0i64;
    let mut total = 0i64;
    if !(1..=10).contains(&lines.len()) {
        return Err(Error::BadRequest("Use one to ten distinct cart lines"));
    }
    for line in lines {
        let valid_id = line
            .variant_id
            .strip_prefix("gid://shopify/ProductVariant/")
            .is_some_and(|s| {
                !s.starts_with('0')
                    && (1..=20).contains(&s.len())
                    && s.bytes().all(|b| b.is_ascii_digit())
            });
        if !valid_id
            || !ids.insert(&line.variant_id)
            || !bounded(&line.title, 300)
            || !(1..=10).contains(&line.quantity)
            || !(0..=MAX_PRICE).contains(&line.unit_price_minor)
        {
            return Err(Error::BadRequest("Invalid cart line"));
        }
        units += line.quantity;
        total += line.quantity * line.unit_price_minor;
    }
    if units > 50 {
        return Err(Error::BadRequest("Use at most fifty total units"));
    }
    Ok(total)
}
#[derive(Deserialize)]
#[serde(deny_unknown_fields)]
pub struct Proposal {
    pub merchant_id: String,
    pub merchant_version: String,
    pub lines: Vec<Line>,
    pub currency: String,
    pub observed_at: i64,
    pub idempotency_key: String,
}
#[derive(Clone, Deserialize, Serialize, PartialEq, Eq)]
pub struct Payload {
    pub merchant_id: String,
    pub merchant_version: String,
    pub merchant_name: String,
    pub shop_domain: String,
    pub lines: Vec<Line>,
    pub currency: String,
    pub observed_at: i64,
    pub quoted_subtotal_minor: i64,
}
#[derive(Clone, Copy, Deserialize, Serialize, PartialEq, Eq)]
#[serde(rename_all = "snake_case")]
pub enum Provider {
    Shopify,
}
#[derive(Clone, Copy, Deserialize, Serialize, PartialEq, Eq)]
#[serde(rename_all = "snake_case")]
pub enum PaymentStatus {
    NotSubmitted,
}
#[derive(Clone, Deserialize, Serialize, PartialEq, Eq)]
#[serde(deny_unknown_fields)]
pub struct CartEvidence {
    pub provider: Provider,
    pub payment_status: PaymentStatus,
    pub currency: Option<String>,
    pub subtotal_minor: Option<i64>,
    pub total_minor: Option<i64>,
    pub lines: Vec<Line>,
}
#[derive(Clone, Deserialize, Serialize, PartialEq, Eq)]
#[serde(deny_unknown_fields)]
pub struct Checkout {
    pub cart_id: String,
    pub checkout_url: String,
}
#[derive(Clone, Copy, Deserialize, Serialize, PartialEq, Eq)]
#[serde(rename_all = "snake_case")]
pub enum CartStatus {
    Prepared,
    Failed,
    Unknown,
}
#[derive(Deserialize)]
#[serde(deny_unknown_fields)]
pub struct CartResult {
    pub status: CartStatus,
    pub evidence: CartEvidence,
    pub checkout: Option<Checkout>,
}
#[derive(Clone, Deserialize, Serialize, PartialEq, Eq)]
#[serde(deny_unknown_fields)]
pub struct OpenEvidence {
    pub payment_status: PaymentStatus,
}
#[derive(Clone, Copy, Deserialize, Serialize, PartialEq, Eq)]
#[serde(rename_all = "snake_case")]
pub enum OpenStatus {
    Opened,
    Failed,
    Unknown,
}
#[derive(Deserialize)]
#[serde(deny_unknown_fields)]
pub struct OpenResult {
    pub status: OpenStatus,
    pub evidence: OpenEvidence,
}
#[derive(Deserialize)]
#[serde(deny_unknown_fields)]
pub struct OpenClaim {
    pub argument_hash: String,
}
#[derive(Clone, Deserialize, Serialize)]
pub struct Cart {
    pub id: String,
    pub kind: String,
    pub payload: Payload,
    pub argument_hash: String,
    pub created_at: i64,
    pub expires_at: i64,
    pub status: String,
    pub evidence: Option<CartEvidence>,
    pub recovery_note: Option<String>,
    pub checkout_status: String,
    pub checkout_evidence: Option<OpenEvidence>,
    decision: Option<Decision>,
    checkout: Option<Checkout>,
}
impl Cart {
    pub fn view(&self) -> Value {
        let mut v = json!(self);
        let o = v.as_object_mut().expect("record object");
        o.remove("decision");
        o.remove("checkout");
        v
    }
    pub fn checkout_view(&self) -> Value {
        json!({"action":self.view(),"checkout":self.checkout})
    }
}
fn current<'a>(merchants: &'a [Merchant], owner: &str, p: &Payload) -> Result<&'a Merchant, Error> {
    let m = merchant(merchants, owner, &p.merchant_id)
        .map_err(|_| Error::Conflict("The merchant is no longer authorized"))?;
    if m.version != p.merchant_version
        || m.shop_domain != p.shop_domain
        || m.display_name != p.merchant_name
    {
        return Err(Error::Conflict("The merchant configuration changed"));
    }
    Ok(m)
}
fn checkout_valid(c: &Checkout, m: &Merchant) -> bool {
    let cart = c.cart_id.strip_prefix("gid://shopify/Cart/");
    let valid_token = |s: &str| {
        !s.is_empty()
            && s.bytes()
                .all(|b| b.is_ascii_alphanumeric() || matches!(b, b'_' | b'-' | b'+' | b'=' | b'.'))
    };
    let cart_ok = cart.is_some_and(|s| {
        let (id, key) = s
            .split_once("?key=")
            .map_or((s, None), |(a, b)| (a, Some(b)));
        valid_token(id) && key.is_none_or(valid_token)
    });
    if !cart_ok
        || c.cart_id.len() > 2048
        || c.checkout_url.len() > 2048
        || !c.checkout_url.is_ascii()
    {
        return false;
    }
    let Some(rest) = c.checkout_url.strip_prefix("https://") else {
        return false;
    };
    let Some((authority, path_query)) = rest.split_once('/') else {
        return false;
    };
    let authority = authority.strip_suffix(":443").unwrap_or(authority);
    if !m.checkout_hosts.iter().any(|s| s == authority) {
        return false;
    }
    let (path, query) = path_query
        .split_once('?')
        .map_or((path_query, None), |(a, b)| (a, Some(b)));
    if query.is_some_and(|q| !q.strip_prefix("key=").is_some_and(valid_token)) {
        return false;
    }
    let path = path.strip_suffix('/').unwrap_or(path);
    let parts = path.split('/').collect::<Vec<_>>();
    let tail = if parts.starts_with(&["cart", "c"]) {
        &parts[2..]
    } else if parts.first() == Some(&"checkouts") {
        &parts[1..]
    } else if parts.len() > 2
        && !parts[0].is_empty()
        && parts[0].bytes().all(|b| b.is_ascii_digit())
        && parts[1] == "checkouts"
    {
        &parts[2..]
    } else {
        return false;
    };
    !tail.is_empty()
        && tail.iter().all(|s| {
            !s.is_empty()
                && s.bytes()
                    .all(|b| b.is_ascii_alphanumeric() || matches!(b, b'_' | b'-'))
        })
}
pub fn initialize(db: &Connection) -> rusqlite::Result<()> {
    db.execute_batch("BEGIN IMMEDIATE;
 CREATE TABLE IF NOT EXISTS commerce(id TEXT PRIMARY KEY,owner_id TEXT NOT NULL,idempotency_key TEXT NOT NULL,data TEXT NOT NULL CHECK(json_valid(data)),UNIQUE(owner_id,idempotency_key));
 CREATE INDEX IF NOT EXISTS commerce_owner ON commerce(owner_id);
 UPDATE commerce SET data=json_set(data,'$.status','unknown','$.recovery_note','Cart execution interrupted; outcome unverified. No automatic retry.') WHERE json_extract(data,'$.status')='executing';
 UPDATE commerce SET data=json_set(data,'$.checkout_status','unknown','$.recovery_note','Checkout opening interrupted; outcome unverified. No automatic reopen.') WHERE json_extract(data,'$.checkout_status')='executing';
 PRAGMA user_version=7;COMMIT;")
}
fn read(db: &Connection, owner: &str, id: &str) -> Result<Cart, Error> {
    let raw: Option<String> = db
        .query_row(
            "SELECT data FROM commerce WHERE owner_id=?1 AND id=?2",
            params![owner, id],
            |r| r.get(0),
        )
        .optional()?;
    serde_json::from_str(&raw.ok_or(Error::NotFound)?).map_err(|_| Error::Storage)
}
fn save(db: &Connection, owner: &str, v: &Cart) -> Result<(), Error> {
    if db.execute(
        "UPDATE commerce SET data=?3 WHERE owner_id=?1 AND id=?2",
        params![owner, v.id, encode(v)?],
    )? != 1
    {
        return Err(Error::NotFound);
    }
    Ok(())
}
impl Knowledge {
    pub fn carts(&self, owner: &str) -> Result<Vec<Cart>, Error> {
        let mut s=self.db.prepare("SELECT data FROM commerce WHERE owner_id=?1 ORDER BY json_extract(data,'$.created_at') DESC,id DESC LIMIT 100")?;
        let rows = s
            .query_map([owner], |r| r.get::<_, String>(0))?
            .collect::<rusqlite::Result<Vec<_>>>()?;
        rows.into_iter()
            .map(|s| serde_json::from_str(&s).map_err(|_| Error::Storage))
            .collect()
    }
    pub fn cart(&self, owner: &str, id: &str) -> Result<Cart, Error> {
        read(&self.db, owner, id)
    }
    pub fn propose_cart(
        &mut self,
        owner: &str,
        p: Proposal,
        merchants: &[Merchant],
        now: i64,
    ) -> Result<(Cart, bool), Error> {
        if !identifier(&p.idempotency_key, 128)
            || !identifier(&p.merchant_id, 64)
            || !hex(&p.merchant_version)
            || !["INR", "USD", "EUR", "GBP"].contains(&p.currency.as_str())
        {
            return Err(Error::BadRequest("Invalid cart proposal fields"));
        }
        let total = lines_total(&p.lines)?;
        let tx = self
            .db
            .transaction_with_behavior(TransactionBehavior::Immediate)?;
        let prior: Option<String> = tx
            .query_row(
                "SELECT data FROM commerce WHERE owner_id=?1 AND idempotency_key=?2",
                params![owner, p.idempotency_key],
                |r| r.get(0),
            )
            .optional()?;
        if let Some(raw) = prior {
            let v: Cart = serde_json::from_str(&raw).map_err(|_| Error::Storage)?;
            let old = &v.payload;
            if old.merchant_id != p.merchant_id
                || old.merchant_version != p.merchant_version
                || old.lines != p.lines
                || old.currency != p.currency
                || old.observed_at != p.observed_at
            {
                return Err(Error::Conflict(
                    "Idempotency key binds different cart arguments",
                ));
            }
            return Ok((v, false));
        }
        let m = merchant(merchants, owner, &p.merchant_id)?;
        if m.version != p.merchant_version {
            return Err(Error::Conflict("Merchant revision changed"));
        }
        if p.observed_at < 0
            || p.observed_at <= now.saturating_sub(300)
            || p.observed_at > now.saturating_add(30)
        {
            return Err(Error::BadRequest(
                "The catalog quote is stale or from the future",
            ));
        }
        let payload = Payload {
            merchant_id: m.id.clone(),
            merchant_version: m.version.clone(),
            merchant_name: m.display_name.clone(),
            shop_domain: m.shop_domain.clone(),
            lines: p.lines,
            currency: p.currency,
            observed_at: p.observed_at,
            quoted_subtotal_minor: total,
        };
        let argument_hash = digest(&json!({"kind":"commerce.prepare_cart","payload":payload}));
        let v = Cart {
            id: Uuid::new_v4().to_string(),
            kind: "commerce.prepare_cart".into(),
            expires_at: now
                .saturating_add(300)
                .min(payload.observed_at.saturating_add(300)),
            payload,
            argument_hash,
            created_at: now,
            status: "awaiting_approval".into(),
            evidence: None,
            recovery_note: None,
            checkout_status: "not_opened".into(),
            checkout_evidence: None,
            decision: None,
            checkout: None,
        };
        tx.execute(
            "INSERT INTO commerce(id,owner_id,idempotency_key,data) VALUES(?1,?2,?3,?4)",
            params![v.id, owner, p.idempotency_key, encode(&v)?],
        )?;
        tx.commit()?;
        Ok((v, true))
    }
    pub fn decide_cart(
        &mut self,
        owner: &str,
        id: &str,
        input: Decide,
        merchants: &[Merchant],
        now: i64,
    ) -> Result<Cart, Error> {
        let tx = self
            .db
            .transaction_with_behavior(TransactionBehavior::Immediate)?;
        let mut v = read(&tx, owner, id)?;
        if v.argument_hash != input.argument_hash {
            return Err(Error::Conflict("Approval arguments do not match"));
        }
        if let Some(previous) = v.decision {
            if previous == input.decision {
                return Ok(v);
            }
            return Err(Error::Conflict("A different decision was already recorded"));
        }
        if v.status != "awaiting_approval" || now >= v.expires_at {
            return Err(Error::Conflict(
                "Cart proposal is no longer awaiting fresh approval",
            ));
        }
        if input.decision == Decision::Approve {
            current(merchants, owner, &v.payload)?;
            v.status = "approved".into();
        } else {
            v.status = "rejected".into();
        }
        v.decision = Some(input.decision);
        save(&tx, owner, &v)?;
        tx.commit()?;
        Ok(v)
    }
    pub fn claim_cart(
        &mut self,
        owner: &str,
        id: &str,
        merchants: &[Merchant],
        now: i64,
    ) -> Result<Cart, Error> {
        let tx = self
            .db
            .transaction_with_behavior(TransactionBehavior::Immediate)?;
        let mut v = read(&tx, owner, id)?;
        if v.status != "approved" || now >= v.expires_at {
            return Err(Error::Conflict("Cart cannot be claimed"));
        }
        current(merchants, owner, &v.payload)?;
        v.status = "executing".into();
        save(&tx, owner, &v)?;
        tx.commit()?;
        Ok(v)
    }
    pub fn finish_cart(
        &mut self,
        owner: &str,
        id: &str,
        input: CartResult,
        merchants: &[Merchant],
    ) -> Result<Cart, Error> {
        let tx = self
            .db
            .transaction_with_behavior(TransactionBehavior::Immediate)?;
        let mut v = read(&tx, owner, id)?;
        let e = &input.evidence;
        let status = match input.status {
            CartStatus::Prepared => "prepared",
            CartStatus::Failed => "failed",
            CartStatus::Unknown => "unknown",
        };
        if v.status == status && v.evidence.as_ref() == Some(e) && v.checkout == input.checkout {
            return Ok(v);
        }
        if v.status != "executing" {
            return Err(Error::Conflict(
                "Cart result is immutable or execution was not claimed",
            ));
        }
        if input.status == CartStatus::Prepared {
            let m = current(merchants, owner, &v.payload)?;
            if e.currency.as_deref() != Some(v.payload.currency.as_str())
                || e.subtotal_minor != Some(v.payload.quoted_subtotal_minor)
                || e.lines != v.payload.lines
                || !e.total_minor.is_some_and(|n| (0..=MAX_TOTAL).contains(&n))
                || !input
                    .checkout
                    .as_ref()
                    .is_some_and(|c| checkout_valid(c, m))
            {
                return Err(Error::BadRequest(
                    "Cart evidence does not match the approved quote or checkout origin",
                ));
            }
        } else if e.currency.is_some()
            || e.subtotal_minor.is_some()
            || e.total_minor.is_some()
            || !e.lines.is_empty()
            || input.checkout.is_some()
        {
            return Err(Error::BadRequest(
                "Unconfirmed cart results cannot include checkout details",
            ));
        }
        v.status = status.into();
        v.evidence = Some(input.evidence);
        v.checkout = input.checkout;
        save(&tx, owner, &v)?;
        tx.commit()?;
        Ok(v)
    }
    pub fn checkout(
        &self,
        owner: &str,
        id: &str,
        merchants: &[Merchant],
        now: i64,
    ) -> Result<Cart, Error> {
        let v = read(&self.db, owner, id)?;
        validate_checkout(&v, owner, merchants, now)?;
        Ok(v)
    }
    pub fn claim_checkout(
        &mut self,
        owner: &str,
        id: &str,
        input: OpenClaim,
        merchants: &[Merchant],
        now: i64,
    ) -> Result<Cart, Error> {
        let tx = self
            .db
            .transaction_with_behavior(TransactionBehavior::Immediate)?;
        let mut v = read(&tx, owner, id)?;
        if v.argument_hash != input.argument_hash || v.checkout_status != "not_opened" {
            return Err(Error::Conflict("Checkout opening cannot be claimed"));
        }
        validate_checkout(&v, owner, merchants, now)?;
        v.checkout_status = "executing".into();
        save(&tx, owner, &v)?;
        tx.commit()?;
        Ok(v)
    }
    pub fn finish_checkout(
        &mut self,
        owner: &str,
        id: &str,
        input: OpenResult,
    ) -> Result<Cart, Error> {
        let tx = self
            .db
            .transaction_with_behavior(TransactionBehavior::Immediate)?;
        let mut v = read(&tx, owner, id)?;
        let status = match input.status {
            OpenStatus::Opened => "opened",
            OpenStatus::Failed => "failed",
            OpenStatus::Unknown => "unknown",
        };
        if v.checkout_status == status && v.checkout_evidence.as_ref() == Some(&input.evidence) {
            return Ok(v);
        }
        if v.checkout_status != "executing" {
            return Err(Error::Conflict(
                "Checkout result is immutable or opening was not claimed",
            ));
        }
        v.checkout_status = status.into();
        v.checkout_evidence = Some(input.evidence);
        save(&tx, owner, &v)?;
        tx.commit()?;
        Ok(v)
    }
}
fn validate_checkout(v: &Cart, owner: &str, merchants: &[Merchant], now: i64) -> Result<(), Error> {
    if v.status != "prepared" || now >= v.expires_at {
        return Err(Error::Conflict(
            "Prepared checkout is unavailable or expired",
        ));
    }
    let m = current(merchants, owner, &v.payload)?;
    if !v.checkout.as_ref().is_some_and(|c| checkout_valid(c, m)) {
        return Err(Error::Conflict(
            "Prepared checkout origin is no longer valid",
        ));
    }
    Ok(())
}

#[cfg(test)]
mod tests {
    use super::*;
    fn required<T>(r: Result<T, Error>) -> T {
        r.unwrap_or_else(|_| panic!("Expected storage operation to succeed"))
    }
    fn merchants() -> Vec<Merchant> {
        vec![MerchantConfig {
            principal_id: "owner".into(),
            id: "shop".into(),
            display_name: "Synthetic Store".into(),
            shop_domain: "synthetic.myshopify.com".into(),
            checkout_hosts: vec!["synthetic.myshopify.com".into()],
        }
        .record()]
    }
    fn proposal(m: &[Merchant], key: &str, at: i64) -> Proposal {
        Proposal {
            merchant_id: "shop".into(),
            merchant_version: m[0].version.clone(),
            lines: vec![Line {
                variant_id: "gid://shopify/ProductVariant/1".into(),
                title: "Shirt".into(),
                quantity: 1,
                unit_price_minor: 10000,
            }],
            currency: "INR".into(),
            observed_at: at,
            idempotency_key: key.into(),
        }
    }
    fn approve(v: &Cart) -> Decide {
        Decide {
            decision: Decision::Approve,
            argument_hash: v.argument_hash.clone(),
        }
    }
    #[test]
    fn expiry_follows_original_quote_and_blocks_approval_claim_and_checkout_at_exact_boundary() {
        let d = tempfile::TempDir::new().unwrap();
        let mut s = Knowledge::open(&d.path().join("hub.db")).unwrap();
        let m = merchants();
        let (v, _) = required(s.propose_cart("owner", proposal(&m, "stale", 900), &m, 1000));
        assert_eq!(v.expires_at, 1200);
        assert!(matches!(
            s.decide_cart("owner", &v.id, approve(&v), &m, 1200),
            Err(Error::Conflict(_))
        ));
        let (v, _) = required(s.propose_cart("owner", proposal(&m, "lateclaim", 900), &m, 1000));
        required(s.decide_cart("owner", &v.id, approve(&v), &m, 1199));
        assert!(matches!(
            s.claim_cart("owner", &v.id, &m, 1200),
            Err(Error::Conflict(_))
        ));
        let (v, _) = required(s.propose_cart("owner", proposal(&m, "lateopen", 900), &m, 1000));
        required(s.decide_cart("owner", &v.id, approve(&v), &m, 1001));
        required(s.claim_cart("owner", &v.id, &m, 1002));
        required(s.finish_cart(
            "owner",
            &v.id,
            CartResult {
                status: CartStatus::Prepared,
                evidence: CartEvidence {
                    provider: Provider::Shopify,
                    payment_status: PaymentStatus::NotSubmitted,
                    currency: Some("INR".into()),
                    subtotal_minor: Some(10000),
                    total_minor: Some(11000),
                    lines: v.payload.lines.clone(),
                },
                checkout: Some(Checkout {
                    cart_id: "gid://shopify/Cart/synthetic".into(),
                    checkout_url: "https://synthetic.myshopify.com/checkouts/synthetic".into(),
                }),
            },
            &m,
        ));
        assert!(s.checkout("owner", &v.id, &m, 1199).is_ok());
        assert!(matches!(
            s.checkout("owner", &v.id, &m, 1200),
            Err(Error::Conflict(_))
        ));
        assert!(matches!(
            s.claim_checkout(
                "owner",
                &v.id,
                OpenClaim {
                    argument_hash: v.argument_hash.clone()
                },
                &m,
                1200
            ),
            Err(Error::Conflict(_))
        ));
        // Retrying the same committed proposal does not extend its original expiry.
        let (replay, created) =
            required(s.propose_cart("owner", proposal(&m, "lateopen", 900), &m, 9999));
        assert!(!created);
        assert_eq!(replay.expires_at, 1200);
    }
    #[test]
    fn merchant_host_order_has_stable_revision_but_owner_or_allowlist_change_does_not() {
        let mut cfg = MerchantConfig {
            principal_id: "owner".into(),
            id: "shop".into(),
            display_name: "Synthetic Store".into(),
            shop_domain: "synthetic.myshopify.com".into(),
            checkout_hosts: vec![
                "synthetic.myshopify.com".into(),
                "checkout.synthetic.test".into(),
            ],
        };
        let first = cfg.record().version;
        cfg.checkout_hosts.reverse();
        assert_eq!(first, cfg.record().version);
        cfg.principal_id = "other".into();
        assert_ne!(first, cfg.record().version);
        cfg.principal_id = "owner".into();
        cfg.checkout_hosts.pop();
        assert_ne!(first, cfg.record().version);
    }
}
