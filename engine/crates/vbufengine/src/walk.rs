//! Fetching a document, or parts of one, from its application: every object's data, a fixed number
//! of calls in flight, the object nearest the top of the document first, so that the top of a large
//! page is complete long before its end.

use std::cell::RefCell;
use std::cmp::Reverse;
use std::collections::{BinaryHeap, HashMap, HashSet};
use std::rc::Rc;
use std::time::{Duration, Instant};

use futures_util::future::LocalBoxFuture;
use futures_util::stream::{FuturesUnordered, StreamExt};
use mirror::client::{Client, is_gone, is_timeout};
use mirror::consts::{
    IFACE_ACCESSIBLE, IFACE_COLLECTION, IFACE_PROPERTIES, IFACE_TEXT, interface_bits,
};
use mirror::decode;
use zbus::{Message, ObjectPath, Variant};

use crate::tables::*;

/// Calls in flight: four already keep Firefox's main thread busy, and more only make every other
/// client wait longer behind them.
pub const WINDOW: usize = 4;

const IFACE_TABLE: &str = "org.a11y.atspi.Table";
const IFACE_TABLE_CELL: &str = "org.a11y.atspi.TableCell";
const IFACE_VALUE: &str = "org.a11y.atspi.Value";
const IFACE_HYPERLINK: &str = "org.a11y.atspi.Hyperlink";
const IFACE_ACTION: &str = "org.a11y.atspi.Action";
const RELATION_LABELLED_BY: u32 = 2;
const RELATION_DETAILS: u32 = 19;
const RELATION_ERROR_MESSAGE: u32 = 21;
const STATE_INVALID_ENTRY: u32 = 36;
/// Roles whose objects are plainly clickable, as controlTypes.clickableRoles has them, or that are
/// never: their actions are not asked for.
const NOT_CLICKABLE_ROLES: [&str; 16] = [
    "link",
    "push button",
    "button",
    "check box",
    "radio button",
    "toggle button",
    "menu item",
    "check menu item",
    "radio menu item",
    "page tab",
    "slider",
    "document web",
    "entry",
    "password text",
    "combo box",
    "internal frame",
];

pub fn iface_bit(name: &str) -> u32 {
    interface_bits([name])
}

#[derive(Debug, Clone, Copy, PartialEq, Eq, Hash, PartialOrd, Ord)]
pub enum Field {
    Children,
    Role,
    States,
    Name,
    Description,
    Interfaces,
    Attributes,
    Text,
    TableRows,
    TableColumns,
    CellSpan,
    ColumnHeaders,
    RowHeaders,
    Value,
    Relations,
    LinkUri,
    /// The language the object's text is in, which a page gives with lang.
    Locale,
    /// How many actions it has, for one that may be clickable without its role saying so.
    Actions,
    /// The name of its first action, which GetActions gives only translated.
    ActionName,
    /// How many objects a subtree holds, up to SUBTREE_LIMIT, by Collection.GetMatches, which stops
    /// counting there; a smaller one is then fetched whole with Tree.
    Size,
    /// A whole subtree in one Collection.GetTree: structure, names, descriptions, attributes and
    /// the interfaces its properties show, leaving roles, states and text to ask for each object.
    Tree,
    /// Name and Description in one Properties.GetAll, where two Properties.Get would take two round
    /// trips. A bridge that refuses GetAll is asked for each with Get instead.
    Props,
}

/// Objects below which a subtree comes in one GetTree. A GetTree holds Firefox's main thread for
/// the whole subtree, about 20 microseconds an object and twice that the first time, so a bigger
/// one would keep every other client waiting.
const SUBTREE_LIMIT: u32 = 200;
/// Collection's canonical order.
const SORT_CANONICAL: u32 = 1;

/// New children of one parent from which a GetTree on the parent replaces one on each.
const PARENT_TREE_CHILDREN: usize = 16;

/// What a subtree's GetTree carries for each object. An interface's property is there only for an
/// object that has the interface.
const TREE_PROPERTIES: [&str; 12] = [
    "Name",
    "Description",
    "Parent",
    "ChildCount",
    "Attributes",
    "Text.CharacterCount",
    "Table.NRows",
    "Table.NColumns",
    "Value.CurrentValue",
    "TableCell.Position",
    "Locale",
    "Action.NActions",
];

/// The match rule `(aiia{ss}iaiiasib)` matching everything; the bridge parses it for GetTree and
/// does not apply it.
type MatchAll = (
    Vec<i32>,
    i32,
    HashMap<String, String>,
    i32,
    Vec<i32>,
    i32,
    Vec<String>,
    i32,
    bool,
);

fn match_all() -> MatchAll {
    (
        vec![0, 0],
        1,
        HashMap::new(),
        1,
        vec![0; 5],
        1,
        Vec::new(),
        1,
        false,
    )
}

/// What is asked of each object walked one at a time.
const BASE: [Field; 6] = [
    Field::Children,
    Field::Role,
    Field::States,
    Field::Props,
    Field::Interfaces,
    Field::Attributes,
];
/// BASE for a bridge that refuses Properties.GetAll.
const BASE_WITHOUT_GET_ALL: [Field; 7] = [
    Field::Children,
    Field::Role,
    Field::States,
    Field::Name,
    Field::Description,
    Field::Interfaces,
    Field::Attributes,
];

fn bit(f: Field) -> u32 {
    1 << f as u32
}

/// What an object needs before it is complete, however it was asked for.
fn base_bits() -> u32 {
    BASE_WITHOUT_GET_ALL.iter().fold(0, |a, f| a | bit(*f))
}

/// What the renderer and the watcher know of one object.
#[derive(Debug, Clone, Default)]
pub struct Obj {
    pub parent: Option<String>,
    pub children: Vec<String>,
    pub role: u32,
    pub states: [u32; 2],
    pub name: String,
    pub description: String,
    pub interfaces: u32,
    pub attributes: Vec<(String, String)>,
    pub text: Option<String>,
    /// Rows and columns, for a table.
    pub table: Option<(i32, i32)>,
    /// Row, column, row span and column span, for a cell.
    pub cell: Option<[i32; 4]>,
    pub column_headers: Vec<String>,
    pub row_headers: Vec<String>,
    pub value: Option<f64>,
    /// Whether a label elsewhere in the document names this object; asked only where it matters.
    pub label_visible: bool,
    pub link_uri: Option<String>,
    /// Its language, asked only of an object with text of its own; empty when it has none.
    pub locale: Option<String>,
    /// How many actions it has and its first action's name, asked only where clickable is not
    /// plain from its role.
    pub n_actions: Option<i32>,
    pub action: Option<String>,
    /// The objects whose text says what is wrong with it, for a field marked invalid.
    pub error_targets: Vec<String>,
    /// Whether another object holds details about it, for one the page gave aria-details.
    pub has_details: bool,
    /// Where it comes in the document: its index in each ancestor up to the walk's start.
    pub key: Vec<u32>,
    /// Fields asked for, and fields answered or given up.
    pub queued: u32,
    pub done: u32,
    pub gone: bool,
    /// Set by the renderer: the object is a field in the buffer.
    pub rendered: bool,
    /// Set by the renderer: its children may be kept as they are when it is rendered again.
    pub child_reuse: bool,
    /// Set by the renderer: a change to it is a change to its parent.
    pub escalate: bool,
    /// Set by the renderer: what its children were rendered with.
    pub child_ctx: crate::render::Ctx,
    /// The interfaces are what a GetTree's properties showed, which leaves out some.
    pub partial_interfaces: bool,
    /// Its data is to come from a GetTree, its own or its parent's.
    pub awaiting_tree: bool,
}

impl Obj {
    pub fn attr(&self, k: &str) -> Option<&str> {
        self.attributes
            .iter()
            .find(|(a, _)| a == k)
            .map(|(_, v)| v.as_str())
    }

    pub fn complete(&self) -> bool {
        self.queued & !self.done == 0 && self.done & base_bits() == base_bits()
    }

    fn known(&self, f: Field) -> bool {
        self.done & bit(f) != 0
    }
}

pub fn has(states: [u32; 2], b: u32) -> bool {
    b < 64 && states[(b / 32) as usize] & (1 << (b % 32)) != 0
}

/// Roles whose name is always what they read as, so a label elsewhere does not change how they
/// are rendered.
fn name_is_content_by_role(role: u32) -> bool {
    let nvda = NVDA_ROLES
        .get(role as usize)
        .copied()
        .unwrap_or(ROLE_UNKNOWN);
    matches!(
        nvda,
        ROLE_LINK
            | ROLE_BUTTON
            | ROLE_TOGGLEBUTTON
            | ROLE_MENUITEM
            | ROLE_GRAPHIC
            | ROLE_HEADING
            | ROLE_TAB
            | ROLE_MENUBUTTON
            | ROLE_STATICTEXT
    )
}

fn role_name(role: u32) -> &'static str {
    ROLE_NAMES.get(role as usize).copied().unwrap_or("")
}

/// The document as rendered, which a walk of part of it consults: where an object sits, and whether
/// a child can be kept as it is.
pub struct Outer {
    pub objs: Rc<RefCell<HashMap<String, Obj>>>,
    /// Changed objects and every ancestor of one.
    pub dirty: HashSet<String>,
    /// Whether children the buffer holds may be kept rather than walked again.
    pub reuse: bool,
}

type Reply = (u64, zbus::Result<Message>, Duration);

pub struct Walk {
    client: Rc<Client>,
    pub objs: HashMap<String, Obj>,
    outer: Option<Outer>,
    heap: BinaryHeap<Reverse<(Vec<u32>, Field, String)>>,
    /// Calls that walk a whole subtree in Firefox, one at a time, so that another client's call
    /// never waits behind more than one of them.
    heavy: BinaryHeap<Reverse<(Vec<u32>, Field, String)>>,
    heavy_inflight: Option<u64>,
    inflight: FuturesUnordered<LocalBoxFuture<'static, Reply>>,
    pending: HashMap<u64, (String, Field)>,
    retried: HashSet<(String, Field)>,
    next_id: u64,
    max_objects: usize,
    deadline: Instant,
    /// Objects every field of which has come back.
    pub complete: usize,
    pub calls: u64,
    pub errors: u64,
    pub timeouts: u64,
    pub truncated: bool,
    pub foreign: usize,
    /// Children kept as they are, which the buffer already holds.
    pub reused: HashSet<String>,
    /// Starting points that no longer exist in the application.
    pub gone: Vec<String>,
    pub first_link: Option<String>,
    /// The longest any one call took, and the call.
    pub slowest: (Duration, Option<Field>, String),
    /// Time spent waiting with the window full.
    started: Instant,
    starts: HashSet<String>,
    /// Fetch small subtrees whole rather than object by object.
    subtrees: bool,
    /// The application refused Properties.GetAll, so names and descriptions are asked one by one.
    no_get_all: bool,
}

impl Walk {
    pub fn new(
        client: Rc<Client>,
        outer: Option<Outer>,
        max_objects: usize,
        max_time: Duration,
    ) -> Self {
        Walk {
            client,
            objs: HashMap::new(),
            outer,
            heap: BinaryHeap::new(),
            heavy: BinaryHeap::new(),
            heavy_inflight: None,
            inflight: FuturesUnordered::new(),
            pending: HashMap::new(),
            retried: HashSet::new(),
            next_id: 0,
            max_objects,
            deadline: Instant::now() + max_time,
            complete: 0,
            calls: 0,
            errors: 0,
            timeouts: 0,
            truncated: false,
            foreign: 0,
            reused: HashSet::new(),
            gone: Vec::new(),
            first_link: None,
            slowest: (Duration::ZERO, None, String::new()),
            started: Instant::now(),
            starts: HashSet::new(),
            subtrees: false,
            no_get_all: false,
        }
    }

    /// What to ask of an object walked one at a time.
    fn base(&self) -> &'static [Field] {
        if self.no_get_all {
            &BASE_WITHOUT_GET_ALL
        } else {
            &BASE
        }
    }

    /// Fetches each subtree under SUBTREE_LIMIT objects with one GetTree, after one GetMatches to
    /// count it, and asks only role, states and text of each object in it.
    pub fn by_subtrees(mut self) -> Self {
        self.subtrees = true;
        self
    }

    /// Fetches path and everything under it, as one more starting point of this walk.
    pub fn start(&mut self, path: &str, key: Vec<u32>) {
        self.starts.insert(path.to_owned());
        self.admit(path, None, key);
    }

    fn admit(&mut self, path: &str, parent: Option<String>, key: Vec<u32>) {
        let o = self.objs.entry(path.to_owned()).or_default();
        o.parent = parent;
        o.key = key;
        for &f in self.base() {
            self.queue(path, f);
        }
    }

    fn queue(&mut self, path: &str, f: Field) {
        self.queue_at(path, f, false);
    }

    /// Queues a call, in document order, or after every call in document order when `last`.
    fn queue_at(&mut self, path: &str, f: Field, last: bool) {
        let Some(o) = self.objs.get_mut(path) else {
            return;
        };
        if o.queued & bit(f) != 0 {
            return;
        }
        o.queued |= bit(f);
        let key = if last {
            // Keys are positions in the tree, so one led by u32::MAX sorts after every real one.
            let mut k = Vec::with_capacity(o.key.len() + 1);
            k.push(u32::MAX);
            k.extend_from_slice(&o.key);
            k
        } else {
            o.key.clone()
        };
        let entry = Reverse((key, f, path.to_owned()));
        if matches!(f, Field::Size | Field::Tree) {
            self.heavy.push(entry);
        } else {
            self.heap.push(entry);
        }
    }

    pub fn seconds(&self) -> f64 {
        self.started.elapsed().as_secs_f64()
    }

    pub fn idle(&self) -> bool {
        self.heap.is_empty() && self.heavy.is_empty() && self.inflight.is_empty()
    }

    /// Sends calls up to the window and takes in one reply. False once nothing is left to do, or
    /// the walk ran out of time.
    pub async fn step(&mut self) -> bool {
        if Instant::now() >= self.deadline {
            if !self.idle() {
                self.truncated = true;
            }
            self.heap.clear();
            self.heavy.clear();
            self.heavy_inflight = None;
            self.inflight = FuturesUnordered::new();
            self.pending.clear();
            return false;
        }
        while self.inflight.len() < WINDOW {
            let heavy_next = self.heavy_inflight.is_none()
                && self
                    .heavy
                    .peek()
                    .is_some_and(|h| self.heap.peek().is_none_or(|l| h.0.0 <= l.0.0));
            let next = if heavy_next {
                self.heavy.pop()
            } else {
                self.heap.pop()
            };
            let Some(Reverse((_, f, path))) = next else {
                break;
            };
            let id = self.next_id;
            if heavy_next {
                self.heavy_inflight = Some(id);
            }
            self.next_id += 1;
            self.calls += 1;
            self.inflight
                .push(issue(self.client.clone(), id, path.clone(), f));
            self.pending.insert(id, (path, f));
        }
        let Some((id, r, took)) = self.inflight.next().await else {
            return false;
        };
        if self.heavy_inflight == Some(id) {
            self.heavy_inflight = None;
        }
        if let Some((path, f)) = self.pending.remove(&id) {
            if took > self.slowest.0 {
                self.slowest = (took, Some(f), path.clone());
            }
            self.reply(path, f, r);
        }
        !self.idle()
    }

    fn reply(&mut self, path: String, f: Field, r: zbus::Result<Message>) {
        if f == Field::Size {
            let small = r
                .ok()
                .and_then(|m| decode::children(&m).ok())
                .is_some_and(|found| (found.len() as u32) < SUBTREE_LIMIT);
            if small {
                if let Some(o) = self.objs.get_mut(&path) {
                    o.awaiting_tree = true;
                }
                self.queue(&path, Field::Tree);
            } else {
                let (parent, key) = self
                    .objs
                    .get(&path)
                    .map(|o| (o.parent.clone(), o.key.clone()))
                    .unwrap_or_default();
                self.admit(&path, parent, key);
            }
            self.finish(&path, f);
            return;
        }
        if f == Field::Tree {
            self.tree(&path, r.as_ref().ok());
            self.finish(&path, f);
            return;
        }
        let msg = match r {
            Ok(m) => m,
            Err(e) => {
                self.errors += 1;
                if is_timeout(&e) {
                    self.timeouts += 1;
                    if self.retried.insert((path.clone(), f)) {
                        // Asked again at the back of the queue, once: the heaps are ordered by
                        // position, where it would come straight back while the application is
                        // still busy.
                        if let Some(o) = self.objs.get_mut(&path) {
                            o.queued &= !bit(f);
                        }
                        self.queue_at(&path, f, true);
                        return;
                    }
                }
                if is_gone(&e) {
                    if let Some(o) = self.objs.get_mut(&path) {
                        o.gone = true;
                    }
                    if self.starts.contains(&path) && !self.gone.contains(&path) {
                        self.gone.push(path.clone());
                    }
                } else if f == Field::Props && !is_timeout(&e) {
                    self.without_get_all(&path);
                    return;
                }
                self.finish(&path, f);
                return;
            }
        };
        if f == Field::Children {
            self.children(&path, &msg);
        } else if let Some(o) = self.objs.get_mut(&path) {
            if decode_into(o, f, &msg).is_err() && f == Field::Props {
                self.without_get_all(&path);
                return;
            }
        }
        self.finish(&path, f);
        self.followups(&path);
    }

    /// The application answered Properties.GetAll with an error or with neither property: ask this
    /// object, and every object after it in this walk, for its name and description one at a time.
    fn without_get_all(&mut self, path: &str) {
        self.no_get_all = true;
        if let Some(o) = self.objs.get_mut(path) {
            // Props is settled without giving the two properties it stands for.
            o.done |= bit(Field::Props);
        }
        self.queue(path, Field::Name);
        self.queue(path, Field::Description);
    }

    fn finish(&mut self, path: &str, f: Field) {
        let Some(o) = self.objs.get_mut(path) else {
            return;
        };
        let was = o.complete();
        o.done |= bit(f);
        if f == Field::Props {
            // GetAll gave both, or the object is gone and neither will come.
            o.done |= bit(Field::Name) | bit(Field::Description);
        }
        if !was && o.complete() {
            self.complete += 1;
        }
    }

    fn children(&mut self, path: &str, msg: &Message) {
        let Ok(kids) = decode::children(msg) else {
            return;
        };
        let app = self.client.app_bus.clone();
        let mut own = Vec::with_capacity(kids.len());
        let mut here = HashSet::with_capacity(kids.len());
        for k in kids {
            if k.is_null() {
                continue;
            }
            if k.bus != app {
                self.foreign += 1;
                continue;
            }
            // An object seen twice is a cycle or a duplicate; it is taken in once.
            if self.objs.contains_key(&k.path) || !here.insert(k.path.clone()) {
                continue;
            }
            own.push(k.path);
        }
        if self.objs.len() + own.len() > self.max_objects {
            self.truncated = true;
            return;
        }
        let key = self
            .objs
            .get(path)
            .map(|o| o.key.clone())
            .unwrap_or_default();
        let new: HashSet<&String> = own.iter().filter(|c| self.is_new(c)).collect();
        // Many new children come cheaper in one GetTree on their parent, which Firefox answers in a
        // fraction of the time it takes to answer one for each.
        let through_parent = new.len() >= PARENT_TREE_CHILDREN && new.len() * 2 >= own.len();
        for (i, c) in own.iter().enumerate() {
            let mut k = key.clone();
            k.push(i as u32);
            if new.contains(c) {
                self.admit_tree(c, Some(path.to_owned()), k, !through_parent);
            } else if self.subtrees && self.outer.is_none() {
                let o = self.objs.entry(c.clone()).or_default();
                o.parent = Some(path.to_owned());
                o.key = k;
                self.queue(c, Field::Size);
            } else if self.reusable(c, path) {
                self.reused.insert(c.clone());
            } else {
                self.admit(c, Some(path.to_owned()), k);
            }
        }
        if through_parent {
            self.queue(path, Field::Tree);
        }
        if let Some(o) = self.objs.get_mut(path) {
            o.children = own;
        }
    }

    /// An object the document did not hold before a change, in a walk of the changed parts: its
    /// whole subtree is new, and comes cheapest in one GetTree.
    fn is_new(&self, path: &str) -> bool {
        self.outer
            .as_ref()
            .is_some_and(|o| !o.objs.borrow().contains_key(path))
    }

    fn admit_tree(&mut self, path: &str, parent: Option<String>, key: Vec<u32>, own_call: bool) {
        let o = self.objs.entry(path.to_owned()).or_default();
        o.parent = parent;
        o.key = key;
        o.awaiting_tree = true;
        if own_call {
            self.queue(path, Field::Tree);
        }
    }

    /// Takes in a subtree from its GetTree: the objects waiting for it, and everything new under
    /// them. Without an answer, they are asked for object by object.
    fn tree(&mut self, path: &str, msg: Option<&Message>) {
        let entries = msg.and_then(|m| tree_entries(m, &self.client.app_bus).ok());
        let Some(entries) = entries.filter(|e| e.first().is_some_and(|f| f.path == path)) else {
            let mut waiting: Vec<String> = Vec::new();
            if let Some(o) = self.objs.get(path) {
                if o.awaiting_tree {
                    waiting.push(path.to_owned());
                }
                waiting.extend(
                    o.children
                        .iter()
                        .filter(|c| self.objs.get(*c).is_some_and(|k| k.awaiting_tree))
                        .cloned(),
                );
            }
            for w in waiting {
                if let Some(o) = self.objs.get_mut(&w) {
                    o.awaiting_tree = false;
                }
                for &f in self.base() {
                    self.queue(&w, f);
                }
            }
            return;
        };
        let given = bit(Field::Children)
            | bit(Field::Name)
            | bit(Field::Description)
            | bit(Field::Interfaces)
            | bit(Field::Attributes)
            | bit(Field::TableRows)
            | bit(Field::TableColumns);
        let mut counts: HashMap<String, usize> = HashMap::new();
        let mut taken: Vec<String> = Vec::with_capacity(entries.len());
        let mut filled: HashSet<String> = HashSet::new();
        for e in entries {
            let (parent, key) = if let Some(o) = self.objs.get(&e.path) {
                if !o.awaiting_tree {
                    continue;
                }
                (o.parent.clone(), o.key.clone())
            } else {
                let Some(p) = e.parent.clone().filter(|p| filled.contains(p)) else {
                    continue;
                };
                if self.objs.len() >= self.max_objects {
                    self.truncated = true;
                    continue;
                }
                let n = counts.entry(p.clone()).or_default();
                let mut key = self.objs[&p].key.clone();
                key.push(*n as u32);
                *n += 1;
                if let Some(po) = self.objs.get_mut(&p) {
                    po.children.push(e.path.clone());
                }
                (Some(p), key)
            };
            let o = self.objs.entry(e.path.clone()).or_default();
            o.parent = parent;
            o.key = key;
            o.awaiting_tree = false;
            filled.insert(e.path.clone());
            o.name = e.name;
            o.description = e.description;
            o.attributes = e.attributes;
            o.interfaces = iface_bit(IFACE_ACCESSIBLE);
            o.partial_interfaces = true;
            if let Some(chars) = e.characters {
                o.interfaces |= iface_bit(IFACE_TEXT);
                if chars == 0 {
                    o.text = Some(String::new());
                    o.queued |= bit(Field::Text);
                    o.done |= bit(Field::Text);
                } else if Some(chars) == e.child_count {
                    // Text that is all embedded objects, one for each child.
                    o.text = Some("\u{fffc}".repeat(chars as usize));
                    o.queued |= bit(Field::Text);
                    o.done |= bit(Field::Text);
                } else if e.child_count == Some(0)
                    && o.attr("explicit-name") != Some("true")
                    && o.name.encode_utf16().count() == chars as usize
                {
                    // A leaf named from its own text, such as a link or a heading, reads as its name.
                    o.text = Some(o.name.clone());
                    o.queued |= bit(Field::Text);
                    o.done |= bit(Field::Text);
                }
            }
            if let (Some(r), Some(c)) = (e.rows, e.columns) {
                o.interfaces |= iface_bit(IFACE_TABLE);
                o.table = Some((r, c));
            }
            if let Some(v) = e.value {
                o.interfaces |= iface_bit(IFACE_VALUE);
                o.value = Some(v);
                o.queued |= bit(Field::Value);
                o.done |= bit(Field::Value);
            }
            if e.cell {
                o.interfaces |= iface_bit(IFACE_TABLE_CELL);
            }
            if let Some(locale) = e.locale {
                o.locale = Some(locale);
                o.queued |= bit(Field::Locale);
                o.done |= bit(Field::Locale);
            }
            if let Some(n) = e.actions {
                o.interfaces |= iface_bit(IFACE_ACTION);
                o.n_actions = Some(n);
                o.queued |= bit(Field::Actions);
                o.done |= bit(Field::Actions);
            }
            o.queued |= given;
            o.done |= given;
            taken.push(e.path);
        }
        for p in taken {
            self.queue(&p, Field::Role);
            self.queue(&p, Field::States);
        }
    }

    fn reusable(&self, child: &str, parent: &str) -> bool {
        let Some(outer) = &self.outer else {
            return false;
        };
        if !outer.reuse || outer.dirty.contains(child) {
            return false;
        }
        let objs = outer.objs.borrow();
        let (Some(c), Some(p)) = (objs.get(child), objs.get(parent)) else {
            return false;
        };
        p.rendered && p.child_reuse && c.rendered && c.parent.as_deref() == Some(parent)
    }

    fn parent_of(&self, path: &str) -> Option<String> {
        if let Some(o) = self.objs.get(path)
            && o.parent.is_some()
        {
            return o.parent.clone();
        }
        self.outer.as_ref()?.objs.borrow().get(path)?.parent.clone()
    }

    fn role_of(&self, path: &str) -> Option<u32> {
        if let Some(o) = self.objs.get(path) {
            return o.known(Field::Role).then_some(o.role);
        }
        self.outer.as_ref()?.objs.borrow().get(path).map(|o| o.role)
    }

    /// Queues the calls an answer has shown to be needed.
    fn followups(&mut self, path: &str) {
        let Some(o) = self.objs.get(path) else { return };
        let mut more = Vec::new();
        if o.known(Field::Interfaces) {
            let i = o.interfaces;
            if i & iface_bit(IFACE_TEXT) != 0 {
                more.push(Field::Text);
            }
            if o.text
                .as_deref()
                .is_some_and(|t| t.chars().any(|c| c != '\u{fffc}' && !c.is_whitespace()))
            {
                more.push(Field::Locale);
            }
            if i & iface_bit(IFACE_ACTION) != 0
                && o.known(Field::Role)
                && !NOT_CLICKABLE_ROLES.contains(&role_name(o.role))
            {
                more.push(Field::Actions);
                if o.n_actions.is_some_and(|n| n > 0) {
                    more.push(Field::ActionName);
                }
            }
            if i & iface_bit(IFACE_TABLE) != 0 {
                more.extend([Field::TableRows, Field::TableColumns]);
            }
            let rn = role_name(o.role);
            let cell_role =
                o.known(Field::Role) && matches!(rn, "table cell" | "column header" | "row header");
            if i & iface_bit(IFACE_TABLE_CELL) != 0 || (o.partial_interfaces && cell_role) {
                more.extend([Field::CellSpan, Field::ColumnHeaders, Field::RowHeaders]);
            }
            let value_role = o.known(Field::Role)
                && matches!(
                    rn,
                    "slider" | "spin button" | "progress bar" | "level bar" | "scroll bar" | "dial"
                );
            if (i & iface_bit(IFACE_VALUE) != 0 || (o.partial_interfaces && value_role))
                && o.known(Field::Attributes)
                && o.attr("valuetext").is_none()
            {
                more.push(Field::Value);
            }
        }
        if (o.known(Field::States) && has(o.states, STATE_INVALID_ENTRY))
            || (o.known(Field::Attributes) && o.attr("details-from").is_some())
        {
            more.push(Field::Relations);
        }
        if o.known(Field::Attributes)
            && o.known(Field::Role)
            && o.attr("explicit-name") == Some("true")
        {
            let nvda = NVDA_ROLES
                .get(o.role as usize)
                .copied()
                .unwrap_or(ROLE_UNKNOWN);
            if !name_is_content_by_role(o.role)
                || nvda == ROLE_CHECKBOX
                || role_name(o.role) == "radio button"
            {
                more.push(Field::Relations);
            }
        }
        let mut link = None;
        if o.known(Field::Role) && o.known(Field::Name) {
            let rn = role_name(o.role);
            if rn == "link" && self.first_link.is_none() {
                self.first_link = Some(path.to_owned());
            }
            if rn == "image" && o.name.is_empty() {
                link = self.link_ancestor(path);
            }
            // Firefox names a link from what it holds, so a link without a name holds nothing worth
            // reading: an image with empty alt text, which Firefox leaves out, or empty containers.
            // fillVBuf reads such a link by its address.
            if rn == "link" && o.name.is_empty() {
                more.push(Field::LinkUri);
            }
        }
        for f in more {
            self.queue(path, f);
        }
        if let Some(l) = link
            && self.objs.contains_key(&l)
        {
            self.queue(&l, Field::LinkUri);
        }
    }

    /// The nearest link above an unlabelled image, whose address names it.
    fn link_ancestor(&self, path: &str) -> Option<String> {
        let mut p = self.parent_of(path);
        for _ in 0..4 {
            let here = p?;
            if self.role_of(&here).is_some_and(|r| role_name(r) == "link") {
                return Some(here);
            }
            p = self.parent_of(&here);
        }
        None
    }
}

fn issue(client: Rc<Client>, id: u64, path: String, f: Field) -> LocalBoxFuture<'static, Reply> {
    Box::pin(async move {
        let t = Instant::now();
        let p = path.as_str();
        let r = match f {
            Field::Children => client.call(p, IFACE_ACCESSIBLE, "GetChildren", &()).await,
            Field::Role => client.call(p, IFACE_ACCESSIBLE, "GetRole", &()).await,
            Field::States => client.call(p, IFACE_ACCESSIBLE, "GetState", &()).await,
            Field::Name => {
                client
                    .call(p, IFACE_PROPERTIES, "Get", &(IFACE_ACCESSIBLE, "Name"))
                    .await
            }
            Field::Description => {
                client
                    .call(
                        p,
                        IFACE_PROPERTIES,
                        "Get",
                        &(IFACE_ACCESSIBLE, "Description"),
                    )
                    .await
            }
            Field::Props => {
                client
                    .call(p, IFACE_PROPERTIES, "GetAll", &(IFACE_ACCESSIBLE,))
                    .await
            }
            Field::Interfaces => client.call(p, IFACE_ACCESSIBLE, "GetInterfaces", &()).await,
            Field::Attributes => client.call(p, IFACE_ACCESSIBLE, "GetAttributes", &()).await,
            Field::Text => client.call(p, IFACE_TEXT, "GetText", &(0i32, -1i32)).await,
            Field::TableRows => {
                client
                    .call(p, IFACE_PROPERTIES, "Get", &(IFACE_TABLE, "NRows"))
                    .await
            }
            Field::TableColumns => {
                client
                    .call(p, IFACE_PROPERTIES, "Get", &(IFACE_TABLE, "NColumns"))
                    .await
            }
            Field::CellSpan => {
                client
                    .call(p, IFACE_TABLE_CELL, "GetRowColumnSpan", &())
                    .await
            }
            Field::ColumnHeaders => {
                client
                    .call(p, IFACE_TABLE_CELL, "GetColumnHeaderCells", &())
                    .await
            }
            Field::RowHeaders => {
                client
                    .call(p, IFACE_TABLE_CELL, "GetRowHeaderCells", &())
                    .await
            }
            Field::Value => {
                client
                    .call(p, IFACE_PROPERTIES, "Get", &(IFACE_VALUE, "CurrentValue"))
                    .await
            }
            Field::Relations => {
                client
                    .call(p, IFACE_ACCESSIBLE, "GetRelationSet", &())
                    .await
            }
            Field::LinkUri => client.call(p, IFACE_HYPERLINK, "GetURI", &(0i32,)).await,
            Field::Locale => {
                client
                    .call(p, IFACE_PROPERTIES, "Get", &(IFACE_ACCESSIBLE, "Locale"))
                    .await
            }
            Field::Actions => {
                client
                    .call(p, IFACE_PROPERTIES, "Get", &(IFACE_ACTION, "NActions"))
                    .await
            }
            Field::ActionName => client.call(p, IFACE_ACTION, "GetName", &(0i32,)).await,
            Field::Size => {
                client
                    .call(
                        p,
                        IFACE_COLLECTION,
                        "GetMatches",
                        &(match_all(), SORT_CANONICAL, SUBTREE_LIMIT as i32, true),
                    )
                    .await
            }
            Field::Tree => {
                client
                    .call(
                        p,
                        IFACE_COLLECTION,
                        "GetTree",
                        &(match_all(), TREE_PROPERTIES.to_vec()),
                    )
                    .await
            }
        };
        (id, r, t.elapsed())
    })
}

fn variant_number(v: &Variant<'_>) -> Option<f64> {
    match v {
        Variant::I32(i) => Some(*i as f64),
        Variant::U32(u) => Some(*u as f64),
        Variant::I64(i) => Some(*i as f64),
        Variant::U64(u) => Some(*u as f64),
        Variant::I16(i) => Some(*i as f64),
        Variant::U16(u) => Some(*u as f64),
        Variant::U8(u) => Some(*u as f64),
        Variant::F64(d) => Some(*d),
        Variant::Variant(inner) => variant_number(inner),
        _ => None,
    }
}

fn number_property(msg: &Message) -> zbus::Result<f64> {
    let body = msg.body();
    let v: Variant<'_> = body.deserialize()?;
    variant_number(&v).ok_or(zbus::Error::IncorrectType)
}

fn row_column_span(msg: &Message) -> zbus::Result<[i32; 4]> {
    let body = msg.body();
    if body.signature().to_string().starts_with('b') {
        let (_, r, c, rs, cs): (bool, i32, i32, i32, i32) = body.deserialize()?;
        return Ok([r, c, rs, cs]);
    }
    let (r, c, rs, cs): (i32, i32, i32, i32) = body.deserialize()?;
    Ok([r, c, rs, cs])
}

fn relations(o: &mut Obj, msg: &Message) -> zbus::Result<()> {
    let body = msg.body();
    let set: Vec<(u32, Vec<(&str, ObjectPath<'_>)>)> = body.deserialize()?;
    let targets = |kind: u32| -> Vec<String> {
        set.iter()
            .filter(|(k, _)| *k == kind)
            .flat_map(|(_, t)| t.iter().map(|(_, p)| p.as_str().to_owned()))
            .collect()
    };
    o.label_visible = !targets(RELATION_LABELLED_BY).is_empty();
    o.error_targets = targets(RELATION_ERROR_MESSAGE);
    o.has_details = !targets(RELATION_DETAILS).is_empty();
    Ok(())
}

fn decode_into(o: &mut Obj, f: Field, msg: &Message) -> zbus::Result<()> {
    match f {
        Field::Children => {}
        Field::Role => o.role = decode::role(msg)?,
        Field::States => o.states = decode::states(msg)?,
        Field::Name => o.name = decode::string_property(msg)?,
        Field::Description => o.description = decode::string_property(msg)?,
        Field::Props => (o.name, o.description) = decode::name_and_description(msg)?,
        Field::Interfaces => o.interfaces = decode::interfaces(msg)?,
        Field::Attributes => o.attributes = decode::attributes(msg)?,
        Field::Text => o.text = Some(decode::string(msg)?),
        Field::TableRows => {
            let n = number_property(msg)? as i32;
            o.table = Some((n, o.table.map_or(0, |t| t.1)));
        }
        Field::TableColumns => {
            let n = number_property(msg)? as i32;
            o.table = Some((o.table.map_or(0, |t| t.0), n));
        }
        Field::CellSpan => o.cell = Some(row_column_span(msg)?),
        Field::ColumnHeaders => {
            o.column_headers = decode::children(msg)?.into_iter().map(|r| r.path).collect()
        }
        Field::RowHeaders => {
            o.row_headers = decode::children(msg)?.into_iter().map(|r| r.path).collect()
        }
        Field::Value => o.value = Some(number_property(msg)?),
        Field::Relations => relations(o, msg)?,
        Field::LinkUri => o.link_uri = Some(decode::string(msg)?),
        Field::Locale => o.locale = Some(decode::string_property(msg)?),
        Field::Actions => o.n_actions = Some(number_property(msg)? as i32),
        Field::ActionName => o.action = Some(decode::string(msg)?),
        Field::Size | Field::Tree => {}
    }
    Ok(())
}

/// One object of a GetTree reply.
struct Entry {
    path: String,
    parent: Option<String>,
    name: String,
    description: String,
    child_count: Option<i32>,
    attributes: Vec<(String, String)>,
    characters: Option<i32>,
    rows: Option<i32>,
    columns: Option<i32>,
    value: Option<f64>,
    cell: bool,
    locale: Option<String>,
    actions: Option<i32>,
}

fn variant_str<'v>(v: &'v Variant<'_>) -> Option<&'v str> {
    match v {
        Variant::Str(s) => Some(s.as_str()),
        Variant::Variant(inner) => variant_str(inner),
        _ => None,
    }
}

fn variant_parent(v: &Variant<'_>) -> Option<(String, String)> {
    match v {
        Variant::Structure(s) => match s.fields() {
            [Variant::Str(bus), Variant::ObjectPath(p)] => {
                Some((bus.as_str().to_owned(), p.as_str().to_owned()))
            }
            _ => None,
        },
        Variant::Variant(inner) => variant_parent(inner),
        _ => None,
    }
}

fn variant_attributes(v: &Variant<'_>) -> Option<Vec<(String, String)>> {
    match v {
        Variant::Dict(d) => {
            let mut out = Vec::new();
            for (k, val) in d.iter() {
                out.push((variant_str(k)?.to_owned(), variant_str(val)?.to_owned()));
            }
            out.sort();
            Some(out)
        }
        Variant::Variant(inner) => variant_attributes(inner),
        _ => None,
    }
}

type RawEntry<'a> = ((&'a str, ObjectPath<'a>), HashMap<&'a str, Variant<'a>>);

/// The entries of a GetTree reply that belong to the application, in its order: each parent before
/// its children, and children in order.
fn tree_entries(msg: &Message, app: &str) -> zbus::Result<Vec<Entry>> {
    let body = msg.body();
    let raw: Vec<RawEntry<'_>> = body.deserialize()?;
    let mut out = Vec::with_capacity(raw.len());
    for ((bus, path), props) in &raw {
        if *bus != app {
            continue;
        }
        let number = |k: &str| props.get(k).and_then(variant_number).map(|n| n as i32);
        out.push(Entry {
            path: path.as_str().to_owned(),
            parent: props
                .get("Parent")
                .and_then(variant_parent)
                .filter(|(b, _)| b == app)
                .map(|(_, p)| p),
            name: props
                .get("Name")
                .and_then(variant_str)
                .unwrap_or("")
                .to_owned(),
            description: props
                .get("Description")
                .and_then(variant_str)
                .unwrap_or("")
                .to_owned(),
            child_count: number("ChildCount"),
            attributes: props
                .get("Attributes")
                .and_then(variant_attributes)
                .unwrap_or_default(),
            characters: number("Text.CharacterCount"),
            rows: number("Table.NRows"),
            columns: number("Table.NColumns"),
            value: props.get("Value.CurrentValue").and_then(variant_number),
            cell: props.contains_key("TableCell.Position"),
            locale: props.get("Locale").and_then(variant_str).map(str::to_owned),
            actions: number("Action.NActions"),
        });
    }
    Ok(out)
}
