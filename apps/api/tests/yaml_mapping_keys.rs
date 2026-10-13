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
        "node_mutation_rate_limits: !!map {delete_per_hour: 18446744073709551616.0}\n",
        "node_mutation_rate_limits: !!map {delete_per_hour: !!float 18446744073709551616.0}\n",
        "node_mutation_rate_limits: {delete_per_hour: !!float 10.0}\n",
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

#[test]
fn tagged_root_keeps_configured_values_in_typed_decoder() {
    let parsed = AppConfig::load_from_str(
        "!custom {max_guest_owned_nodes: 21, auth_cookie_secure: false}\n",
    )
    .expect("tagged YAML root must preserve the validated configuration mapping");
    assert_eq!(parsed.max_guest_owned_nodes, 21);
    assert!(!parsed.auth_cookie_secure);
}

#[test]
fn declared_yaml_string_tag_handles_are_compatible() {
    for (case, source) in [
        (
            "block",
            "%TAG !core! tag:yaml.org,2002:\n---\n!core!str max_guest_owned_nodes: 21\n",
        ),
        (
            "flow",
            "%TAG !core! tag:yaml.org,2002:\n---\n{!core!str max_guest_owned_nodes: 21}\n",
        ),
    ] {
        let parsed = AppConfig::load_from_str(source).unwrap_or_else(|error| {
            panic!("{case} declared YAML string handle rejected: {error:#}")
        });
        assert_eq!(parsed.max_guest_owned_nodes, 21, "{case}");
    }
}

#[test]
fn declared_non_string_tag_handles_stay_rejected() {
    for source in [
        "%TAG !core! tag:yaml.org,2002:\n---\n!core!int '1': ignored\n",
        "%TAG !core! tag:yaml.org,2002:\n---\n{!core!bool 'true': ignored}\n",
    ] {
        assert!(
            AppConfig::load_from_str(source).is_err(),
            "declared non-string tag handle escaped key type guard: {source:?}"
        );
    }
}

#[test]
fn tagged_root_block_and_standard_map_preserve_all_declared_values() {
    for (case, source) in [
        (
            "tagged-block",
            "!custom\nmax_guest_owned_nodes: 21\nauth_cookie_secure: false\n",
        ),
        (
            "standard-map",
            "!!map {max_guest_owned_nodes: 21, auth_cookie_secure: false}\n",
        ),
    ] {
        let parsed = AppConfig::load_from_str(source)
            .unwrap_or_else(|error| panic!("{case} tagged root rejected: {error:#}"));
        assert_eq!(parsed.max_guest_owned_nodes, 21, "{case}");
        assert!(!parsed.auth_cookie_secure, "{case}");
    }
}

#[test]
fn declared_tag_handle_cannot_spoof_builtin_yaml_string_tag() {
    for source in [
        "%TAG !! tag:example.invalid,2026:\n---\n!!str max_guest_owned_nodes: 21\n",
        "%TAG !core! tag:yaml.org,2002:\n---\n!core!int '1': ignored\n",
    ] {
        assert!(
            AppConfig::load_from_str(source).is_err(),
            "overridden or non-string YAML tag handle escaped check: {source:?}"
        );
    }
}

#[test]
fn declared_plain_handle_prefix_is_compatible() {
    let parsed = AppConfig::load_from_str(
        "%TAG ! tag:yaml.org,2002:\n---\n!str max_guest_owned_nodes: 21\n",
    )
    .expect("YAML primary handle may resolve to a declared standard string tag");
    assert_eq!(parsed.max_guest_owned_nodes, 21);
}

#[test]
fn merge_key_cannot_inject_valid_security_config_options() {
    for source in [
        "<<: {auth_cookie_secure: false}\n",
        "<<: {max_guest_owned_nodes: 1}\n",
        "base: &settings {auth_cookie_secure: false}\n<<: *settings\n",
    ] {
        assert!(
            AppConfig::load_from_str(source).is_err(),
            "YAML merge key silently modified security config: {source:?}"
        );
    }
}
#[test]
fn directive_trailing_comments_compatible_with_yaml_string_handles() {
    let parsed = AppConfig::load_from_str(
        "%TAG !core! tag:yaml.org,2002: # core types\n---\n!core!str max_guest_owned_nodes: 21\n",
    )
    .expect("trailing comments on YAML tag directives must preserve core string handles");
    assert_eq!(parsed.max_guest_owned_nodes, 21);
}

#[test]
fn explicitly_tagged_empty_string_keys_remain_valid() {
    let parsed = AppConfig::load_from_str("!!str : ignored\nmax_guest_owned_nodes: 21\n")
        .expect("a verified !!str empty key is a string, not an implicit null key");
    assert_eq!(parsed.max_guest_owned_nodes, 21);
    assert!(AppConfig::load_from_str(": ignored\nmax_guest_owned_nodes: 21\n").is_err());
}
