//! Decoders from AT-SPI reply and signal bodies to plain owned values.
//!
//! Each borrows from the message while it decodes, so a reply's strings are
//! copied once, into the value returned.

use std::collections::HashMap;

use zbus::{Message, ObjectPath, Variant};

use crate::consts::{EVENT_PREFIX, NULL_PATH, interface_bits};

type RawRef<'a> = (&'a str, ObjectPath<'a>);

/// An object reference `(so)`: the unique bus name of the owning application
/// and an object path. A path alone is not a key: the desktop and every
/// application root share the path `/org/a11y/atspi/accessible/root`.
#[derive(Debug, Clone, PartialEq, Eq, Hash)]
pub struct ObjRef {
    pub bus: String,
    pub path: String,
}

impl ObjRef {
    fn from_raw(r: &RawRef<'_>) -> Self {
        Self {
            bus: r.0.to_owned(),
            path: r.1.as_str().to_owned(),
        }
    }

    pub fn is_null(&self) -> bool {
        self.path == NULL_PATH
    }
}

/// One item of `Cache.GetItems` or `Cache.AddAccessible`, signature
/// `((so)(so)(so)iiassusau)`.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct CacheItem {
    pub object: ObjRef,
    pub app: ObjRef,
    pub parent: ObjRef,
    /// -1 for transient objects and GTK menu items.
    pub index_in_parent: i32,
    /// -1 for objects that manage their descendants, defunct ones and GTK
    /// menus; the bridge did not list those children.
    pub child_count: i32,
    pub interfaces: u32,
    pub name: String,
    pub role: u32,
    pub description: String,
    pub states: [u32; 2],
}

type RawItem<'a> = (
    RawRef<'a>,
    RawRef<'a>,
    RawRef<'a>,
    i32,
    i32,
    Vec<&'a str>,
    &'a str,
    u32,
    &'a str,
    Vec<u32>,
);

fn item_from_raw(r: &RawItem<'_>) -> CacheItem {
    CacheItem {
        object: ObjRef::from_raw(&r.0),
        app: ObjRef::from_raw(&r.1),
        parent: ObjRef::from_raw(&r.2),
        index_in_parent: r.3,
        child_count: r.4,
        interfaces: interface_bits(r.5.iter().copied()),
        name: r.6.to_owned(),
        role: r.7,
        description: r.8.to_owned(),
        states: states_from_words(&r.9),
    }
}

fn states_from_words(w: &[u32]) -> [u32; 2] {
    [
        w.first().copied().unwrap_or(0),
        w.get(1).copied().unwrap_or(0),
    ]
}

pub fn items(msg: &Message) -> zbus::Result<Vec<CacheItem>> {
    let body = msg.body();
    let raw: Vec<RawItem<'_>> = body.deserialize()?;
    Ok(raw.iter().map(item_from_raw).collect())
}

pub fn add_accessible(msg: &Message) -> zbus::Result<CacheItem> {
    let body = msg.body();
    let raw: RawItem<'_> = body.deserialize()?;
    Ok(item_from_raw(&raw))
}

pub fn remove_accessible(msg: &Message) -> zbus::Result<ObjRef> {
    let body = msg.body();
    let raw: RawRef<'_> = body.deserialize()?;
    Ok(ObjRef::from_raw(&raw))
}

pub fn children(msg: &Message) -> zbus::Result<Vec<ObjRef>> {
    let body = msg.body();
    let raw: Vec<RawRef<'_>> = body.deserialize()?;
    Ok(raw.iter().map(ObjRef::from_raw).collect())
}

pub fn role(msg: &Message) -> zbus::Result<u32> {
    msg.body().deserialize()
}

pub fn states(msg: &Message) -> zbus::Result<[u32; 2]> {
    let w: Vec<u32> = msg.body().deserialize()?;
    Ok(states_from_words(&w))
}

pub fn interfaces(msg: &Message) -> zbus::Result<u32> {
    let body = msg.body();
    let names: Vec<&str> = body.deserialize()?;
    Ok(interface_bits(names))
}

/// `a{ss}`, sorted by key so that two fetches compare equal.
pub fn attributes(msg: &Message) -> zbus::Result<Vec<(String, String)>> {
    let body = msg.body();
    let map: HashMap<&str, &str> = body.deserialize()?;
    Ok(sorted_pairs(map))
}

fn sorted_pairs(map: HashMap<&str, &str>) -> Vec<(String, String)> {
    let mut v: Vec<(String, String)> = map
        .into_iter()
        .map(|(k, v)| (k.to_owned(), v.to_owned()))
        .collect();
    v.sort();
    v
}

pub fn string(msg: &Message) -> zbus::Result<String> {
    let body = msg.body();
    let s: &str = body.deserialize()?;
    Ok(s.to_owned())
}

/// A reply that is one object reference, `(so)`, as `GetApplication`'s.
pub fn obj_ref(msg: &Message) -> zbus::Result<ObjRef> {
    let body = msg.body();
    let raw: RawRef<'_> = body.deserialize()?;
    Ok(ObjRef::from_raw(&raw))
}

/// The reply of `Properties.Get` for an object property such as `Parent`,
/// `(v)` holding `(so)`.
pub fn ref_property(msg: &Message) -> zbus::Result<ObjRef> {
    let body = msg.body();
    let v: Variant<'_> = body.deserialize()?;
    variant_ref(&v).ok_or(zbus::Error::IncorrectType)
}

/// The reply of `Properties.Get` for a string property, `(v)` holding `s`.
pub fn string_property(msg: &Message) -> zbus::Result<String> {
    let body = msg.body();
    let v: Variant<'_> = body.deserialize()?;
    variant_str(&v)
        .map(str::to_owned)
        .ok_or(zbus::Error::IncorrectType)
}

/// The name and description from the reply of `Properties.GetAll` on the
/// Accessible interface, `(a{sv})`, which gives both in one round trip where
/// two `Properties.Get` would take two. A property the reply lacks is empty,
/// as AT-SPI gives an object with none; a reply that holds neither is an
/// error, as from a bridge that answers GetAll with nothing.
pub fn name_and_description(msg: &Message) -> zbus::Result<(String, String)> {
    let body = msg.body();
    let props: HashMap<&str, Variant<'_>> = body.deserialize()?;
    let name = props.get("Name").and_then(variant_str);
    let description = props.get("Description").and_then(variant_str);
    if name.is_none() && description.is_none() {
        return Err(zbus::Error::IncorrectType);
    }
    Ok((
        name.unwrap_or_default().to_owned(),
        description.unwrap_or_default().to_owned(),
    ))
}

fn variant_str<'a>(v: &'a Variant<'_>) -> Option<&'a str> {
    match v {
        Variant::Str(s) => Some(s.as_str()),
        Variant::Variant(inner) => variant_str(inner),
        _ => None,
    }
}

fn variant_ref(v: &Variant<'_>) -> Option<ObjRef> {
    match v {
        Variant::Structure(s) => match s.fields() {
            [Variant::Str(bus), Variant::ObjectPath(p)] => Some(ObjRef {
                bus: bus.as_str().to_owned(),
                path: p.as_str().to_owned(),
            }),
            _ => None,
        },
        Variant::Variant(inner) => variant_ref(inner),
        _ => None,
    }
}

fn variant_int(v: &Variant<'_>) -> Option<i64> {
    match *v {
        Variant::I32(i) => Some(i.into()),
        Variant::U32(u) => Some(u.into()),
        Variant::I16(i) => Some(i.into()),
        Variant::U16(u) => Some(u.into()),
        Variant::I64(i) => Some(i),
        Variant::U8(u) => Some(u.into()),
        Variant::Variant(ref inner) => variant_int(inner),
        _ => None,
    }
}

/// One entry of the bridge's undocumented `Collection.GetTree`, whose reply is
/// `a((so)a{sv})`. It carries no role and no states.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct TreeEntry {
    pub object: ObjRef,
    pub parent: Option<ObjRef>,
    pub name: Option<String>,
    pub description: Option<String>,
    pub child_count: Option<i32>,
    pub attributes: Option<Vec<(String, String)>>,
}

pub fn tree(msg: &Message) -> zbus::Result<Vec<TreeEntry>> {
    let body = msg.body();
    let raw: Vec<(RawRef<'_>, HashMap<&str, Variant<'_>>)> = body.deserialize()?;
    Ok(raw
        .iter()
        .map(|(r, props)| TreeEntry {
            object: ObjRef::from_raw(r),
            parent: props.get("Parent").and_then(variant_ref),
            name: props.get("Name").and_then(variant_str).map(str::to_owned),
            description: props
                .get("Description")
                .and_then(variant_str)
                .map(str::to_owned),
            child_count: props
                .get("ChildCount")
                .and_then(variant_int)
                .and_then(|i| i32::try_from(i).ok()),
            attributes: props.get("Attributes").and_then(variant_attributes),
        })
        .collect())
}

fn variant_attributes(v: &Variant<'_>) -> Option<Vec<(String, String)>> {
    match v {
        Variant::Dict(d) => {
            let mut v = Vec::new();
            for (k, val) in d.iter() {
                v.push((variant_str(k)?.to_owned(), variant_str(val)?.to_owned()));
            }
            v.sort();
            Some(v)
        }
        Variant::Variant(inner) => variant_attributes(inner),
        _ => None,
    }
}

/// What an event's `any_data` held, reduced to what the mirror uses.
#[derive(Debug, Clone, PartialEq, Eq)]
pub enum AnyData {
    Ref(ObjRef),
    Str(String),
    Int(i64),
    Other,
}

/// An AT-SPI event signal, `org.a11y.atspi.Event.<Class>.<Member>` with body
/// `(siiva{sv})`.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct Event {
    pub sender: String,
    pub path: String,
    pub class: String,
    pub member: String,
    /// With the bridge's first `/` turned back into `:`, as libatspi does,
    /// so `add/system` reads `add:system`.
    pub minor: String,
    pub detail1: i32,
    pub detail2: i32,
    pub any: AnyData,
}

impl Event {
    /// The minor part up to its first `:`, such as `add` for `add:system`.
    pub fn minor_kind(&self) -> &str {
        self.minor.split(':').next().unwrap_or("")
    }
}

/// None for a signal that is not an AT-SPI event; an error for one whose body
/// does not decode.
pub fn event(msg: &Message) -> zbus::Result<Option<Event>> {
    let header = msg.header();
    let Some(class) = header
        .interface()
        .and_then(|i| i.as_str().strip_prefix(EVENT_PREFIX))
    else {
        return Ok(None);
    };
    let body = msg.body();
    let (minor, detail1, detail2, any, _props): (
        &str,
        i32,
        i32,
        Variant<'_>,
        HashMap<&str, Variant<'_>>,
    ) = body.deserialize()?;
    let any = if let Some(r) = variant_ref(&any) {
        AnyData::Ref(r)
    } else if let Some(s) = variant_str(&any) {
        AnyData::Str(s.to_owned())
    } else if let Some(i) = variant_int(&any) {
        AnyData::Int(i)
    } else {
        AnyData::Other
    };
    Ok(Some(Event {
        sender: header
            .sender()
            .map(|s| s.as_str().to_owned())
            .unwrap_or_default(),
        path: header
            .path()
            .map(|p| p.as_str().to_owned())
            .unwrap_or_default(),
        class: class.to_owned(),
        member: header
            .member()
            .map(|m| m.as_str().to_owned())
            .unwrap_or_default(),
        minor: minor.replacen('/', ":", 1),
        detail1,
        detail2,
        any,
    }))
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::consts::INTERFACE_TEXT_BIT;

    fn path(p: &str) -> ObjectPath<'_> {
        ObjectPath::try_from(p).unwrap()
    }

    fn body_message<B>(body: &B) -> Message
    where
        B: serde::Serialize + zbus::DynamicType,
    {
        Message::method_call("/test", "Reply").build(body).unwrap()
    }

    const APP: &str = ":1.7";

    fn raw_item<'a>(
        obj: &'a str,
        parent_bus: &'a str,
        parent: &'a str,
        index: i32,
        count: i32,
        name: &'a str,
    ) -> RawItem<'a> {
        (
            (APP, path(obj)),
            (APP, path("/org/a11y/atspi/accessible/root")),
            (parent_bus, path(parent)),
            index,
            count,
            vec!["org.a11y.atspi.Accessible", "org.a11y.atspi.Text"],
            name,
            88,
            "described",
            vec![1 << 12, 1 << 11],
        )
    }

    #[test]
    fn decodes_get_items() {
        let body = vec![
            raw_item(
                "/org/a11y/atspi/accessible/root",
                ":1.1",
                "/org/a11y/atspi/accessible/root",
                -1,
                1,
                "Firefox",
            ),
            raw_item(
                "/org/a11y/atspi/accessible/5",
                APP,
                "/org/a11y/atspi/accessible/root",
                0,
                0,
                "link 3",
            ),
        ];
        let msg = body_message(&body);
        assert_eq!(
            msg.body().signature().to_string(),
            "a((so)(so)(so)iiassusau)"
        );
        let items = items(&msg).unwrap();
        assert_eq!(items.len(), 2);
        let link = &items[1];
        assert_eq!(link.object.bus, APP);
        assert_eq!(link.object.path, "/org/a11y/atspi/accessible/5");
        assert_eq!(link.parent.path, "/org/a11y/atspi/accessible/root");
        assert_eq!(link.index_in_parent, 0);
        assert_eq!(link.child_count, 0);
        assert_eq!(link.name, "link 3");
        assert_eq!(link.description, "described");
        assert_eq!(link.role, 88);
        assert_eq!(link.states, [1 << 12, 1 << 11]);
        assert_ne!(link.interfaces & INTERFACE_TEXT_BIT, 0);
        // The application's parent is the desktop: same path, other bus.
        assert_eq!(items[0].parent.bus, ":1.1");
    }

    #[test]
    fn decodes_add_and_remove_accessible() {
        let item = raw_item("/org/a11y/atspi/accessible/9", APP, "/p", 2, 3, "new");
        let msg = Message::signal(
            "/org/a11y/atspi/cache",
            "org.a11y.atspi.Cache",
            "AddAccessible",
        )
        .build(&item)
        .unwrap();
        let it = add_accessible(&msg).unwrap();
        assert_eq!(it.object.path, "/org/a11y/atspi/accessible/9");
        assert_eq!((it.index_in_parent, it.child_count), (2, 3));

        let msg = Message::signal(
            "/org/a11y/atspi/cache",
            "org.a11y.atspi.Cache",
            "RemoveAccessible",
        )
        .build(&(APP, path("/org/a11y/atspi/accessible/9")))
        .unwrap();
        assert_eq!(
            remove_accessible(&msg).unwrap(),
            ObjRef {
                bus: APP.into(),
                path: "/org/a11y/atspi/accessible/9".into()
            }
        );
    }

    #[test]
    fn decodes_per_object_replies() {
        let msg = body_message(&vec![(APP, path("/a/1")), (APP, path(NULL_PATH))]);
        let kids = children(&msg).unwrap();
        assert_eq!(kids.len(), 2);
        assert!(kids[1].is_null());

        assert_eq!(role(&body_message(&95u32)).unwrap(), 95);
        assert_eq!(states(&body_message(&vec![8u32])).unwrap(), [8, 0]);
        assert_eq!(
            interfaces(&body_message(&vec!["org.a11y.atspi.Text"])).unwrap(),
            INTERFACE_TEXT_BIT
        );
        let mut attrs = HashMap::new();
        attrs.insert("tag", "h2");
        attrs.insert("level", "2");
        assert_eq!(
            attributes(&body_message(&attrs)).unwrap(),
            vec![
                ("level".to_owned(), "2".to_owned()),
                ("tag".to_owned(), "h2".to_owned())
            ]
        );
        assert_eq!(
            string(&body_message(&"text \u{fffc}")).unwrap(),
            "text \u{fffc}"
        );
        assert_eq!(
            string_property(&body_message(&Variant::new("Heading 1"))).unwrap(),
            "Heading 1"
        );
        assert!(string_property(&body_message(&Variant::new(3i32))).is_err());
    }

    #[test]
    fn decodes_name_and_description_from_get_all() {
        let mut props: HashMap<&str, Variant<'_>> = HashMap::new();
        props.insert("Name", Variant::new("Search"));
        props.insert("Description", Variant::new("Find on the site"));
        props.insert("ChildCount", Variant::new(2i32));
        assert_eq!(
            name_and_description(&body_message(&props)).unwrap(),
            ("Search".to_owned(), "Find on the site".to_owned())
        );
        let mut only_name: HashMap<&str, Variant<'_>> = HashMap::new();
        only_name.insert("Name", Variant::new("Search"));
        assert_eq!(
            name_and_description(&body_message(&only_name)).unwrap(),
            ("Search".to_owned(), String::new())
        );
        let empty: HashMap<&str, Variant<'_>> = HashMap::new();
        assert!(name_and_description(&body_message(&empty)).is_err());
    }

    #[test]
    fn decodes_get_tree() {
        let mut props: HashMap<&str, Variant<'_>> = HashMap::new();
        props.insert("Name", Variant::new("Para"));
        props.insert("ChildCount", Variant::new(2i32));
        props.insert(
            "Parent",
            Variant::from((APP, path("/org/a11y/atspi/accessible/root"))),
        );
        let mut attrs = HashMap::new();
        attrs.insert("tag", "p");
        props.insert("Attributes", Variant::from(attrs));
        let body = vec![((APP, path("/org/a11y/atspi/accessible/4")), props)];
        let msg = body_message(&body);
        assert_eq!(msg.body().signature().to_string(), "a((so)a{sv})");
        let t = tree(&msg).unwrap();
        assert_eq!(t.len(), 1);
        assert_eq!(t[0].name.as_deref(), Some("Para"));
        assert_eq!(t[0].child_count, Some(2));
        assert_eq!(
            t[0].parent.as_ref().map(|p| p.path.as_str()),
            Some("/org/a11y/atspi/accessible/root")
        );
        assert_eq!(
            t[0].attributes,
            Some(vec![("tag".to_owned(), "p".to_owned())])
        );
        assert_eq!(t[0].description, None);
    }

    fn event_message(member: &str, minor: &str, d1: i32, d2: i32, any: Variant<'_>) -> Message {
        let props: HashMap<&str, Variant<'_>> = HashMap::new();
        Message::signal(
            "/org/a11y/atspi/accessible/12",
            "org.a11y.atspi.Event.Object",
            member,
        )
        .sender(APP)
        .build(&(minor, d1, d2, any, props))
        .unwrap()
    }

    #[test]
    fn decodes_events() {
        let msg = event_message(
            "ChildrenChanged",
            "add/system",
            4,
            0,
            Variant::from((APP, path("/org/a11y/atspi/accessible/40"))),
        );
        assert_eq!(msg.body().signature().to_string(), "(siiva{sv})");
        let e = event(&msg).unwrap().unwrap();
        assert_eq!(e.class, "Object");
        assert_eq!(e.member, "ChildrenChanged");
        assert_eq!(e.minor, "add:system");
        assert_eq!(e.minor_kind(), "add");
        assert_eq!(e.sender, APP);
        assert_eq!(e.path, "/org/a11y/atspi/accessible/12");
        assert_eq!(e.detail1, 4);
        assert_eq!(
            e.any,
            AnyData::Ref(ObjRef {
                bus: APP.into(),
                path: "/org/a11y/atspi/accessible/40".into()
            })
        );

        let e = event(&event_message(
            "TextChanged",
            "insert",
            0,
            2,
            Variant::new("hi"),
        ))
        .unwrap()
        .unwrap();
        assert_eq!(e.any, AnyData::Str("hi".into()));
        let e = event(&event_message(
            "StateChanged",
            "focused",
            1,
            0,
            Variant::new(0i32),
        ))
        .unwrap()
        .unwrap();
        assert_eq!(e.any, AnyData::Int(0));

        let other = Message::signal("/x", "org.example.Iface", "Thing")
            .build(&())
            .unwrap();
        assert_eq!(event(&other).unwrap(), None);
    }

    #[test]
    fn an_unsigned_detail_is_a_number() {
        let e = event(&event_message(
            "PropertyChange",
            "accessible-role",
            0,
            0,
            Variant::new(88u32),
        ))
        .unwrap()
        .unwrap();
        assert_eq!(e.any, AnyData::Int(88));
    }

    #[test]
    fn a_string_inside_a_variant_inside_a_variant_is_read() {
        let inner = Variant::new("Heading 1");
        let msg = body_message(&Variant::Variant(Box::new(inner)));
        assert_eq!(msg.body().signature().to_string(), "v");
        assert_eq!(string_property(&msg).unwrap(), "Heading 1");
    }

    fn tree_entry(props: HashMap<&str, Variant<'_>>) -> TreeEntry {
        let body = vec![((APP, path("/org/a11y/atspi/accessible/4")), props)];
        tree(&body_message(&body)).unwrap().remove(0)
    }

    #[test]
    fn a_child_count_too_big_for_the_field_is_unknown() {
        let mut props: HashMap<&str, Variant<'_>> = HashMap::new();
        props.insert("ChildCount", Variant::new(1i64 << 40));
        assert_eq!(tree_entry(props).child_count, None);
        let mut props: HashMap<&str, Variant<'_>> = HashMap::new();
        props.insert("ChildCount", Variant::new(7u32));
        assert_eq!(tree_entry(props).child_count, Some(7));
    }

    #[test]
    fn attributes_with_a_value_that_is_not_a_string_are_unknown() {
        let mut attrs: HashMap<&str, Variant<'_>> = HashMap::new();
        attrs.insert("tag", Variant::new("p"));
        attrs.insert("level", Variant::new(2i32));
        let mut props: HashMap<&str, Variant<'_>> = HashMap::new();
        props.insert("Attributes", Variant::from(attrs));
        assert_eq!(tree_entry(props).attributes, None);
    }

    #[test]
    fn tree_attributes_come_sorted_by_name() {
        let mut attrs = HashMap::new();
        for (k, v) in [
            ("tag", "p"),
            ("display", "block"),
            ("level", "2"),
            ("a", "1"),
        ] {
            attrs.insert(k, v);
        }
        let mut props: HashMap<&str, Variant<'_>> = HashMap::new();
        props.insert("Attributes", Variant::from(attrs));
        let names: Vec<String> = tree_entry(props)
            .attributes
            .unwrap()
            .into_iter()
            .map(|(k, _)| k)
            .collect();
        assert_eq!(names, vec!["a", "display", "level", "tag"]);
    }
}
