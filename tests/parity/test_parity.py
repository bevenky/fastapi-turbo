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
