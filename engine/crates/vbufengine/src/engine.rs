//! The engine's own thread: one connection to the accessibility bus for signals, one peer-to-peer
//! connection to each application for calls, the documents rendered and watched, and the updates
//! their changes make. Everything here runs on that one thread, so nothing is shared but the queues
//! in lib.rs.

use std::cell::{Cell, RefCell};
use std::collections::{HashMap, HashSet};
use std::future::poll_fn;
use std::num::NonZeroUsize;
use std::rc::Rc;
use std::sync::Arc;
use std::sync::mpsc;
use std::task::Poll;
use std::time::{Duration, Instant};

use futures_util::FutureExt;
use futures_util::future::LocalBoxFuture;
use futures_util::stream::{FuturesUnordered, SelectAll, StreamExt};
use mirror::client::Client;
use mirror::consts::{IFACE_ACCESSIBLE, IFACE_PROPERTIES};
use mirror::decode;
use mirror::discovery::{self, Timeouts};
use mirror::{app, time};
use serde_json::{Map, Value, json};
use zbus::message::Type;
use zbus::{Connection, MatchRule, Message, MessageStream};

use crate::render::{Mark, Renderer};
use crate::walk::{Obj, Outer, Walk, has};
use crate::{Inbox, RenderReply, Request, Shared};

const IFACE_DOCUMENT: &str = "org.a11y.atspi.Document";
const IFACE_ACTION: &str = "org.a11y.atspi.Action";
const EVENT_OBJECT: &str = "org.a11y.atspi.Event.Object";
const EVENT_DOCUMENT: &str = "org.a11y.atspi.Event.Document";
/// The events registered with the registry, so that applications send them at all.
const EVENTS: [&str; 5] = [
    "object:children-changed",
    "object:text-changed",
    "object:property-change",
    "object:state-changed",
    "document:",
];
/// Property changes that change what a document reads as. The rest, above all the accessible-parent
/// Firefox sends for every object the first time it is touched, are left with the bus.
const PROPERTIES: [&str; 4] = [
    "accessible-name",
    "accessible-description",
    "accessible-role",
    "accessible-value",
];
/// State changes that change nothing the buffer holds, or that come in floods.
const IGNORED_STATES: [&str; 6] = [
    "focused",
    "showing",
    "active",
    "stale",
    "armed",
    "selectable-text",
];
/// Changes are gathered this long after the first before the changed parts are walked again.
const COALESCE: Duration = Duration::from_millis(100);
/// The top of a large document is returned once this many objects are complete, or once this much
/// time has passed, whichever comes first.
const FIRST_PART_OBJECTS: usize = 2_000;
const FIRST_PART_TIME: Duration = Duration::from_millis(300);
const MAX_OBJECTS: usize = 500_000;
const MAX_WALK_TIME: Duration = Duration::from_secs(120);
const SIGNAL_QUEUE: usize = 1 << 20;

/// Calls over the bus: activation can take a second and more on a large page.
fn bus_timeouts() -> Timeouts {
    Timeouts {
        method: Duration::from_secs(10),
        connect: Duration::from_secs(5),
    }
}

/// Calls over an application's own connection, each of which has a second.
fn p2p_timeouts() -> Timeouts {
    Timeouts {
        method: Duration::from_secs(1),
        connect: Duration::from_secs(5),
    }
}

struct App {
    client: Rc<Client>,
    primed: Cell<bool>,
    actions_primed: Cell<bool>,
}

#[derive(Default)]
struct Pending {
    /// The first walk is still running; what changed meanwhile is looked at once it has finished.
    walking: bool,
    early: Vec<(String, String)>,
    invalid: HashSet<String>,
    since: Option<Instant>,
    url_check: bool,
    updating: bool,
    unwatched: bool,
    /// The first few signals behind the next update, for its info.
    why: Vec<String>,
}

struct Doc {
    bus: String,
    root: String,
    handle: i32,
    reuse: bool,
    frame: RefCell<Option<String>>,
    url: RefCell<String>,
    objs: Rc<RefCell<HashMap<String, Obj>>>,
    p: RefCell<Pending>,
}

struct State {
    shared: Arc<Shared>,
    bus: RefCell<Option<Connection>>,
    registered: Cell<bool>,
    apps: RefCell<HashMap<String, Rc<App>>>,
    docs: RefCell<HashMap<(String, String), Rc<Doc>>>,
    new_streams: RefCell<Vec<MessageStream>>,
    spawned: RefCell<Vec<LocalBoxFuture<'static, ()>>>,
}

fn reply_error(reply: &mpsc::Sender<RenderReply>, mut info: Map<String, Value>, error: String) {
    info.insert("error".into(), error.into());
    let _ = reply.send(RenderReply {
        stream: None,
        info: Value::Object(info).to_string(),
    });
}

impl State {
    async fn bus(&self) -> Result<Connection, String> {
        if let Some(c) = self.bus.borrow().as_ref() {
            return Ok(c.clone());
        }
        let address = discovery::find_bus_address(bus_timeouts())
            .await
            .map_err(|e| e.to_string())?;
        let conn = discovery::connect_bus(&address.address, bus_timeouts())
            .await
            .map_err(|e| e.to_string())?;
        *self.bus.borrow_mut() = Some(conn.clone());
        Ok(conn)
    }

    async fn app(
        &self,
        bus_name: &str,
        doc: &str,
        info: &mut Map<String, Value>,
    ) -> Result<Rc<App>, String> {
        if let Some(a) = self.apps.borrow().get(bus_name) {
            info.insert("connectSeconds".into(), 0.0.into());
            return Ok(a.clone());
        }
        let t = Instant::now();
        let conn = self.bus().await?;
        // Subscribed before anything is fetched, so that no change after the fetch is missed.
        let streams = subscribe(&conn, bus_name)
            .await
            .map_err(|e| format!("subscribing: {e}"))?;
        if !self.registered.get() {
            for e in EVENTS {
                app::register_event(&conn, e)
                    .await
                    .map_err(|err| format!("registering {e}: {err}"))?;
            }
            self.registered.set(true);
        }
        let owner = app::owner(&conn, bus_name)
            .await
            .map_err(|e| e.to_string())?;
        let address = private_address(&Client::bus(conn.clone(), bus_name), doc, info).await?;
        let client = if address.is_empty() {
            Client::bus(conn.clone(), bus_name)
        } else {
            let p2p = discovery::connect_p2p(&address, owner, p2p_timeouts())
                .await
                .map_err(|e| e.to_string())?;
            Client::p2p(p2p, bus_name)
        };
        info.insert("connectSeconds".into(), t.elapsed().as_secs_f64().into());
        if let Some(a) = self.apps.borrow().get(bus_name) {
            return Ok(a.clone());
        }
        self.new_streams.borrow_mut().extend(streams);
        let a = Rc::new(App {
            client: Rc::new(client),
            primed: Cell::new(false),
            actions_primed: Cell::new(false),
        });
        self.apps
            .borrow_mut()
            .insert(bus_name.to_owned(), a.clone());
        Ok(a)
    }

    fn docs_of(&self, bus: &str) -> Vec<Rc<Doc>> {
        self.docs
            .borrow()
            .values()
            .filter(|d| d.bus == bus)
            .cloned()
            .collect()
    }

    fn doc(&self, bus: &str, path: &str) -> Option<Rc<Doc>> {
        self.docs
            .borrow()
            .get(&(bus.to_owned(), path.to_owned()))
            .cloned()
    }

    fn is_current(&self, d: &Rc<Doc>) -> bool {
        self.doc(&d.bus, &d.root).is_some_and(|c| Rc::ptr_eq(&c, d))
    }

    fn drop_doc(&self, d: &Rc<Doc>) {
        d.p.borrow_mut().unwatched = true;
        let key = (d.bus.clone(), d.root.clone());
        let mut docs = self.docs.borrow_mut();
        if docs.get(&key).is_some_and(|c| Rc::ptr_eq(c, d)) {
            docs.remove(&key);
        }
    }

    fn emit(
        &self,
        bus: &str,
        doc: &str,
        events: &[&str],
        stream: Option<Vec<u16>>,
        mut info: Map<String, Value>,
    ) {
        info.insert("bus".into(), bus.into());
        info.insert("doc".into(), doc.into());
        info.insert("events".into(), json!(events));
        if !info.contains_key("docURL") {
            let url = self
                .doc(bus, doc)
                .map(|d| d.url.borrow().clone())
                .unwrap_or_default();
            info.insert("docURL".into(), url.into());
        }
        self.shared.push(stream, Value::Object(info).to_string());
    }

    fn lifecycle(&self, d: &Rc<Doc>, event: &str) {
        self.emit(&d.bus, &d.root, &[event], None, Map::new());
    }

    /// Marks the rendered object nearest path as changed, in whichever watched document holds it.
    fn invalidate(&self, bus: &str, path: &str, why: &str) {
        for d in self.docs_of(bus) {
            let mut p = d.p.borrow_mut();
            if p.unwatched {
                continue;
            }
            // The objects a walk in progress finds are not known until it ends, and a change to one
            // of them would be lost; it is looked at once the walk's objects are in.
            if p.walking || p.updating {
                p.early
                    .push((path.to_owned(), format!("{why} while walking")));
                continue;
            }
            let objs = d.objs.borrow();
            if let Some(t) = target_for(&objs, path, &d.root) {
                if p.why.len() < 8 {
                    p.why
                        .push(format!("{why} {}", path.rsplit('/').next().unwrap_or("")));
                }
                p.invalid.insert(t);
                p.since.get_or_insert_with(Instant::now);
            }
        }
    }

    fn check_url(&self, d: &Rc<Doc>) {
        let mut p = d.p.borrow_mut();
        p.url_check = true;
        p.since.get_or_insert_with(Instant::now);
    }

    fn on_signal(self: &Rc<Self>, msg: &Message) {
        let header = msg.header();
        if header
            .member()
            .is_some_and(|m| m.as_str() == "NameOwnerChanged")
        {
            let body = msg.body();
            if let Ok((name, _old, new)) = body.deserialize::<(&str, &str, &str)>()
                && new.is_empty()
            {
                self.app_gone(name);
            }
            return;
        }
        let Ok(Some(e)) = decode::event(msg) else {
            return;
        };
        match (e.class.as_str(), e.member.as_str()) {
            ("Document", "LoadComplete") => self.load_complete(&e.sender, &e.path),
            ("Document", "Reload") => {
                if let Some(d) = self.doc(&e.sender, &e.path) {
                    self.lifecycle(&d, "reload");
                }
            }
            ("Object", "ChildrenChanged") => {
                // A text leaf comes and goes with index -1; the TextChanged after it says what changed.
                if e.detail1 >= 0 {
                    self.invalidate(&e.sender, &e.path, &format!("children-changed:{}", e.minor));
                }
            }
            ("Object", "TextChanged") => {
                self.invalidate(&e.sender, &e.path, &format!("text-changed:{}", e.minor))
            }
            ("Object", "StateChanged") => {
                let state = e.minor.split(':').next().unwrap_or("");
                let root = self.doc(&e.sender, &e.path);
                match (state, root) {
                    // A link to a place in the same page moves focus to the document, and nothing else
                    // says the address changed.
                    ("focused", Some(d)) if e.detail1 == 1 => self.check_url(&d),
                    ("defunct", Some(d)) if e.detail1 == 1 => {
                        self.lifecycle(&d, "gone");
                        self.drop_doc(&d);
                    }
                    ("busy", Some(_)) => {}
                    (s, _) if IGNORED_STATES.contains(&s) => {}
                    _ => self.invalidate(
                        &e.sender,
                        &e.path,
                        &format!("state-changed:{} {}", e.minor, e.detail1),
                    ),
                }
            }
            ("Object", "PropertyChange") => {
                self.invalidate(&e.sender, &e.path, &format!("property-change:{}", e.minor));
                if e.minor == "accessible-name"
                    && let Some(d) = self.doc(&e.sender, &e.path)
                {
                    self.check_url(&d);
                }
            }
            _ => {}
        }
    }

    fn load_complete(self: &Rc<Self>, bus: &str, path: &str) {
        if let Some(d) = self.doc(bus, path) {
            self.lifecycle(&d, "loadComplete");
            self.check_url(&d);
            return;
        }
        if self.docs_of(bus).is_empty() {
            return;
        }
        self.emit(bus, path, &["loadComplete"], None, Map::new());
        // A new document in a frame that held a watched one replaces it.
        let st = self.clone();
        let bus = bus.to_owned();
        let path = path.to_owned();
        self.spawned.borrow_mut().push(Box::pin(async move {
            let Some(a) = st.apps.borrow().get(&bus).cloned() else {
                return;
            };
            let Some(frame) = parent_of(&a.client, &path).await else {
                return;
            };
            for d in st.docs_of(&bus) {
                if d.root != path && d.frame.borrow().as_deref() == Some(frame.as_str()) {
                    st.lifecycle(&d, "gone");
                    st.drop_doc(&d);
                }
            }
            st.invalidate(&bus, &frame, "load-complete");
        }));
    }

    fn app_gone(&self, bus: &str) {
        if self.apps.borrow_mut().remove(bus).is_none() {
            return;
        }
        for d in self.docs_of(bus) {
            self.lifecycle(&d, "gone");
            self.drop_doc(&d);
        }
    }

    fn unwatch(&self, bus: &str, doc: &str) {
        if let Some(d) = self.doc(bus, doc) {
            self.drop_doc(&d);
        }
    }

    /// Documents whose changes have waited long enough, and when the next one will have.
    fn due(&self, now: Instant) -> (Vec<Rc<Doc>>, Option<Instant>) {
        let mut ready = Vec::new();
        let mut next: Option<Instant> = None;
        for d in self.docs.borrow().values() {
            let p = d.p.borrow();
            if p.walking || p.updating || p.unwatched {
                continue;
            }
            let Some(since) = p.since else { continue };
            let at = since + COALESCE;
            if at <= now {
                ready.push(d.clone());
            } else {
                next = Some(next.map_or(at, |n| n.min(at)));
            }
        }
        (ready, next)
    }
}

/// The rendered object a change to path changes: path or its nearest rendered ancestor, or the
/// parent of one that is rendered as part of its parent. None when path is not in the document.
fn target_for(objs: &HashMap<String, Obj>, path: &str, root: &str) -> Option<String> {
    let mut cur = path.to_owned();
    loop {
        let o = objs.get(&cur)?;
        if o.rendered {
            break;
        }
        cur = o.parent.clone()?;
    }
    while objs.get(&cur).is_some_and(|o| o.escalate) && cur != root {
        let mut up = objs.get(&cur).and_then(|o| o.parent.clone());
        loop {
            let Some(u) = up else { return Some(cur) };
            let Some(o) = objs.get(&u) else {
                return Some(cur);
            };
            if o.rendered {
                cur = u;
                break;
            }
            up = o.parent.clone();
        }
    }
    Some(cur)
}

async fn subscribe(conn: &Connection, bus: &str) -> zbus::Result<Vec<MessageStream>> {
    let mut rules = Vec::new();
    for member in ["ChildrenChanged", "TextChanged", "StateChanged"] {
        rules.push(
            MatchRule::builder()
                .msg_type(Type::Signal)
                .sender(bus)
                .interface(EVENT_OBJECT)
                .member(member)
                .build()?,
        );
    }
    for property in PROPERTIES {
        rules.push(
            MatchRule::builder()
                .msg_type(Type::Signal)
                .sender(bus)
                .interface(EVENT_OBJECT)
                .member("PropertyChange")
                .arg(0, property)
                .build()?,
        );
    }
    rules.push(
        MatchRule::builder()
            .msg_type(Type::Signal)
            .sender(bus)
            .interface(EVENT_DOCUMENT)
            .build()?,
    );
    rules.push(
        MatchRule::builder()
            .msg_type(Type::Signal)
            .sender("org.freedesktop.DBus")
            .interface("org.freedesktop.DBus")
            .member("NameOwnerChanged")
            .arg(0, bus)
            .build()?,
    );
    let mut streams = Vec::new();
    for rule in rules {
        streams.push(
            MessageStream::for_match_rule(rule, conn, NonZeroUsize::new(SIGNAL_QUEUE)).await?,
        );
    }
    Ok(streams)
}

/// The address of the application's own server, which also activates its bridge, or an empty
/// string when it is to be read over the bus. Not every application keeps its root at the standard
/// path: WebKitGTK's web process keeps it under /org/a11y/webkit and offers no server of its own, so
/// the root the document names is asked next, and an application that answers neither is read over
/// the bus. Only a call that failed without an answer fails the render.
async fn private_address(
    bus: &Client,
    doc: &str,
    info: &mut Map<String, Value>,
) -> Result<String, String> {
    let answered =
        |e: &mirror::Error| matches!(e, mirror::Error::Call(c) if !c.kind.worth_retrying());
    let first = match app::activate(bus).await {
        Ok(address) => return Ok(address),
        Err(e) if answered(&e) => e,
        Err(e) => return Err(e.to_string()),
    };
    let mut refused = vec![first.to_string()];
    if let Ok(root) = app::application_of(bus, doc).await
        && root.bus == bus.app_bus
        && root.path != mirror::consts::ROOT_PATH
    {
        info.insert("appRoot".into(), root.path.clone().into());
        match app::activate_at(bus, &root.path).await {
            Ok(address) => return Ok(address),
            Err(e) if answered(&e) => refused.push(e.to_string()),
            Err(e) => return Err(e.to_string()),
        }
    }
    info.insert("overBus".into(), json!(refused));
    Ok(String::new())
}

async fn parent_of(client: &Client, path: &str) -> Option<String> {
    let msg = client
        .call(path, IFACE_PROPERTIES, "Get", &(IFACE_ACCESSIBLE, "Parent"))
        .await
        .ok()?;
    decode::ref_property(&msg).ok().map(|r| r.path)
}

async fn doc_url(client: &Client, doc: &str) -> String {
    let Ok(msg) = client.call(doc, IFACE_DOCUMENT, "GetAttributes", &()).await else {
        return String::new();
    };
    decode::attributes(&msg)
        .unwrap_or_default()
        .into_iter()
        .find(|(k, _)| crate::chromium::is_doc_url(k))
        .map(|(_, v)| v)
        .unwrap_or_default()
}

/// Firefox answers the first question about each kind of data with nothing and only then starts
/// keeping it, for every document for the rest of its life, without saying when it is there. Asking
/// for the object attributes asks for all of them: attributes, actions, text, tables and values.
async fn prime(client: &Client, doc: &str) -> usize {
    for attempt in 0..20 {
        if let Ok(msg) = client
            .call(doc, IFACE_ACCESSIBLE, "GetAttributes", &())
            .await
            && decode::attributes(&msg).is_ok_and(|a| !a.is_empty())
        {
            return attempt + 1;
        }
        if let Ok(s) = time::sleep(Duration::from_millis(50)) {
            s.await;
        }
    }
    20
}

/// Until a link says it has an action, the first DoAction on one is dropped.
async fn prime_actions(client: &Client, link: &str) -> bool {
    for _ in 0..20 {
        if let Ok(msg) = client
            .call(link, IFACE_PROPERTIES, "Get", &(IFACE_ACTION, "NActions"))
            .await
        {
            let body = msg.body();
            if let Ok(v) = body.deserialize::<zbus::Variant<'_>>() {
                if matches!(v, zbus::Variant::I32(n) if n > 0) {
                    return true;
                }
                if let zbus::Variant::Variant(inner) = v
                    && matches!(*inner, zbus::Variant::I32(n) if n > 0)
                {
                    return true;
                }
            }
        }
        if let Ok(s) = time::sleep(Duration::from_millis(50)) {
            s.await;
        }
    }
    false
}

fn apply_marks(objs: &mut HashMap<String, Obj>, marks: Vec<Mark>, everything: bool) {
    if everything {
        for o in objs.values_mut() {
            o.rendered = false;
        }
    }
    for m in marks {
        if let Some(o) = objs.get_mut(&m.path) {
            o.rendered = true;
            o.child_reuse = m.child_reuse;
            o.escalate = m.escalate;
            o.child_ctx = m.child_ctx;
        }
    }
}

fn render_whole(
    objs: &HashMap<String, Obj>,
    root: &str,
    handle: i32,
    prefix_only: bool,
) -> (Vec<u16>, usize, Vec<Mark>) {
    let none = HashSet::new();
    let mut r = Renderer::new(objs, handle, root, prefix_only, &none);
    r.render(root, Default::default());
    (r.out, r.nodes, r.marks)
}

async fn render_task(
    st: Rc<State>,
    bus: String,
    root: String,
    handle: i32,
    flags: u32,
    reply: mpsc::Sender<RenderReply>,
) {
    let t0 = Instant::now();
    let mut info = Map::new();
    let progressive = flags & 1 != 0;
    let app = match st.app(&bus, &root, &mut info).await {
        Ok(a) => a,
        Err(e) => return reply_error(&reply, info, e),
    };
    let d = Rc::new(Doc {
        bus: bus.clone(),
        root: root.clone(),
        handle,
        reuse: flags & 2 != 0,
        frame: RefCell::new(None),
        url: RefCell::new(String::new()),
        objs: Rc::new(RefCell::new(HashMap::new())),
        p: RefCell::new(Pending {
            walking: true,
            ..Pending::default()
        }),
    });
    if let Some(old) = st
        .docs
        .borrow_mut()
        .insert((bus.clone(), root.clone()), d.clone())
    {
        old.p.borrow_mut().unwatched = true;
    }
    let client = app.client.clone();
    if !app.primed.get() {
        let t = Instant::now();
        info.insert("primeCalls".into(), prime(&client, &root).await.into());
        info.insert("primeSeconds".into(), t.elapsed().as_secs_f64().into());
        app.primed.set(true);
    }
    let url = doc_url(&client, &root).await;
    *d.url.borrow_mut() = url.clone();
    info.insert("docURL".into(), url.into());
    *d.frame.borrow_mut() = parent_of(&client, &root).await;

    let calls_before = client.counters().calls;
    let t_walk = Instant::now();
    let mut walk = Walk::new(client.clone(), None, MAX_OBJECTS, MAX_WALK_TIME);
    // LINDA_VBUF_WALK=objects asks every object for every field, for bisecting.
    if std::env::var("LINDA_VBUF_WALK").as_deref() != Ok("objects") {
        walk = walk.by_subtrees();
    }
    walk.start(&root, Vec::new());
    let mut first_sent = false;
    while walk.step().await {
        if d.p.borrow().unwatched {
            // Rendered again or no longer wanted: what was walked so far still answers the request,
            // whose buffer is thrown away, and the application is asked nothing more for it.
            info.insert("cancelled".into(), true.into());
            break;
        }
        if progressive
            && !first_sent
            && (walk.complete >= FIRST_PART_OBJECTS
                || (t_walk.elapsed() >= FIRST_PART_TIME
                    && walk.objs.get(&root).is_some_and(Obj::complete)))
        {
            let (out, nodes, _) = render_whole(&walk.objs, &root, handle, true);
            let mut part = info.clone();
            part.insert("complete".into(), false.into());
            part.insert("busy".into(), busy(&walk.objs, &root).into());
            part.insert("objects".into(), nodes.into());
            part.insert("seconds".into(), t0.elapsed().as_secs_f64().into());
            let _ = reply.send(RenderReply {
                stream: Some(out),
                info: Value::Object(part).to_string(),
            });
            first_sent = true;
        }
    }
    if !d.p.borrow().unwatched
        && !app.actions_primed.get()
        && let Some(link) = walk.first_link.clone()
    {
        app.actions_primed.set(prime_actions(&client, &link).await);
    }
    info.insert("walkSeconds".into(), t_walk.elapsed().as_secs_f64().into());
    info.insert(
        "calls".into(),
        (client.counters().calls - calls_before).into(),
    );
    info.insert("walkErrors".into(), walk.errors.into());
    info.insert("walkTimeouts".into(), walk.timeouts.into());
    info.insert("walkTruncated".into(), walk.truncated.into());
    info.insert(
        "slowestCall".into(),
        json!([
            walk.slowest.0.as_secs_f64(),
            format!("{:?}", walk.slowest.1),
            walk.slowest.2
        ]),
    );
    info.insert("documentObjects".into(), walk.objs.len().into());
    let still_busy = match busy_now(&client, &root).await {
        Some(b) => b,
        None => busy(&walk.objs, &root),
    };
    info.insert("busy".into(), still_busy.into());
    let t_render = Instant::now();
    let (out, nodes, marks) = render_whole(&walk.objs, &root, handle, false);
    info.insert(
        "renderSeconds".into(),
        t_render.elapsed().as_secs_f64().into(),
    );
    info.insert("objects".into(), nodes.into());
    info.insert("complete".into(), true.into());
    info.insert("seconds".into(), t0.elapsed().as_secs_f64().into());
    {
        let mut objs = d.objs.borrow_mut();
        *objs = std::mem::take(&mut walk.objs);
        apply_marks(&mut objs, marks, true);
    }
    if first_sent {
        if st.is_current(&d) {
            let mut stream = vec![b'S' as u16];
            stream.extend(
                format!("{handle},{};", crate::render::id_of(&root).unwrap_or(0)).encode_utf16(),
            );
            stream.extend(out);
            st.emit(&bus, &root, &["complete"], Some(stream), info);
        }
    } else {
        let _ = reply.send(RenderReply {
            stream: Some(out),
            info: Value::Object(info).to_string(),
        });
    }
    let early = {
        let mut p = d.p.borrow_mut();
        p.walking = false;
        std::mem::take(&mut p.early)
    };
    let mut seen = HashSet::new();
    for (path, why) in early {
        if seen.insert(path.clone()) {
            st.invalidate(&bus, &path, &why);
        }
    }
}

fn busy(objs: &HashMap<String, Obj>, root: &str) -> bool {
    objs.get(root)
        .is_some_and(|o| has(o.states, mirror::consts::STATE_BUSY))
}

/// Whether the document is still loading now, which may have changed while it was walked.
async fn busy_now(client: &Client, root: &str) -> Option<bool> {
    let msg = client
        .call(root, IFACE_ACCESSIBLE, "GetState", &())
        .await
        .ok()?;
    decode::states(&msg)
        .ok()
        .map(|s| has(s, mirror::consts::STATE_BUSY))
}

/// The objects that have to be walked again: each changed one whose ancestors have not changed.
/// And every changed object with its ancestors, none of which can be kept as it was.
fn targets(
    objs: &HashMap<String, Obj>,
    invalid: &HashSet<String>,
) -> (Vec<String>, HashSet<String>) {
    let mut dirty = HashSet::new();
    let mut out = Vec::new();
    for t in invalid {
        let Some(o) = objs.get(t) else { continue };
        dirty.insert(t.clone());
        let mut nested = false;
        let mut p = o.parent.clone();
        while let Some(here) = p {
            nested |= invalid.contains(&here);
            p = objs.get(&here).and_then(|o| o.parent.clone());
            dirty.insert(here);
        }
        if !nested {
            out.push(t.clone());
        }
    }
    (out, dirty)
}

/// Swaps each target's subtree for what the walk found, keeping the children it kept.
fn merge(
    objs: &mut HashMap<String, Obj>,
    mut found: HashMap<String, Obj>,
    targets: &[String],
    kept: &HashSet<String>,
) {
    let mut parents = HashMap::new();
    let mut remove = Vec::new();
    for t in targets {
        parents.insert(t.clone(), objs.get(t).and_then(|o| o.parent.clone()));
        let mut stack = vec![t.clone()];
        while let Some(p) = stack.pop() {
            if kept.contains(&p) {
                continue;
            }
            if let Some(o) = objs.get(&p) {
                stack.extend(o.children.iter().cloned());
            }
            remove.push(p);
        }
    }
    for p in remove {
        objs.remove(&p);
    }
    for (path, parent) in parents {
        if let Some(o) = found.get_mut(&path) {
            o.parent = parent;
        }
    }
    objs.extend(found);
}

async fn update_task(st: Rc<State>, d: Rc<Doc>) {
    let (invalid, url_check, why) = {
        let mut p = d.p.borrow_mut();
        p.since = None;
        p.updating = true;
        (
            std::mem::take(&mut p.invalid),
            std::mem::take(&mut p.url_check),
            std::mem::take(&mut p.why),
        )
    };
    let Some(app) = st.apps.borrow().get(&d.bus).cloned() else {
        end_update(&st, &d);
        return;
    };
    let t0 = Instant::now();
    let mut events: Vec<&str> = Vec::new();
    let mut stream = None;
    let mut info = Map::new();
    let (targets, dirty) = targets(&d.objs.borrow(), &invalid);
    if !targets.is_empty() {
        let outer = Outer {
            objs: d.objs.clone(),
            dirty: if d.reuse { dirty } else { HashSet::new() },
            reuse: d.reuse,
        };
        let mut walk = Walk::new(app.client.clone(), Some(outer), MAX_OBJECTS, MAX_WALK_TIME);
        for t in &targets {
            walk.start(t, Vec::new());
        }
        // A document that is unwatched or rendered again meanwhile has no use for this walk: stopping
        // at once leaves the application's main thread to the walk that replaced it.
        while walk.step().await {
            if d.p.borrow().unwatched || !st.is_current(&d) {
                break;
            }
        }
        if d.p.borrow().unwatched || !st.is_current(&d) {
            end_update(&st, &d);
            return;
        }
        let gone: HashSet<String> = walk.gone.iter().cloned().collect();
        let live: Vec<String> = targets
            .iter()
            .filter(|t| !gone.contains(*t))
            .cloned()
            .collect();
        let reused = std::mem::take(&mut walk.reused);
        let found: HashMap<String, Obj> = std::mem::take(&mut walk.objs)
            .into_iter()
            .filter(|(p, _)| !gone.contains(p))
            .collect();
        {
            let mut objs = d.objs.borrow_mut();
            merge(&mut objs, found, &live, &reused);
        }
        let (out, marks) = {
            let objs = d.objs.borrow();
            let mut r = Renderer::new(&objs, d.handle, &d.root, false, &reused);
            for t in &live {
                r.replace(t);
            }
            (r.out, r.marks)
        };
        apply_marks(&mut d.objs.borrow_mut(), marks, false);
        if !out.is_empty() {
            stream = Some(out);
        }
        // A target the application no longer has went with its parent, whose own change says so.
        for g in gone {
            let parent = d.objs.borrow().get(&g).and_then(|o| o.parent.clone());
            if let Some(parent) = parent {
                st.invalidate(&d.bus, &parent, "gone");
            }
        }
        info.insert("objects".into(), live.len().into());
        info.insert(
            "targets".into(),
            json!(live.iter().take(16).collect::<Vec<_>>()),
        );
        info.insert("calls".into(), walk.calls.into());
        info.insert("walkSeconds".into(), walk.seconds().into());
        info.insert(
            "slowestCall".into(),
            json!([
                walk.slowest.0.as_secs_f64(),
                format!("{:?}", walk.slowest.1),
                walk.slowest.2
            ]),
        );
        info.insert("reused".into(), reused.len().into());
    }
    if url_check || stream.is_some() {
        let url = doc_url(&app.client, &d.root).await;
        if d.p.borrow().unwatched {
            end_update(&st, &d);
            return;
        }
        if !url.is_empty() && *d.url.borrow() != url {
            *d.url.borrow_mut() = url;
            events.push("urlChanged");
        }
    }
    info.insert("seconds".into(), t0.elapsed().as_secs_f64().into());
    info.insert("signals".into(), json!(why));
    if (stream.is_some() || !events.is_empty()) && st.is_current(&d) {
        st.emit(&d.bus, &d.root, &events, stream, info);
    }
    end_update(&st, &d);
}

/// Ends an update: the changes that came while it walked are looked at now that its objects are in.
fn end_update(st: &State, d: &Rc<Doc>) {
    let early = {
        let mut p = d.p.borrow_mut();
        p.updating = false;
        std::mem::take(&mut p.early)
    };
    let mut seen = HashSet::new();
    for (path, why) in early {
        if seen.insert(path.clone()) {
            st.invalidate(&d.bus, &path, &why);
        }
    }
}

enum Wake {
    Request(Request),
    Signal(Message),
    Nothing,
}

pub async fn run(inbox: Arc<Inbox>, shared: Arc<Shared>) {
    let st = Rc::new(State {
        shared,
        bus: RefCell::new(None),
        registered: Cell::new(false),
        apps: RefCell::new(HashMap::new()),
        docs: RefCell::new(HashMap::new()),
        new_streams: RefCell::new(Vec::new()),
        spawned: RefCell::new(Vec::new()),
    });
    let mut tasks: FuturesUnordered<LocalBoxFuture<'static, ()>> = FuturesUnordered::new();
    let mut signals: SelectAll<MessageStream> = SelectAll::new();
    let mut timer: Option<(Instant, LocalBoxFuture<'static, ()>)> = None;
    let mut turn = 0usize;
    loop {
        for s in st.new_streams.borrow_mut().drain(..) {
            signals.push(s);
        }
        tasks.extend(st.spawned.borrow_mut().drain(..));
        let now = Instant::now();
        let (ready, next) = st.due(now);
        for d in ready {
            d.p.borrow_mut().updating = true;
            tasks.push(Box::pin(update_task(st.clone(), d)));
        }
        if next != timer.as_ref().map(|t| t.0) {
            timer = next.and_then(|at| {
                let sleep = time::sleep_until(at).ok()?;
                Some((at, sleep.map(|_| ()).boxed_local()))
            });
        }
        let wake = poll_fn(|cx| {
            turn = turn.wrapping_add(1);
            for i in 0..4 {
                match (turn + i) % 4 {
                    0 => {
                        if let Poll::Ready(r) = inbox.poll_recv(cx) {
                            return Poll::Ready(Wake::Request(r));
                        }
                    }
                    1 => {
                        if !signals.is_empty()
                            && let Poll::Ready(Some(m)) = signals.poll_next_unpin(cx)
                        {
                            return Poll::Ready(match m {
                                Ok(m) => Wake::Signal(m),
                                Err(_) => Wake::Nothing,
                            });
                        }
                    }
                    2 => {
                        if !tasks.is_empty()
                            && let Poll::Ready(Some(())) = tasks.poll_next_unpin(cx)
                        {
                            return Poll::Ready(Wake::Nothing);
                        }
                    }
                    _ => {
                        if let Some((_, t)) = timer.as_mut()
                            && t.poll_unpin(cx).is_ready()
                        {
                            timer = None;
                            return Poll::Ready(Wake::Nothing);
                        }
                    }
                }
            }
            Poll::Pending
        })
        .await;
        match wake {
            Wake::Request(Request::Render {
                bus,
                doc,
                handle,
                flags,
                reply,
            }) => tasks.push(Box::pin(render_task(
                st.clone(),
                bus,
                doc,
                handle,
                flags,
                reply,
            ))),
            Wake::Request(Request::Unwatch { bus, doc }) => st.unwatch(&bus, &doc),
            Wake::Request(Request::Shutdown) => break,
            Wake::Signal(m) => st.on_signal(&m),
            Wake::Nothing => {}
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn tree() -> HashMap<String, Obj> {
        let mut objs = HashMap::new();
        for (path, parent, children) in [
            ("/r", None, vec!["/p", "/q"]),
            ("/p", Some("/r"), vec!["/l"]),
            ("/l", Some("/p"), vec![]),
            ("/q", Some("/r"), vec!["/i"]),
            ("/i", Some("/q"), vec![]),
        ] {
            objs.insert(
                path.to_owned(),
                Obj {
                    parent: parent.map(str::to_owned),
                    children: children.into_iter().map(str::to_owned).collect(),
                    rendered: path != "/i",
                    ..Obj::default()
                },
            );
        }
        objs
    }

    #[test]
    fn a_change_inside_a_changed_object_is_walked_with_it() {
        let objs = tree();
        let invalid: HashSet<String> = ["/p", "/l"].iter().map(|s| (*s).to_owned()).collect();
        let (walked, dirty) = targets(&objs, &invalid);
        assert_eq!(walked, vec!["/p".to_owned()]);
        let mut dirty: Vec<_> = dirty.into_iter().collect();
        dirty.sort();
        assert_eq!(dirty, vec!["/l", "/p", "/r"]);
    }

    #[test]
    fn a_change_goes_to_the_nearest_rendered_object_and_past_one_rendered_with_its_parent() {
        let mut objs = tree();
        assert_eq!(target_for(&objs, "/i", "/r").as_deref(), Some("/q"));
        objs.get_mut("/q").unwrap().escalate = true;
        assert_eq!(target_for(&objs, "/i", "/r").as_deref(), Some("/r"));
        assert_eq!(target_for(&objs, "/elsewhere", "/r"), None);
    }

    #[test]
    fn a_merge_swaps_the_subtree_and_keeps_what_was_kept() {
        let mut objs = tree();
        let mut found = HashMap::new();
        found.insert(
            "/q".to_owned(),
            Obj {
                children: vec!["/n".into()],
                ..Obj::default()
            },
        );
        found.insert(
            "/n".to_owned(),
            Obj {
                parent: Some("/q".into()),
                ..Obj::default()
            },
        );
        merge(&mut objs, found, &["/q".to_owned()], &HashSet::new());
        assert!(!objs.contains_key("/i"));
        assert_eq!(objs["/q"].parent.as_deref(), Some("/r"));
        assert_eq!(objs["/q"].children, vec!["/n".to_owned()]);
        let kept: HashSet<String> = ["/l".to_owned()].into();
        let found = HashMap::from([(
            "/p".to_owned(),
            Obj {
                children: vec!["/l".into()],
                ..Obj::default()
            },
        )]);
        merge(&mut objs, found, &["/p".to_owned()], &kept);
        assert!(
            objs["/l"].rendered,
            "a kept child stays as the buffer holds it"
        );
    }
}
