//! Checks real router responses and PostgreSQL rows against the canonical
//! JSON Schemas under `contracts/domain/`.
//!
//! This is a deliberately small draft-07 subset, not a general validator.
//! Every keyword it meets must be in `CHECKED` or `IGNORED`; anything else
//! panics, so a schema can never silently rely on a keyword this test skips.
//! `pattern` is the only constraint left to AJV (`just contracts-domain-check`),
//! because the API crate has no regex dependency.
#![allow(dead_code)]

use serde_json::Value;

const CHECKED: &[&str] = &[
    "$ref",
    "type",
    "enum",
    "const",
    "properties",
    "required",
    "additionalProperties",
    "items",
    "maxItems",
    "minLength",
    "maxLength",
    "minimum",
    "format",
    "allOf",
    "if",
    "then",
    "else",
];
const IGNORED: &[&str] = &[
    "$schema",
    "$id",
    "title",
    "description",
    "definitions",
    "pattern",
];

pub fn load(name: &str) -> Value {
    let path = format!(
        "{}/../../contracts/domain/{name}.schema.json",
        env!("CARGO_MANIFEST_DIR")
    );
    let text = std::fs::read_to_string(&path).unwrap_or_else(|e| panic!("{path}: {e}"));
    serde_json::from_str(&text).unwrap_or_else(|e| panic!("{path}: {e}"))
}

/// `pointer` is "#" for the top-level record or "#/definitions/<name>".
pub fn assert_valid(root: &Value, pointer: &str, instance: &Value, context: &str) {
    let schema = resolve(root, pointer);
    let mut errors = Vec::new();
    validate(root, schema, instance, "$", &mut errors);
    assert!(
        errors.is_empty(),
        "{context}: response violates {}{pointer}:\n  {}\ninstance: {instance}",
        root["$id"].as_str().unwrap_or("schema"),
        errors.join("\n  ")
    );
}

fn resolve<'a>(root: &'a Value, pointer: &str) -> &'a Value {
    let fragment = pointer
        .strip_prefix('#')
        .unwrap_or_else(|| panic!("only local references are supported: {pointer}"));
    root.pointer(fragment)
        .unwrap_or_else(|| panic!("unresolvable schema reference {pointer}"))
}

fn type_matches(name: &str, value: &Value) -> bool {
    match name {
        "object" => value.is_object(),
        "array" => value.is_array(),
        "string" => value.is_string(),
        "boolean" => value.is_boolean(),
        "null" => value.is_null(),
        "integer" => value.is_i64() || value.is_u64(),
        "number" => value.is_number(),
        other => panic!("unsupported JSON Schema type {other}"),
    }
}

fn validate(root: &Value, schema: &Value, value: &Value, at: &str, errors: &mut Vec<String>) {
    let object = match schema {
        Value::Bool(true) => return,
        Value::Bool(false) => {
            errors.push(format!("{at}: no value allowed"));
            return;
        }
        Value::Object(object) => object,
        other => panic!("schema at {at} is not an object: {other}"),
    };
    for keyword in object.keys() {
        assert!(
            CHECKED.contains(&keyword.as_str()) || IGNORED.contains(&keyword.as_str()),
            "schema keyword {keyword:?} at {at} is not supported by this check"
        );
    }

    if let Some(reference) = object.get("$ref").and_then(Value::as_str) {
        validate(root, resolve(root, reference), value, at, errors);
    }
    if let Some(expected) = object.get("type") {
        let ok = match expected {
            Value::String(name) => type_matches(name, value),
            Value::Array(names) => names
                .iter()
                .any(|name| type_matches(name.as_str().expect("type name"), value)),
            other => panic!("invalid type keyword {other}"),
        };
        if !ok {
            errors.push(format!("{at}: expected type {expected}, got {value}"));
            return;
        }
    }
    if let Some(options) = object.get("enum").and_then(Value::as_array) {
        if !options.contains(value) {
            errors.push(format!("{at}: {value} not in enum {options:?}"));
        }
    }
    if let Some(expected) = object.get("const") {
        if expected != value {
            errors.push(format!("{at}: expected const {expected}, got {value}"));
        }
    }
    if let Some(text) = value.as_str() {
        let length = text.chars().count() as u64;
        if let Some(min) = object.get("minLength").and_then(Value::as_u64) {
            if length < min {
                errors.push(format!("{at}: shorter than {min}"));
            }
        }
        if let Some(max) = object.get("maxLength").and_then(Value::as_u64) {
            if length > max {
                errors.push(format!("{at}: longer than {max}"));
            }
        }
        match object.get("format").and_then(Value::as_str) {
            None => {}
            Some("uuid") => {
                if uuid::Uuid::parse_str(text).is_err() {
                    errors.push(format!("{at}: {text:?} is not a uuid"));
                }
            }
            Some("date-time") => {
                if chrono::DateTime::parse_from_rfc3339(text).is_err() {
                    errors.push(format!("{at}: {text:?} is not an RFC 3339 date-time"));
                }
            }
            Some(other) => panic!("unsupported format {other}"),
        }
    }
    if let Some(min) = object.get("minimum").and_then(Value::as_f64) {
        if value.as_f64().is_some_and(|number| number < min) {
            errors.push(format!("{at}: below minimum {min}"));
        }
    }
    if let Some(items) = value.as_array() {
        if let Some(max) = object.get("maxItems").and_then(Value::as_u64) {
            if items.len() as u64 > max {
                errors.push(format!("{at}: {} items exceed maxItems {max}", items.len()));
            }
        }
        if let Some(item_schema) = object.get("items") {
            for (index, item) in items.iter().enumerate() {
                validate(root, item_schema, item, &format!("{at}[{index}]"), errors);
            }
        }
    }
    if let Some(fields) = value.as_object() {
        let properties = object.get("properties").and_then(Value::as_object);
        if let Some(required) = object.get("required").and_then(Value::as_array) {
            for name in required {
                let name = name.as_str().expect("required entries are strings");
                if !fields.contains_key(name) {
                    errors.push(format!("{at}: missing required {name}"));
                }
            }
        }
        if let Some(properties) = properties {
            for (name, property_schema) in properties {
                if let Some(field) = fields.get(name) {
                    validate(
                        root,
                        property_schema,
                        field,
                        &format!("{at}.{name}"),
                        errors,
                    );
                }
            }
        }
        match object.get("additionalProperties") {
            None | Some(Value::Bool(true)) => {}
            Some(Value::Bool(false)) => {
                for name in fields.keys() {
                    if !properties.is_some_and(|declared| declared.contains_key(name)) {
                        errors.push(format!("{at}: undeclared property {name}"));
                    }
                }
            }
            Some(other) => panic!("unsupported additionalProperties {other}"),
        }
    }
    if let Some(all) = object.get("allOf").and_then(Value::as_array) {
        for sub in all {
            validate(root, sub, value, at, errors);
        }
    }
    if let Some(condition) = object.get("if") {
        let mut probe = Vec::new();
        validate(root, condition, value, at, &mut probe);
        let branch = if probe.is_empty() {
            object.get("then")
        } else {
            object.get("else")
        };
        if let Some(branch) = branch {
            validate(root, branch, value, at, errors);
        }
    }
}
