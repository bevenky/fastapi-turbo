"""Behavioral parity tests: patterns 1-100.

Tests run the same app on both FastAPI (uvicorn) and fastapi-turbo, comparing
status codes and response bodies. Uses session-scoped servers (one startup).

Run:
    pytest tests/parity/test_parity.py -v
    pytest tests/parity/test_parity.py -k P001 -v
"""
import base64

import pytest


# ── Helpers ──────────────────────────────────────────────────────

def hit(client, dual_servers, method, path, follow_redirects=True, **kwargs):
    """Hit both servers with the same request, return (fa_response, rs_response)."""
    fa_url = f"http://127.0.0.1:{dual_servers.fa_port}{path}"
    rs_url = f"http://127.0.0.1:{dual_servers.rs_port}{path}"
    fa_r = getattr(client, method)(fa_url, follow_redirects=follow_redirects, **kwargs)
    rs_r = getattr(client, method)(rs_url, follow_redirects=follow_redirects, **kwargs)
    return fa_r, rs_r


def assert_json_match(fa_r, rs_r):
    assert fa_r.status_code == rs_r.status_code, (
        f"status mismatch: FA={fa_r.status_code} RS={rs_r.status_code}"
    )
    assert fa_r.json() == rs_r.json(), (
        f"body mismatch:\n  FA={fa_r.json()}\n  RS={rs_r.json()}"
    )


def assert_status_match(fa_r, rs_r):
    assert fa_r.status_code == rs_r.status_code, (
        f"status mismatch: FA={fa_r.status_code} RS={rs_r.status_code}"
    )


def assert_text_match(fa_r, rs_r):
    assert fa_r.status_code == rs_r.status_code, (
        f"status mismatch: FA={fa_r.status_code} RS={rs_r.status_code}"
    )
    assert fa_r.text == rs_r.text, (
        f"text mismatch:\n  FA={fa_r.text[:200]}\n  RS={rs_r.text[:200]}"
    )


# ══════════════════════════════════════════════════════════════════
# ROUTING (P001-P030)
# ══════════════════════════════════════════════════════════════════

ITEM = {"name": "widget", "price": 9.99, "description": "a widget"}


class TestRouting:
    def test_P001_basic_get(self, client, dual_servers):
        fa, rs = hit(client, dual_servers, "get", "/p001-basic-get")
        assert_json_match(fa, rs)

    def test_P002_basic_post(self, client, dual_servers):
        fa, rs = hit(client, dual_servers, "post", "/p002-basic-post", json=ITEM)
        assert_json_match(fa, rs)

    def test_P003_basic_put(self, client, dual_servers):
        fa, rs = hit(client, dual_servers, "put", "/p003-basic-put", json=ITEM)
        assert_json_match(fa, rs)

    def test_P004_basic_patch(self, client, dual_servers):
        fa, rs = hit(client, dual_servers, "patch", "/p004-basic-patch", json=ITEM)
        assert_json_match(fa, rs)

    def test_P005_basic_delete(self, client, dual_servers):
        fa, rs = hit(client, dual_servers, "delete", "/p005-basic-delete")
        assert_json_match(fa, rs)

    def test_P006_path_int(self, client, dual_servers):
        fa, rs = hit(client, dual_servers, "get", "/p006-path-int/42")
        assert_json_match(fa, rs)

    # P006b-g: Rust fast-lane int path-param edges — the door fast-parses
    # `-?[0-9]+` (i64 range) in Rust and MUST fall back to the Pydantic
    # TypeAdapter for every other shape so lax coercions and 422 bodies
    # stay FA-exact.
    def test_P006b_path_int_negative(self, client, dual_servers):
        fa, rs = hit(client, dual_servers, "get", "/p006-path-int/-3")
        assert_json_match(fa, rs)

    def test_P006c_path_int_leading_zeros(self, client, dual_servers):
        # Pydantic lax accepts int("007") == 7 — the Rust fast lane must too.
        fa, rs = hit(client, dual_servers, "get", "/p006-path-int/007")
        assert_json_match(fa, rs)

    def test_P006d_path_int_invalid_422(self, client, dual_servers):
        fa, rs = hit(client, dual_servers, "get", "/p006-path-int/abc")
        assert_json_match(fa, rs)

    def test_P006e_path_int_beyond_i64(self, client, dual_servers):
        # > i64::MAX — outside the Rust fast lane; Pydantic accepts big ints.
        fa, rs = hit(client, dual_servers, "get",
                     "/p006-path-int/99999999999999999999")
        assert_json_match(fa, rs)

    def test_P006f_path_int_plus_sign(self, client, dual_servers):
        # "+7" is outside the fast lane; Pydantic lax parses it as 7.
        fa, rs = hit(client, dual_servers, "get", "/p006-path-int/+7")
        assert_json_match(fa, rs)

    def test_P006g_path_int_underscore(self, client, dual_servers):
        # "1_0" is outside the fast lane; Pydantic lax parses it as 10.
        fa, rs = hit(client, dual_servers, "get", "/p006-path-int/1_0")
        assert_json_match(fa, rs)

    def test_P007_path_str(self, client, dual_servers):
        fa, rs = hit(client, dual_servers, "get", "/p007-path-str/hello")
        assert_json_match(fa, rs)

    def test_P008_path_float(self, client, dual_servers):
        fa, rs = hit(client, dual_servers, "get", "/p008-path-float/3.14")
        assert_json_match(fa, rs)

    def test_P009_query_required(self, client, dual_servers):
        fa, rs = hit(client, dual_servers, "get", "/p009-query-required?q=test")
        assert_json_match(fa, rs)

    def test_P010_query_default(self, client, dual_servers):
        fa, rs = hit(client, dual_servers, "get", "/p010-query-default")
        assert_json_match(fa, rs)

    def test_P010b_query_override(self, client, dual_servers):
        fa, rs = hit(client, dual_servers, "get", "/p010-query-default?q=world")
        assert_json_match(fa, rs)

    def test_P011_query_optional_missing(self, client, dual_servers):
        fa, rs = hit(client, dual_servers, "get", "/p011-query-optional")
        assert_json_match(fa, rs)

    def test_P011b_query_optional_present(self, client, dual_servers):
        fa, rs = hit(client, dual_servers, "get", "/p011-query-optional?q=found")
        assert_json_match(fa, rs)

    def test_P012_query_int(self, client, dual_servers):
        fa, rs = hit(client, dual_servers, "get", "/p012-query-int?n=5")
        assert_json_match(fa, rs)

    def test_P013_query_bool(self, client, dual_servers):
        fa, rs = hit(client, dual_servers, "get", "/p013-query-bool?flag=true")
        assert_json_match(fa, rs)

    def test_P014_query_list(self, client, dual_servers):
        fa, rs = hit(client, dual_servers, "get", "/p014-query-list?items=a&items=b")
        assert_json_match(fa, rs)

    def test_P015_multi_query(self, client, dual_servers):
        fa, rs = hit(client, dual_servers, "get", "/p015-multi-query?skip=5&limit=20")
        assert_json_match(fa, rs)

    def test_P016_header(self, client, dual_servers):
        fa, rs = hit(client, dual_servers, "get", "/p016-header")
        assert_json_match(fa, rs)

    def test_P017_cookie(self, client, dual_servers):
        fa, rs = hit(client, dual_servers, "get", "/p017-cookie")
        assert_json_match(fa, rs)

    def test_P018_path_query(self, client, dual_servers):
        fa, rs = hit(client, dual_servers, "get", "/p018-path-query/7?q=hello")
        assert_json_match(fa, rs)

    def test_P019_body_model(self, client, dual_servers):
        fa, rs = hit(client, dual_servers, "post", "/p019-body-model", json=ITEM)
        assert_json_match(fa, rs)

    def test_P020_body_embed(self, client, dual_servers):
        fa, rs = hit(client, dual_servers, "post", "/p020-body-embed", json={"item": ITEM})
        assert_json_match(fa, rs)

    def test_P021_multi_body(self, client, dual_servers):
        fa, rs = hit(client, dual_servers, "post", "/p021-multi-body",
                     json={"item": ITEM, "extra": "stuff"})
        assert_json_match(fa, rs)

    def test_P022_form(self, client, dual_servers):
        fa, rs = hit(client, dual_servers, "post", "/p022-form",
                     data={"username": "admin", "password": "secret"})
        assert_json_match(fa, rs)

    def test_P023_file_bytes(self, client, dual_servers):
        fa, rs = hit(client, dual_servers, "post", "/p023-file",
                     files={"file": ("test.txt", b"hello world")})
        assert_json_match(fa, rs)

    def test_P024_uploadfile(self, client, dual_servers):
        fa, rs = hit(client, dual_servers, "post", "/p024-uploadfile",
                     files={"file": ("test.txt", b"hello world")})
        assert_json_match(fa, rs)

    def test_P025_form_file(self, client, dual_servers):
        fa, rs = hit(client, dual_servers, "post", "/p025-form-file",
                     data={"name": "doc"}, files={"file": ("test.txt", b"hello world")})
        assert_json_match(fa, rs)

    def test_P026_response_model(self, client, dual_servers):
        fa, rs = hit(client, dual_servers, "get", "/p026-response-model")
        assert_json_match(fa, rs)

    def test_P027_response_model_exclude_unset(self, client, dual_servers):
        fa, rs = hit(client, dual_servers, "get", "/p027-response-model-exclude")
        assert_json_match(fa, rs)

    def test_P028_status_code(self, client, dual_servers):
        fa, rs = hit(client, dual_servers, "get", "/p028-status-code")
        assert_json_match(fa, rs)

    def test_P029_deprecated(self, client, dual_servers):
        fa, rs = hit(client, dual_servers, "get", "/p029-deprecated")
        assert_json_match(fa, rs)

    def test_P030_tags(self, client, dual_servers):
        fa, rs = hit(client, dual_servers, "get", "/p030-tags")
        assert_json_match(fa, rs)


# ══════════════════════════════════════════════════════════════════
# RESPONSE TYPES (P031-P050)
# ══════════════════════════════════════════════════════════════════

class TestResponseTypes:
    def test_P031_json(self, client, dual_servers):
        fa, rs = hit(client, dual_servers, "get", "/p031-json")
        assert_json_match(fa, rs)

    def test_P032_json_response(self, client, dual_servers):
        fa, rs = hit(client, dual_servers, "get", "/p032-json-response")
        assert_json_match(fa, rs)

    def test_P033_html(self, client, dual_servers):
        fa, rs = hit(client, dual_servers, "get", "/p033-html")
        assert_text_match(fa, rs)

    def test_P034_plain(self, client, dual_servers):
        fa, rs = hit(client, dual_servers, "get", "/p034-plain")
        assert_text_match(fa, rs)

    def test_P035_redirect(self, client, dual_servers):
        fa, rs = hit(client, dual_servers, "get", "/p035-redirect", follow_redirects=False)
        assert_status_match(fa, rs)
        assert fa.headers.get("location") == rs.headers.get("location")

    def test_P036_stream(self, client, dual_servers):
        fa, rs = hit(client, dual_servers, "get", "/p036-stream")
        assert_text_match(fa, rs)

    def test_P037_file(self, client, dual_servers):
        fa, rs = hit(client, dual_servers, "get", "/p037-file")
        assert_text_match(fa, rs)

    def test_P038_orjson(self, client, dual_servers):
        fa, rs = hit(client, dual_servers, "get", "/p038-orjson")
        assert_json_match(fa, rs)

    def test_P039_custom_headers(self, client, dual_servers):
        fa, rs = hit(client, dual_servers, "get", "/p039-custom-headers")
        assert_json_match(fa, rs)

    def test_P040_set_cookie(self, client, dual_servers):
        fa, rs = hit(client, dual_servers, "get", "/p040-set-cookie")
        assert_json_match(fa, rs)

    def test_P041_delete_cookie(self, client, dual_servers):
        fa, rs = hit(client, dual_servers, "get", "/p041-delete-cookie")
        assert_json_match(fa, rs)

    def test_P042_status_204(self, client, dual_servers):
        fa, rs = hit(client, dual_servers, "get", "/p042-status-204")
        assert_status_match(fa, rs)

    def test_P043_bytes(self, client, dual_servers):
        fa, rs = hit(client, dual_servers, "get", "/p043-bytes")
        assert_text_match(fa, rs)

    def test_P044_return_model(self, client, dual_servers):
        fa, rs = hit(client, dual_servers, "post", "/p044-return-model", json=ITEM)
        assert_json_match(fa, rs)

    def test_P045_return_none(self, client, dual_servers):
        fa, rs = hit(client, dual_servers, "get", "/p045-none")
        assert_status_match(fa, rs)

    def test_P046_return_string(self, client, dual_servers):
        fa, rs = hit(client, dual_servers, "get", "/p046-string")
        assert fa.text == rs.text

    def test_P047_return_int(self, client, dual_servers):
        fa, rs = hit(client, dual_servers, "get", "/p047-int")
        assert_json_match(fa, rs)

    def test_P048_return_list(self, client, dual_servers):
        fa, rs = hit(client, dual_servers, "get", "/p048-list")
        assert_json_match(fa, rs)

    def test_P049_nested_dict(self, client, dual_servers):
        fa, rs = hit(client, dual_servers, "get", "/p049-nested-dict")
        assert_json_match(fa, rs)

    def test_P050_decimal(self, client, dual_servers):
        fa, rs = hit(client, dual_servers, "get", "/p050-decimal")
        assert_json_match(fa, rs)


# ══════════════════════════════════════════════════════════════════
# VALIDATION (P051-P070)
# ══════════════════════════════════════════════════════════════════

class TestValidation:
    def test_P051_query_ge_pass(self, client, dual_servers):
        fa, rs = hit(client, dual_servers, "get", "/p051-query-ge?n=5")
        assert_json_match(fa, rs)

    def test_P051_query_ge_fail(self, client, dual_servers):
        fa, rs = hit(client, dual_servers, "get", "/p051-query-ge?n=-1")
        assert_status_match(fa, rs)

    def test_P052_query_le_pass(self, client, dual_servers):
        fa, rs = hit(client, dual_servers, "get", "/p052-query-le?n=5")
        assert_json_match(fa, rs)

    def test_P052_query_le_fail(self, client, dual_servers):
        fa, rs = hit(client, dual_servers, "get", "/p052-query-le?n=20")
        assert_status_match(fa, rs)

    def test_P053_query_gt_lt(self, client, dual_servers):
        fa, rs = hit(client, dual_servers, "get", "/p053-query-gt-lt?n=5")
        assert_json_match(fa, rs)

    def test_P054_body_validation_fail(self, client, dual_servers):
        fa, rs = hit(client, dual_servers, "post", "/p054-body-validation",
                     json={"bad": "data"})
        assert_status_match(fa, rs)

    def test_P055_nested_validation_fail(self, client, dual_servers):
        fa, rs = hit(client, dual_servers, "post", "/p055-nested-validation",
                     json={"child": {"value": "not_int"}, "label": "x"})
        assert_status_match(fa, rs)

    def test_P056_enum_query(self, client, dual_servers):
        fa, rs = hit(client, dual_servers, "get", "/p056-enum-query?color=red")
        assert_json_match(fa, rs)

    def test_P057_regex_pass(self, client, dual_servers):
        fa, rs = hit(client, dual_servers, "get", "/p057-regex-query?code=ABC")
        assert_json_match(fa, rs)

    def test_P057_regex_fail(self, client, dual_servers):
        fa, rs = hit(client, dual_servers, "get", "/p057-regex-query?code=abc")
        assert_status_match(fa, rs)

    def test_P058_min_max_length_pass(self, client, dual_servers):
        fa, rs = hit(client, dual_servers, "get", "/p058-min-max-length?s=hi")
        assert_json_match(fa, rs)

    def test_P058_min_max_length_fail(self, client, dual_servers):
        fa, rs = hit(client, dual_servers, "get", "/p058-min-max-length?s=")
        assert_status_match(fa, rs)

    def test_P059_optional_fields(self, client, dual_servers):
        fa, rs = hit(client, dual_servers, "post", "/p059-optional-fields",
                     json={"required_field": "yes"})
        assert_json_match(fa, rs)

    def test_P060_list_field(self, client, dual_servers):
        fa, rs = hit(client, dual_servers, "post", "/p060-list-field",
                     json={"tags": ["a", "b", "c"]})
        assert_json_match(fa, rs)

    def test_P061_nested_model(self, client, dual_servers):
        fa, rs = hit(client, dual_servers, "post", "/p061-nested-model",
                     json={"child": {"value": 42}, "label": "test"})
        assert_json_match(fa, rs)

    def test_P062_field_alias(self, client, dual_servers):
        fa, rs = hit(client, dual_servers, "post", "/p062-field-alias",
                     json={"itemName": "widget"})
        assert_json_match(fa, rs)

    def test_P063_field_description(self, client, dual_servers):
        fa, rs = hit(client, dual_servers, "post", "/p063-field-description",
                     json={"value": 10})
        assert_json_match(fa, rs)

    def test_P064_field_example(self, client, dual_servers):
        fa, rs = hit(client, dual_servers, "post", "/p064-field-example",
                     json={"count": 5})
        assert_json_match(fa, rs)

    def test_P065_field_default(self, client, dual_servers):
        fa, rs = hit(client, dual_servers, "post", "/p065-field-default", json={})
        assert_json_match(fa, rs)

    def test_P066_field_ge_pass(self, client, dual_servers):
        fa, rs = hit(client, dual_servers, "post", "/p066-field-ge",
                     json={"amount": 5})
        assert_json_match(fa, rs)

    def test_P066_field_ge_fail(self, client, dual_servers):
        fa, rs = hit(client, dual_servers, "post", "/p066-field-ge",
                     json={"amount": -1})
        assert_status_match(fa, rs)

    def test_P067_discriminated_union(self, client, dual_servers):
        fa, rs = hit(client, dual_servers, "post", "/p067-discriminated-union",
                     json={"kind": "a", "a_val": 42})
        assert_json_match(fa, rs)

    def test_P068_typed_path(self, client, dual_servers):
        fa, rs = hit(client, dual_servers, "get", "/p068-typed-path/99")
        assert_json_match(fa, rs)


# ══════════════════════════════════════════════════════════════════
# DEPENDENCY INJECTION (P071-P100)
# ══════════════════════════════════════════════════════════════════

class TestDependencyInjection:
    def test_P071_simple_dep(self, client, dual_servers):
        fa, rs = hit(client, dual_servers, "get", "/p071-simple-dep")
        assert_json_match(fa, rs)

    def test_P072_chained_dep(self, client, dual_servers):
        fa, rs = hit(client, dual_servers, "get", "/p072-chained-dep")
        assert_json_match(fa, rs)

    def test_P073_generator_dep(self, client, dual_servers):
        fa, rs = hit(client, dual_servers, "get", "/p073-generator-dep")
        assert_json_match(fa, rs)

    def test_P074_async_dep(self, client, dual_servers):
        fa, rs = hit(client, dual_servers, "get", "/p074-async-dep")
        assert_json_match(fa, rs)

    def test_P075_class_dep(self, client, dual_servers):
        fa, rs = hit(client, dual_servers, "get", "/p075-class-dep")
        assert_json_match(fa, rs)

    def test_P076_dep_no_return(self, client, dual_servers):
        fa, rs = hit(client, dual_servers, "get", "/p076-dep-no-return")
        assert_json_match(fa, rs)

    def test_P077_dep_override(self, client, dual_servers):
        fa, rs = hit(client, dual_servers, "get", "/p077-dep-override")
        assert_json_match(fa, rs)

    def test_P078_dep_with_query(self, client, dual_servers):
        fa, rs = hit(client, dual_servers, "get", "/p078-dep-with-query?q=custom")
        assert_json_match(fa, rs)

    def test_P079_dep_with_header(self, client, dual_servers):
        fa, rs = hit(client, dual_servers, "get", "/p079-dep-with-header")
        assert_json_match(fa, rs)

    def test_P080_dep_with_request(self, client, dual_servers):
        fa, rs = hit(client, dual_servers, "get", "/p080-dep-with-request")
        assert_json_match(fa, rs)

    def test_P081_oauth2(self, client, dual_servers):
        fa, rs = hit(client, dual_servers, "get", "/p081-security-oauth2",
                     headers={"Authorization": "Bearer mytoken123"})
        assert_json_match(fa, rs)

    def test_P081_oauth2_noauth(self, client, dual_servers):
        fa, rs = hit(client, dual_servers, "get", "/p081-security-oauth2")
        assert_status_match(fa, rs)

    def test_P082_bearer(self, client, dual_servers):
        fa, rs = hit(client, dual_servers, "get", "/p082-security-bearer",
                     headers={"Authorization": "Bearer xyz"})
        assert_json_match(fa, rs)

    def test_P083_apikey(self, client, dual_servers):
        fa, rs = hit(client, dual_servers, "get", "/p083-security-apikey",
                     headers={"X-API-Key": "secret123"})
        assert_json_match(fa, rs)

    def test_P084_basic(self, client, dual_servers):
        creds = base64.b64encode(b"user:pass").decode()
        fa, rs = hit(client, dual_servers, "get", "/p084-security-basic",
                     headers={"Authorization": f"Basic {creds}"})
        assert_json_match(fa, rs)

    def test_P085_security_scopes(self, client, dual_servers):
        fa, rs = hit(client, dual_servers, "get", "/p085-security-scopes",
                     headers={"Authorization": "Bearer scopedtoken"})
        assert_json_match(fa, rs)

    def test_P086_oauth2_form(self, client, dual_servers):
        fa, rs = hit(client, dual_servers, "post", "/p086-oauth2-form",
                     data={"username": "admin", "password": "secret",
                           "scope": "", "grant_type": "password"})
        assert_json_match(fa, rs)

    def test_P087_dep_cached(self, client, dual_servers):
        fa, rs = hit(client, dual_servers, "get", "/p087-dep-cached")
        # Both should return same=True (dep called once due to caching)
        assert fa.json()["same"] == rs.json()["same"]

    def test_P088_async_generator_dep(self, client, dual_servers):
        fa, rs = hit(client, dual_servers, "get", "/p088-dep-async-generator")
        assert_json_match(fa, rs)

    def test_P089_nested_deps_3_deep(self, client, dual_servers):
        fa, rs = hit(client, dual_servers, "get", "/p089-nested-deps-3-deep")
        assert_json_match(fa, rs)

    def test_P090_dep_exception(self, client, dual_servers):
        fa, rs = hit(client, dual_servers, "get", "/p090-dep-exception")
        assert_status_match(fa, rs)

    def test_P091_router_dep(self, client, dual_servers):
        fa, rs = hit(client, dual_servers, "get", "/p091-router-dep")
        assert_json_match(fa, rs)

    def test_P092_include_router_dep(self, client, dual_servers):
        fa, rs = hit(client, dual_servers, "get", "/p092-include-router-dep")
        assert_json_match(fa, rs)

    def test_P093_dep_background(self, client, dual_servers):
        fa, rs = hit(client, dual_servers, "get", "/p093-dep-background")
        assert_json_match(fa, rs)

    def test_P094_dep_response(self, client, dual_servers):
        fa, rs = hit(client, dual_servers, "get", "/p094-dep-response")
        assert_json_match(fa, rs)

    def test_P095_dep_websocket_skip(self, client, dual_servers):
        fa, rs = hit(client, dual_servers, "get", "/p095-dep-websocket")
        assert_json_match(fa, rs)

    def test_P096_multiple_deps(self, client, dual_servers):
        fa, rs = hit(client, dual_servers, "get", "/p096-multiple-deps")
        assert_json_match(fa, rs)

    def test_P097_dep_default(self, client, dual_servers):
        fa, rs = hit(client, dual_servers, "get", "/p097-dep-default")
        assert_json_match(fa, rs)

    def test_P098_annotated_dep(self, client, dual_servers):
        fa, rs = hit(client, dual_servers, "get", "/p098-annotated-dep")
        assert_json_match(fa, rs)

    def test_P099_security_auto_error_no_token(self, client, dual_servers):
        fa, rs = hit(client, dual_servers, "get", "/p099-security-auto-error")
        assert_json_match(fa, rs)

    def test_P099_security_auto_error_with_token(self, client, dual_servers):
        fa, rs = hit(client, dual_servers, "get", "/p099-security-auto-error",
                     headers={"Authorization": "Bearer tok99"})
        assert_json_match(fa, rs)

    def test_P100_dep_yield_cleanup(self, client, dual_servers):
        fa, rs = hit(client, dual_servers, "get", "/p100-dep-yield-cleanup")
        assert_json_match(fa, rs)


# ══════════════════════════════════════════════════════════════════
# DOCS / OPENAPI (P101-P106) — corpus expansion (P1)
#
# These guard the auto-generated docs surface so the L6 lever
# (drop the embedded Swagger/ReDoc HTML in favour of Python-rendered
# HTML) can be verified safe: /docs and /redoc must still serve HTML
# that loads the schema, and /openapi.json must expose the same paths.
# HTML bodies are allowed to differ between renderers, so we assert on
# status + content-type + structural openapi equality, not byte-equality.
# ══════════════════════════════════════════════════════════════════


class TestDocsOpenAPI:
    def test_P101_openapi_json_status_and_content_type(self, client, dual_servers):
        fa, rs = hit(client, dual_servers, "get", "/openapi.json")
        assert_status_match(fa, rs)
        assert fa.status_code == 200, fa.text
        assert "application/json" in fa.headers.get("content-type", "")
        assert "application/json" in rs.headers.get("content-type", "")

    def test_P102_openapi_paths_set_matches(self, client, dual_servers):
        fa, rs = hit(client, dual_servers, "get", "/openapi.json")
        fa_paths = set(fa.json().get("paths", {}))
        rs_paths = set(rs.json().get("paths", {}))
        assert fa_paths == rs_paths, (
            f"openapi paths differ:\n  only FA={sorted(fa_paths - rs_paths)}\n"
            f"  only RS={sorted(rs_paths - fa_paths)}"
        )

    def test_P103_openapi_version_and_info(self, client, dual_servers):
        fa, rs = hit(client, dual_servers, "get", "/openapi.json")
        fa_j, rs_j = fa.json(), rs.json()
        assert fa_j.get("openapi") == rs_j.get("openapi"), (
            f"openapi version: FA={fa_j.get('openapi')} RS={rs_j.get('openapi')}"
        )
        assert fa_j.get("info") == rs_j.get("info"), (
            f"info block differs:\n  FA={fa_j.get('info')}\n  RS={rs_j.get('info')}"
        )

    def test_P104_swagger_docs_served(self, client, dual_servers):
        fa, rs = hit(client, dual_servers, "get", "/docs")
        assert_status_match(fa, rs)
        assert fa.status_code == 200, fa.text
        assert "text/html" in fa.headers.get("content-type", "")
        assert "text/html" in rs.headers.get("content-type", "")
        # Both must reference the schema URL so the UI can load it.
        assert "/openapi.json" in fa.text
        assert "/openapi.json" in rs.text

    def test_P105_redoc_served(self, client, dual_servers):
        fa, rs = hit(client, dual_servers, "get", "/redoc")
        assert_status_match(fa, rs)
        assert fa.status_code == 200, fa.text
        assert "text/html" in fa.headers.get("content-type", "")
        assert "text/html" in rs.headers.get("content-type", "")
        assert "/openapi.json" in fa.text
        assert "/openapi.json" in rs.text

    def test_P106_openapi_components_schemas_set_matches(self, client, dual_servers):
        fa, rs = hit(client, dual_servers, "get", "/openapi.json")
        fa_c = set(fa.json().get("components", {}).get("schemas", {}))
        rs_c = set(rs.json().get("components", {}).get("schemas", {}))
        assert fa_c == rs_c, (
            f"component schema names differ:\n  only FA={sorted(fa_c - rs_c)}\n"
            f"  only RS={sorted(rs_c - fa_c)}"
        )


# ══════════════════════════════════════════════════════════════════
# VALIDATION 422 BODIES (P110-P119) — corpus expansion (P1)
#
# Guards the FULL 422 error body (not just status) byte-for-byte
# across every shape the Rust 422 shaper emits — int/float/bool path,
# int/bool query, missing required query, body model (missing field,
# wrong type, malformed JSON), and a nested body error. This is the
# safety net for the P2 'L4' lever (collapse the 15-variant Rust 422
# shaper into 2 functions): the {detail:[{type,loc,msg,input}]} shape
# must stay identical to upstream FastAPI through the refactor.
# ══════════════════════════════════════════════════════════════════


def assert_422_body_match(fa_r, rs_r):
    """422 status + identical detail array (order-insensitive)."""
    assert fa_r.status_code == rs_r.status_code, (
        f"status mismatch: FA={fa_r.status_code} RS={rs_r.status_code}\n"
        f"  FA body={fa_r.text[:300]}\n  RS body={rs_r.text[:300]}"
    )
    assert fa_r.status_code == 422, f"expected 422, got {fa_r.status_code}: {fa_r.text[:300]}"

    def norm(resp):
        detail = resp.json().get("detail", [])
        # Order-insensitive: FA/turbo may emit multi-error details in a
        # different order. Compare as a set of canonical tuples.
        return sorted(
            (
                e.get("type"),
                tuple(e.get("loc", [])),
                e.get("msg"),
                repr(e.get("input")),
            )
            for e in detail
        )

    fa_n, rs_n = norm(fa_r), norm(rs_r)
    assert fa_n == rs_n, (
        f"422 detail differs:\n  FA={fa_n}\n  RS={rs_n}"
    )


class TestValidation422:
    def test_P110_path_int_bad(self, client, dual_servers):
        fa, rs = hit(client, dual_servers, "get", "/p006-path-int/notanint")
        assert_422_body_match(fa, rs)

    def test_P111_path_float_bad(self, client, dual_servers):
        fa, rs = hit(client, dual_servers, "get", "/p008-path-float/notafloat")
        assert_422_body_match(fa, rs)

    def test_P112_query_int_bad(self, client, dual_servers):
        fa, rs = hit(client, dual_servers, "get", "/p012-query-int?n=notanint")
        assert_422_body_match(fa, rs)

    def test_P113_query_bool_bad(self, client, dual_servers):
        fa, rs = hit(client, dual_servers, "get", "/p013-query-bool?flag=notabool")
        assert_422_body_match(fa, rs)

    def test_P114_query_required_missing(self, client, dual_servers):
        fa, rs = hit(client, dual_servers, "get", "/p009-query-required")
        assert_422_body_match(fa, rs)

    def test_P115_body_missing_required_field(self, client, dual_servers):
        # Item requires name + price; omit price.
        fa, rs = hit(client, dual_servers, "post", "/p019-body-model",
                     json={"name": "x"})
        assert_422_body_match(fa, rs)

    def test_P116_body_wrong_type(self, client, dual_servers):
        # price must be a float-coercible value.
        fa, rs = hit(client, dual_servers, "post", "/p019-body-model",
                     json={"name": "x", "price": "not_a_number"})
        assert_422_body_match(fa, rs)

    def test_P117_body_malformed_json(self, client, dual_servers):
        fa, rs = hit(client, dual_servers, "post", "/p019-body-model",
                     content=b"{not valid json",
                     headers={"content-type": "application/json"})
        assert_422_body_match(fa, rs)

    def test_P118_body_wrong_root_type(self, client, dual_servers):
        # Send a JSON array where an object is expected.
        fa, rs = hit(client, dual_servers, "post", "/p019-body-model",
                     json=[1, 2, 3])
        assert_422_body_match(fa, rs)

    def test_P119_nested_body_error(self, client, dual_servers):
        fa, rs = hit(client, dual_servers, "post", "/p055-nested-validation",
                     json={"child": {"value": "not_int"}, "label": "x"})
        assert_422_body_match(fa, rs)


# ══════════════════════════════════════════════════════════════════
# STATIC FILES (P120-P124) — corpus expansion (P1)
#
# Safety net for the P2 'L3' lever (replace the hand-rolled 222-line
# CachedServeDir with tower-http's ServeDir): static file serving must
# stay byte-identical, with the same content-type-by-extension and the
# same 404 for missing files.
# ══════════════════════════════════════════════════════════════════


def _ct_base(resp):
    return resp.headers.get("content-type", "").split(";")[0].strip().lower()


class TestStaticFiles:
    def test_P120_static_txt_body(self, client, dual_servers):
        fa, rs = hit(client, dual_servers, "get", "/static/data.txt")
        assert_status_match(fa, rs)
        assert fa.status_code == 200, fa.text
        assert fa.content == rs.content, (
            f"static body differs:\n  FA={fa.content!r}\n  RS={rs.content!r}"
        )

    def test_P121_static_txt_content_type(self, client, dual_servers):
        fa, rs = hit(client, dual_servers, "get", "/static/data.txt")
        assert _ct_base(fa) == _ct_base(rs), (
            f"content-type differs: FA={_ct_base(fa)} RS={_ct_base(rs)}"
        )

    def test_P122_static_css_content_type(self, client, dual_servers):
        fa, rs = hit(client, dual_servers, "get", "/static/style.css")
        assert_status_match(fa, rs)
        assert fa.content == rs.content
        assert _ct_base(fa) == _ct_base(rs), (
            f"css content-type differs: FA={_ct_base(fa)} RS={_ct_base(rs)}"
        )

    def test_P123_static_js_content_type(self, client, dual_servers):
        fa, rs = hit(client, dual_servers, "get", "/static/app.js")
        assert_status_match(fa, rs)
        assert fa.content == rs.content
        assert _ct_base(fa) == _ct_base(rs), (
            f"js content-type differs: FA={_ct_base(fa)} RS={_ct_base(rs)}"
        )

    def test_P124_static_missing_404(self, client, dual_servers):
        fa, rs = hit(client, dual_servers, "get", "/static/does-not-exist.txt")
        assert_status_match(fa, rs)
        assert fa.status_code == 404, fa.text

    # P125-P131: content-type parity on extensions where mime_guess/ServeDir
    # DIVERGES from Python mimetypes. Proves the L3 fix matches Starlette for
    # ALL extensions (it derives content-type from Python mimetypes), not just
    # the .js case that first surfaced the bug.
    @pytest.mark.parametrize("path", [
        "/static/mod.mjs",
        "/static/doc.xml",
        "/static/conf.yaml",
        "/static/icon.svg",
        "/static/readme.md",
        "/static/data.json",
        "/static/blob.unknownext",
    ])
    def test_P125_static_divergent_ext_content_type(self, client, dual_servers, path):
        fa, rs = hit(client, dual_servers, "get", path)
        assert_status_match(fa, rs)
        assert fa.status_code == 200, fa.text
        assert fa.content == rs.content
        assert _ct_base(fa) == _ct_base(rs), (
            f"{path}: content-type differs — FA={_ct_base(fa)} RS={_ct_base(rs)}"
        )


# ══════════════════════════════════════════════════════════════════
# MULTIPART BYTE PARITY (P132-P137) — corpus expansion (P1)
#
# The Rust path parses multipart with `multer`; the Python in-process
# dispatcher parses it independently. Both must match Starlette/
# python-multipart byte-for-byte on filename, content-type, exact bytes,
# multiple files, and form fields interleaved with files. This is the
# safety net for collapsing the two request engines (P2 "two doors").
# ══════════════════════════════════════════════════════════════════


class TestMultipartParity:
    def test_P132_filename_and_content_type_roundtrip(self, client, dual_servers):
        fa, rs = hit(client, dual_servers, "post", "/pmp-echo-file",
                     files={"file": ("rep ort.txt", b"hello world", "text/plain")})
        assert_json_match(fa, rs)

    def test_P133_no_explicit_content_type(self, client, dual_servers):
        # httpx will guess; both servers must report the SAME guess.
        fa, rs = hit(client, dual_servers, "post", "/pmp-echo-file",
                     files={"file": ("data.csv", b"a,b,c\n1,2,3")})
        assert_json_match(fa, rs)

    def test_P134_multiple_files_same_field(self, client, dual_servers):
        files = [
            ("files", ("a.txt", b"alpha", "text/plain")),
            ("files", ("b.txt", b"beta", "text/plain")),
            ("files", ("c.bin", b"\x00\x01\x02gamma", "application/octet-stream")),
        ]
        fa, rs = hit(client, dual_servers, "post", "/pmp-multi-files", files=files)
        assert_json_match(fa, rs)

    def test_P135_form_fields_and_file_interleaved(self, client, dual_servers):
        fa, rs = hit(client, dual_servers, "post", "/pmp-form-and-files",
                     data={"title": "My Doc", "tags": ["x", "y", "z"]},
                     files={"file": ("body.txt", b"the body", "text/plain")})
        assert_json_match(fa, rs)

    def test_P136_binary_content_exact_bytes(self, client, dual_servers):
        # All 256 byte values — catches any charset / boundary mangling.
        blob = bytes(range(256)) * 4
        fa, rs = hit(client, dual_servers, "post", "/pmp-binary",
                     files={"file": ("blob.bin", blob, "application/octet-stream")})
        assert_json_match(fa, rs)

    def test_P137_filename_with_unicode(self, client, dual_servers):
        fa, rs = hit(client, dual_servers, "post", "/pmp-echo-file",
                     files={"file": ("résumé.txt", b"unicode name", "text/plain")})
        assert_json_match(fa, rs)


# ══════════════════════════════════════════════════════════════════
# MIDDLEWARE ORDERING (P140-P143) — corpus expansion (P1)
#
# FastAPI/Starlette run middleware in reverse-registration order
# (last-registered = outermost). Two @app.middleware("http") each append
# their name to X-MW-Trace on the way out; the resulting header reveals
# nesting order. The Rust engine and the Python in-process dispatcher
# apply middleware independently, so this guards that they nest identically
# before we collapse the two engines (P2 "two doors").
# ══════════════════════════════════════════════════════════════════


class TestMiddlewareOrdering:
    def test_P140_mw_trace_order_matches(self, client, dual_servers):
        fa, rs = hit(client, dual_servers, "get", "/pmw-trace")
        assert_json_match(fa, rs)
        fa_trace = fa.headers.get("x-mw-trace")
        rs_trace = rs.headers.get("x-mw-trace")
        assert fa_trace == rs_trace, (
            f"middleware nesting order differs: FA={fa_trace!r} RS={rs_trace!r}"
        )
        # Sanity: both middlewares ran (inner appended before outer).
        assert fa_trace == "inner,outer", f"unexpected FA trace: {fa_trace!r}"

    def test_P141_mw_headers_present_on_normal_route(self, client, dual_servers):
        # Middleware-added headers appear on a regular (non-dedicated) route too.
        fa, rs = hit(client, dual_servers, "get", "/p001-basic-get")
        assert_json_match(fa, rs)
        assert fa.headers.get("x-mw-trace") == rs.headers.get("x-mw-trace")

    def test_P142_mw_headers_on_error_response(self, client, dual_servers):
        # Middleware wraps error responses too (422) — trace must still match.
        fa, rs = hit(client, dual_servers, "get", "/p012-query-int?n=bad")
        assert_status_match(fa, rs)
        assert fa.headers.get("x-mw-trace") == rs.headers.get("x-mw-trace")

    def test_P143_mw_headers_on_404(self, client, dual_servers):
        # Middleware wraps the 404 fallback too.
        fa, rs = hit(client, dual_servers, "get", "/no-such-route-xyz")
        assert_status_match(fa, rs)
        assert fa.headers.get("x-mw-trace") == rs.headers.get("x-mw-trace")


# ══════════════════════════════════════════════════════════════════
# WEBSOCKET PARITY (P150-P154) — corpus expansion (P1), folded into the gate
#
# Drives a real ws client against BOTH dual servers (FastAPI+uvicorn and turbo
# app.run) and diffs observable behaviour. turbo runs each WS connection on a
# dedicated per-connection OS thread + local event loop (src/websocket.rs), with
# the GIL RELEASED while parked on receive (py.detach + crossbeam) — so this is
# the in-the-gate safety net for the dispatcher collapse (WS has its own
# in-process dispatch twin). Endpoints live on parity_app under /pws/*.
# ══════════════════════════════════════════════════════════════════


def _ws_url(dual_servers, which, path):
    port = dual_servers.fa_port if which == "fa" else dual_servers.rs_port
    return f"ws://127.0.0.1:{port}{path}"


def _ws_both(dual_servers, path, scenario):
    """Run ``scenario(connect, url)`` against FA and turbo; return (fa, rs)."""
    from websockets.sync.client import connect
    out = {}
    for which in ("fa", "rs"):
        out[which] = scenario(connect, _ws_url(dual_servers, which, path))
    return out["fa"], out["rs"]


class TestWebSocketParity:
    def test_P150_ws_echo_text(self, dual_servers):
        def scen(connect, url):
            with connect(url) as ws:
                ws.send("hello")
                a = ws.recv()
                ws.send("world")
                b = ws.recv()
                return [a, b]
        fa, rs = _ws_both(dual_servers, "/pws/echo", scen)
        assert fa == rs == ["hello", "world"], f"FA={fa} RS={rs}"

    def test_P151_ws_echo_json(self, dual_servers):
        import json as _j

        def scen(connect, url):
            with connect(url) as ws:
                ws.send(_j.dumps({"name": "bob", "n": 3}))
                return _j.loads(ws.recv())
        fa, rs = _ws_both(dual_servers, "/pws/json", scen)
        assert fa == rs == {"echo": {"name": "bob", "n": 3}}, f"FA={fa} RS={rs}"

    def test_P152_ws_scope_path_query_header(self, dual_servers):
        import json as _j

        def scen(connect, url):
            with connect(url, additional_headers={"x-test": "hdrval"}) as ws:
                return _j.loads(ws.recv())
        fa, rs = _ws_both(dual_servers, "/pws/scope/alice?q=42", scen)
        assert fa == rs, f"FA={fa} RS={rs}"
        assert fa == {"who": "alice", "q": "42", "hdr": "hdrval"}, fa

    def test_P153_ws_close_code_and_reason(self, dual_servers):
        def scen(connect, url):
            with connect(url) as ws:
                payload = ws.recv()
                code = reason = None
                try:
                    ws.recv()
                except Exception as exc:  # ConnectionClosed*
                    code = getattr(exc, "code", None)
                    reason = getattr(exc, "reason", None)
                return {"payload": payload, "code": code, "reason": reason}
        fa, rs = _ws_both(dual_servers, "/pws/close-code", scen)
        assert fa == rs, f"FA={fa} RS={rs}"
        assert fa["payload"] == "first-and-only" and fa["code"] == 4002, fa

    def test_P154_ws_dep_reject_then_pass(self, dual_servers):
        # Reject = dep raises WebSocketException(4401) before accept (the
        # normative Starlette path). The connection is refused; the client
        # sees either an HTTP reject status or a close code — capture both
        # shapes and just require FA and turbo agree.
        def scen_reject(connect, url):
            try:
                with connect(url) as ws:
                    ws.recv()
            except Exception as exc:
                return {
                    "kind": type(exc).__name__,
                    "code": getattr(exc, "code", None),
                    "status": getattr(getattr(exc, "response", None), "status_code", None),
                }
            return {"kind": "connected", "code": None, "status": None}

        def scen_pass(connect, url):
            with connect(url) as ws:
                return ws.recv()

        fa_r, rs_r = _ws_both(dual_servers, "/pws/dep", scen_reject)
        assert fa_r == rs_r, f"reject: FA={fa_r} RS={rs_r}"
        fa_p, rs_p = _ws_both(dual_servers, "/pws/dep?token=secret", scen_pass)
        assert fa_p == rs_p == "authed:secret", f"pass: FA={fa_p} RS={rs_p}"

    def test_P155_ws_close_before_accept(self, dual_servers):
        # A WS handler that does `await ws.close()` BEFORE accept() must refuse
        # the handshake (HTTP 403), matching Starlette — NOT complete the upgrade
        # then send a WS close. Was a real divergence (turbo returned WS close
        # 4401); fixed by routing pre-accept close through the handshake-refuse
        # path (websockets.py close()).
        def scen(connect, url):
            try:
                with connect(url) as ws:
                    ws.recv()
            except Exception as exc:
                return {
                    "kind": type(exc).__name__,
                    "status": getattr(getattr(exc, "response", None), "status_code", None),
                }
            return {"kind": "connected", "status": None}
        fa, rs = _ws_both(dual_servers, "/pws/close-before-accept", scen)
        assert fa == rs, f"FA={fa} RS={rs}"
        assert fa["status"] == 403, f"expected HTTP 403 handshake refusal, got {fa}"
