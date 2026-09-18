from bqa.session import load_or_create_secret, make_token, read_token

SECRET = b"unit-test-secret-that-is-long-enough"
ALLOWED = {"a@x.org", "b@x.org"}


def test_roundtrip():
    tok = make_token(SECRET, "A@X.org", 3600, now=1000)
    assert read_token(SECRET, tok, ALLOWED, now=2000) == "a@x.org"


def test_expired_token_is_rejected():
    tok = make_token(SECRET, "a@x.org", 60, now=1000)
    assert read_token(SECRET, tok, ALLOWED, now=1061) is None


def test_tampered_token_is_rejected():
    tok = make_token(SECRET, "a@x.org", 3600, now=1000)
    email_b64, exp, sig = tok.split(".")
    assert read_token(SECRET, f"{email_b64}.{int(exp) + 999999}.{sig}", ALLOWED, now=2000) is None
    other = make_token(SECRET, "b@x.org", 3600, now=1000).split(".")[0]
    assert read_token(SECRET, f"{other}.{exp}.{sig}", ALLOWED, now=2000) is None
    assert read_token(b"another-secret", tok, ALLOWED, now=2000) is None


def test_delisted_address_is_rejected():
    tok = make_token(SECRET, "a@x.org", 3600, now=1000)
    assert read_token(SECRET, tok, {"b@x.org"}, now=2000) is None


def test_garbage_is_rejected():
    for bad in (None, "", "x", "a.b", "a.b.c", "!!.12.deadbeef"):
        assert read_token(SECRET, bad, ALLOWED, now=2000) is None


def test_secret_is_persisted_and_reused(tmp_path):
    p = tmp_path / "secret"
    s1 = load_or_create_secret("", p)
    s2 = load_or_create_secret("", p)
    assert s1 == s2 and len(s1) >= 32
    assert load_or_create_secret("configured", p) == b"configured"
