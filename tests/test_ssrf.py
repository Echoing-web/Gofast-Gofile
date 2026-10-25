"""TDD: gofast52.ssrf URLGuard contracts (SPEC 4.11)."""
import pytest

from gofast52.errors import FatalError
from gofast52.ssrf import URLGuard


def resolver_map(mapping, default=("203.0.113.10",)):  # default is TEST-NET but we only
    # use it for hosts not in the map — tests never rely on default being public
    def resolve(host):
        if host in mapping:
            return list(mapping[host])
        return list(default)
    return resolve


class TestScheme:
    def test_https_allowed(self):
        g = URLGuard(resolver=resolver_map({"cdn.example": ["104.21.0.5"]}))
        g.validate("https://cdn.example/file")  # no raise

    def test_http_allowed(self):
        g = URLGuard(resolver=resolver_map({"cdn.example": ["104.21.0.5"]}))
        g.validate("http://cdn.example/file")

    @pytest.mark.parametrize("scheme", ["ftp", "file", "gopher", "ws", "javascript", ""])
    def test_other_schemes_rejected(self, scheme):
        g = URLGuard(resolver=resolver_map({}))
        with pytest.raises(FatalError):
            g.validate(f"{scheme}://cdn.example/file" if scheme else "//cdn.example/file")

    def test_no_host_rejected(self):
        g = URLGuard(resolver=resolver_map({}))
        with pytest.raises(FatalError):
            g.validate("https:///file")


class TestIpLiteral:
    @pytest.mark.parametrize("host", [
        "127.0.0.1", "10.0.0.5", "172.16.1.1", "192.168.1.1", "169.254.169.254",
        "0.0.0.0", "100.64.1.1", "224.0.0.1", "240.0.0.1", "255.255.255.255",
        "198.18.0.1", "192.0.2.1", "203.0.113.1", "198.51.100.1",
        "[::1]", "[fe80::1]", "[fc00::1]", "[fd12::1]", "[2001:db8::1]",
        "[::ffff:10.0.0.1]", "[::]",
    ])
    def test_private_or_reserved_rejected(self, host):
        g = URLGuard(resolver=resolver_map({}))
        with pytest.raises(FatalError):
            g.validate(f"http://{host}/x")

    @pytest.mark.parametrize("host", ["104.21.0.5", "1.1.1.1", "8.8.8.8", "[2606:4700::1111]"])
    def test_public_allowed(self, host):
        g = URLGuard(resolver=resolver_map({}))
        g.validate(f"http://{host}/x")


class TestHostname:
    def test_localhost_rejected(self):
        g = URLGuard(resolver=resolver_map({}))
        with pytest.raises(FatalError):
            g.validate("https://localhost/file")

    def test_dot_local_rejected(self):
        g = URLGuard(resolver=resolver_map({}))
        with pytest.raises(FatalError):
            g.validate("https://printer.local/file")

    def test_resolves_to_public_allowed(self):
        g = URLGuard(resolver=resolver_map({"store1.gofile.io": ["104.21.0.5", "172.67.0.1"]}))
        g.validate("https://store1.gofile.io/download/web/x")

    def test_resolves_to_private_rejected(self):
        g = URLGuard(resolver=resolver_map({"evil.example": ["10.0.0.5"]}))
        with pytest.raises(FatalError):
            g.validate("https://evil.example/x")

    def test_mixed_resolution_any_private_rejected(self):
        g = URLGuard(resolver=resolver_map({"sneaky.example": ["1.1.1.1", "192.168.0.1"]}))
        with pytest.raises(FatalError):
            g.validate("https://sneaky.example/x")

    def test_resolution_failure_rejected(self):
        def boom(host):
            raise OSError("dns fail")
        g = URLGuard(resolver=boom)
        with pytest.raises(FatalError):
            g.validate("https://nonexistent.invalid/x")

    def test_userinfo_trick_rejected(self):
        # host is the part after @ -> private IP
        g = URLGuard(resolver=resolver_map({}))
        with pytest.raises(FatalError):
            g.validate("http://legit.example@10.0.0.5/x")

    def test_port_ignored_for_public(self):
        g = URLGuard(resolver=resolver_map({"cdn.example": ["104.21.0.5"]}))
        g.validate("https://cdn.example:8443/file")
