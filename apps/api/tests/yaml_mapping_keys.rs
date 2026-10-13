//! Parser-compatibility regressions: inspect actual YAML key types before decoding.
use weltgewebe_api::config::AppConfig;

#[test]
fn implicit_null_mapping_keys_must_not_be_silently_ignored() {
    let invalid = [
        ("root block", ": ignored\nmax_guest_owned_nodes: 21\n"),
        ("explicit bare root key", "max_guest_owned_nodes: 21\n?\n"),
        (
            "explicit bare nested key",
            "unknown:\n  ?\nmax_guest_owned_nodes: 21\n",
        ),
        ("explicit bare flow key", "{?, max_guest_owned_nodes: 21}\n"),
        (
            "explicit bare flow tail",
            "{max_guest_owned_nodes: 21, ?}\n",
        ),
        ("nested block", "unknown:\n  : ignored\n"),
        (
            "rate limits block",
            "node_mutation_rate_limits:\n  : ignored\n",
        ),
        ("root flow", "{: ignored, max_guest_owned_nodes: 21}\n"),
        ("deep nested block", "unknown:\n  nested:\n    : ignored\n"),
        ("sequence nested flow", "unknown: [{: ignored}]\n"),
        (
            "anchor nested block",
            "unknown: &mapping\n  : ignored\nother: *mapping\n",
        ),
        ("tagged empty root key", "! : ignored\n"),
        (
            "rate limits flow",
            "node_mutation_rate_limits: {: ignored}\n",
        ),
    ];
    for (case, source) in invalid {
        assert!(
            AppConfig::load_from_str(source).is_err(),
            "implicitly null YAML key was silently accepted in {case}: {source:?}"
        );
    }
}

#[test]
fn nonspecific_bare_tag_on_string_key_preserves_compatibility() {
    let config = AppConfig::load_from_str("! max_guest_owned_nodes: 21\n")
        .expect("the YAML non-specific ! tag on a scalar key denotes a string");
    assert_eq!(config.max_guest_owned_nodes, 21);
}

#[test]
fn tagged_mutation_rate_limits_cannot_be_dropped_to_defaults() {
    for source in [
        "node_mutation_rate_limits: !custom {replace_per_minute: 1.5}\n",
        "node_mutation_rate_limits: ! {replace_per_minute: 1.5}\n",
        "node_mutation_rate_limits: !!map {replace_per_minute: 1.5}\n",
    ] {
        assert!(
            AppConfig::load_from_str(source).is_err(),
            "tagged fractional rate limit was silently dropped: {source:?}"
        );
    }
    let valid = AppConfig::load_from_str(
        "node_mutation_rate_limits: {replace_per_minute: 11, delete_per_hour: 42}\n",
    )
    .expect("valid integer limits must decode to configured values");
    assert_eq!(valid.node_mutation_rate_limits.replace_per_minute, 11);
    assert_eq!(valid.node_mutation_rate_limits.delete_per_hour, 42);
}

#[test]
fn block_scalar_mapping_keys_are_valid_strings() {
    for (case, source) in [
        ("literal", "? |-\n  max_guest_owned_nodes\n: 21\n"),
        ("folded", "? >-\n  max_guest_owned_nodes\n: 21\n"),
    ] {
        let parsed = AppConfig::load_from_str(source)
            .unwrap_or_else(|error| panic!("{case} YAML block string key rejected: {error:#}"));
        assert_eq!(parsed.max_guest_owned_nodes, 21, "{case}");
    }
}
#[test]
fn anchored_string_mapping_key_aliases_keep_legacy_compatibility() {
    for (case, source) in [
        ("plain", "field: &key max_guest_owned_nodes\n? *key\n: 21\n"),
        (
            "quoted",
            "field: &key 'max_guest_owned_nodes'\n? *key\n: 21\n",
        ),
        (
            "comment-separated",
            "field: &key # comment\n  max_guest_owned_nodes\n? *key\n: 21\n",
        ),
    ] {
        let parsed = AppConfig::load_from_str(source)
            .unwrap_or_else(|error| panic!("{case} anchored scalar key alias rejected: {error:#}"));
        assert_eq!(parsed.max_guest_owned_nodes, 21, "{case}");
    }
}

#[test]
fn non_string_key_aliases_must_not_be_silently_ignored() {
    for source in [
        "field: &key 1\n? *key\n: ignored\n",
        "field: &key true\n? *key\n: ignored\n",
        "field: &key null\n? *key\n: ignored\n",
        "field: &key {nested: value}\n? *key\n: ignored\n",
    ] {
        assert!(
            AppConfig::load_from_str(source).is_err(),
            "non-string key alias accepted: {source:?}"
        );
    }
}
