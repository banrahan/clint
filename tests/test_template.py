from auto_test_tool.template import substitute_template, substitute_in_mapping
import pytest


def test_simple_substitution():
    assert substitute_template("port={port}", {"port": 9000}) == "port=9000"


def test_multiple_names():
    out = substitute_template(
        "azd ai agent run --port {agent} && curl localhost:{agent}/health",
        {"agent": 49733},
    )
    assert out == "azd ai agent run --port 49733 && curl localhost:49733/health"


def test_two_distinct_names():
    out = substitute_template("{a}:{b}", {"a": 1, "b": 2})
    assert out == "1:2"


def test_unknown_placeholder_raises():
    with pytest.raises(KeyError) as exc:
        substitute_template("hello {missing}", {"port": 1})
    assert "missing" in str(exc.value)


def test_double_brace_escape():
    assert substitute_template("{{port}}", {"port": 1}) == "{port}"


def test_double_brace_with_real_placeholder():
    out = substitute_template("{{literal}} and {real}", {"real": 42})
    assert out == "{literal} and 42"


def test_non_identifier_braces_left_alone():
    src = """curl -d '{"msg": "hi"}' && echo ${HOME:-/tmp}"""
    assert substitute_template(src, {}) == src


def test_non_string_input_passthrough():
    assert substitute_template(123, {}) == 123  # type: ignore[arg-type]


def test_substitute_in_mapping():
    out = substitute_in_mapping({"FOO": "{port}", "BAR": "static"}, {"port": 5})
    assert out == {"FOO": "5", "BAR": "static"}


def test_empty_vars_no_placeholders():
    assert substitute_template("plain text", {}) == "plain text"
