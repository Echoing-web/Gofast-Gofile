"""BDD: security boundaries (features/security.feature)."""
from pytest_bdd import scenarios, given, when, then

from gofast52.errors import FatalError
from gofast52.ssrf import URLGuard
from gofast52.transport import RequestsTransport
from tests.test_transport import FakeResponse, FakeSession

scenarios("features/security.feature")


@given("一个解析到10.0.0.5的直链", target_fixture="target_url")
def given_private_link(ctx):
    ctx["guard"] = URLGuard(resolver=lambda host: ["10.0.0.5"])
    ctx["url"] = "https://cdn.example/file.bin"
    return ctx["url"]


@given("一个ftp协议的直链", target_fixture="target_url")
def given_ftp_link(ctx):
    ctx["guard"] = URLGuard(resolver=lambda host: ["104.21.0.5"])
    ctx["url"] = "ftp://cdn.example/file.bin"
    return ctx["url"]


@given("一个会302跳转到127.0.0.1的直链", target_fixture="target_url")
def given_redirect_link(ctx):
    ctx["url"] = "https://cdn.example/file.bin"
    session = FakeSession([
        FakeResponse(302, {"Location": "http://127.0.0.1/steal"}),
    ])
    ctx["session"] = session
    ctx["transport"] = RequestsTransport(
        session=session,
        guard=URLGuard(resolver=lambda host: ["104.21.0.5"]))
    return ctx["url"]


@when("尝试校验该直链")
def when_validate(ctx):
    try:
        ctx["guard"].validate(ctx["url"])
        ctx["error"] = None
    except FatalError as e:
        ctx["error"] = e


@when("通过传输层发起请求")
def when_transport_request(ctx):
    try:
        with ctx["transport"].stream(ctx["url"]):
            ctx["error"] = None
    except FatalError as e:
        ctx["error"] = e


@then("校验以致命错误拒绝")
def then_rejected(ctx):
    assert isinstance(ctx["error"], FatalError)


@then("请求以致命错误终止")
def then_request_rejected(ctx):
    assert isinstance(ctx["error"], FatalError)


@then("环回地址从未收到任何请求")
def then_loopback_never_contacted(ctx):
    urls = [c["url"] for c in ctx["session"].calls]
    assert urls == ["https://cdn.example/file.bin"]
