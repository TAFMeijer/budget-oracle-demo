from bqa.ratelimit import LoginLimiter, client_ip


def make():
    return LoginLimiter(max_failures=3, window=600, lockout=60, max_lockout=480,
                        global_max_failures=10, global_lockout=300)


def test_no_lock_before_limit():
    lim = make()
    assert lim.failure("1.1.1.1", "a@x", now=0) == 0
    assert lim.failure("1.1.1.1", "a@x", now=1) == 0
    assert lim.retry_after("1.1.1.1", "a@x", now=2) == 0


def test_lock_at_limit_and_backoff_doubles():
    lim = make()
    for t in (0, 1):
        lim.failure("1.1.1.1", "a@x", now=t)
    assert lim.failure("1.1.1.1", "a@x", now=2) == 60          # third failure -> 60 s lockout
    assert lim.retry_after("1.1.1.1", "a@x", now=30) == 32
    assert lim.retry_after("1.1.1.1", "a@x", now=62) == 0
    for t in (100, 101):
        lim.failure("1.1.1.1", "a@x", now=t)
    assert lim.failure("1.1.1.1", "a@x", now=102) == 120       # second lockout doubles
    for t in (300, 301, 302, 500, 501, 502, 1000, 1001):
        lim.failure("1.1.1.1", "a@x", now=t)
    assert lim.failure("1.1.1.1", "a@x", now=1002) == 480      # capped at max_lockout


def test_failures_expire_after_window():
    lim = make()
    lim.failure("1.1.1.1", "a@x", now=0)
    lim.failure("1.1.1.1", "a@x", now=1)
    assert lim.failure("1.1.1.1", "a@x", now=700) == 0         # the first two are forgotten


def test_email_lock_holds_across_ips():
    lim = make()
    for ip in ("1.1.1.1", "2.2.2.2"):
        lim.failure(ip, "a@x", now=0)
    assert lim.failure("3.3.3.3", "a@x", now=1) == 60          # e-mail bucket hit the limit
    assert lim.retry_after("4.4.4.4", "a@x", now=2) > 0        # ... so a fresh IP still waits
    assert lim.retry_after("4.4.4.4", "b@x", now=2) == 0       # ... but another e-mail does not


def test_ip_lock_holds_across_emails():
    lim = make()
    for e in ("a@x", "b@x", "c@x"):
        lim.failure("1.1.1.1", e, now=0)
    assert lim.retry_after("1.1.1.1", "d@x", now=1) > 0


def test_global_circuit_breaker():
    lim = make()
    for i in range(10):
        lim.failure(f"10.0.0.{i}", f"u{i}@x", now=i)            # 10 different ips and e-mails
    assert lim.retry_after("9.9.9.9", "fresh@x", now=11) > 0    # everyone locked


def test_success_clears_counters():
    lim = make()
    lim.failure("1.1.1.1", "a@x", now=0)
    lim.failure("1.1.1.1", "a@x", now=1)
    lim.success("1.1.1.1", "a@x")
    assert lim.failure("1.1.1.1", "a@x", now=2) == 0


def test_email_is_case_insensitive():
    lim = make()
    lim.failure("1.1.1.1", "A@X", now=0)
    lim.failure("2.2.2.2", "a@x", now=0)
    assert lim.failure("3.3.3.3", "a@X ", now=1) == 60


def test_client_ip_prefers_cloudflare_header():
    assert client_ip({"Cf-Connecting-Ip": "203.0.113.7", "X-Forwarded-For": "10.0.0.1"}) == "203.0.113.7"
    assert client_ip({"X-Forwarded-For": "203.0.113.8, 10.0.0.1"}) == "203.0.113.8"
    assert client_ip({}, fallback="127.0.0.1") == "127.0.0.1"
    assert client_ip(None) == "unknown"


# ---------------------------------------------------------------- the question limiter (public deployments)

def test_question_limit_per_ip_and_window():
    from bqa.ratelimit import QuestionLimiter
    lim = QuestionLimiter(per_ip=2, window=3600)
    assert lim.acquire("1.1.1.1", now=0) == "" and lim.acquire("1.1.1.1", now=10) == ""
    refusal = lim.acquire("1.1.1.1", now=20)
    assert "2 questions per an hour" in refusal and "ask again in" in refusal
    assert lim.acquire("2.2.2.2", now=20) == ""                 # another visitor is unaffected
    assert lim.acquire("1.1.1.1", now=3601) == ""                # the first question has aged out


def test_question_limit_global_and_concurrent():
    from bqa.ratelimit import QuestionLimiter
    lim = QuestionLimiter(global_max=3, window=3600, concurrent=2)
    assert lim.acquire("a", now=0) == "" and lim.acquire("b", now=1) == ""
    assert "busy" in lim.acquire("c", now=2)                     # two in flight: the third waits, and is not counted
    lim.release()
    assert lim.acquire("c", now=3) == ""
    lim.release(); lim.release()
    assert "limit for everyone" in lim.acquire("d", now=4)       # three counted in the window
    assert lim.acquire("d", now=3700) == ""


def test_question_limiter_off_by_default():
    from bqa.ratelimit import QuestionLimiter
    lim = QuestionLimiter()
    assert all(lim.acquire("x", now=i) == "" for i in range(500))
