"""Budget Query Assistant — ask a budget database a question in plain language.

    streamlit run app.py
"""
from __future__ import annotations

import hmac
import json
import time
import uuid

import pandas as pd
import plotly.express as px
import streamlit as st
import streamlit.components.v1 as components

from bqa.config import settings
from bqa.export import to_xlsx
from bqa.query import Assistant, expand_choice
from bqa.public import js_string, public_message
from bqa.ratelimit import LoginLimiter, QuestionLimiter, client_ip
from bqa.session import COOKIE_NAME, load_or_create_secret, make_token, read_token
from bqa.config import ROOT
from bqa.schema import schema_cache_age

st.set_page_config(page_title=settings.app_title, page_icon="💬", layout="centered",
                   initial_sidebar_state=340)      # px: the guide in the sidebar reads better wide

# The two "New conversation" buttons are red: pressing one throws the conversation away.
# Streamlit tags each keyed element's container with st-key-<key>.
st.markdown(
    """<style>
    .st-key-new-conv-sidebar button, .st-key-new-conv-main button {
        background: #B3261E; border-color: #B3261E; color: #fff;
    }
    .st-key-new-conv-sidebar button:hover, .st-key-new-conv-main button:hover,
    .st-key-new-conv-sidebar button:focus, .st-key-new-conv-main button:focus {
        background: #8F1E18; border-color: #8F1E18; color: #fff;
    }
    </style>""",
    unsafe_allow_html=True)

# Fallbacks for a deployment that sets neither USER_GUIDE nor EXAMPLE_QUESTIONS (e.g. the demo
# database). A real deployment describes its own data in those files instead of in this code.
DEFAULT_EXAMPLES = [
    "Total budget by country",
    "Budget by module for Kenya",
    "How much goes to cost category 4. Health products - pharmaceutical products, by region?",
    "Budget for prevention",                       # → the assistant asks which 'prevention' is meant
    "What is the population of Uganda?",           # → cannot answer from these tables
]

DEFAULT_GUIDE = """#### What you can ask about

Budget lines, grouped and filtered however you like.

**Where** country · region
**What** module → intervention
**How** cost category → cost input
**When** implementation period

#### Worth knowing

- If a question could mean two things, you get one question back instead of a confident wrong answer.
- Questions the tables cannot answer are refused rather than guessed at.
"""

EXAMPLES = settings.examples() or DEFAULT_EXAMPLES


# ---------------------------------------------------------------- browser notifications
# Answers take 10-30s, so people switch tabs. These fire only when the tab is in the background;
# if they are watching the answer arrive, a desktop notification would just be noise.
#
# Both snippets reach through window.parent: the component runs in a same-origin srcdoc iframe,
# and creating the Notification in the top-level context is what makes clicking it focus the
# right tab. The API needs a secure context, so it is unavailable over plain http:// on a LAN
# address — the button below says so rather than failing silently.

def notification_permission_button() -> None:
    """Ask for notification permission from a real click inside the iframe. Safari (and Chrome,
    increasingly) ignore requestPermission() that is not driven by a user gesture."""
    components.html(
        """
        <button id="ask">Enable browser notifications</button>
        <div id="msg"></div>
        <style>
          #ask {font:inherit;font-size:.82rem;padding:.35rem .7rem;border:1px solid #c9ced4;
                border-radius:.4rem;background:#fff;cursor:pointer;width:100%;}
          #ask:hover {border-color:#0F6E8C;color:#0F6E8C;}
          #msg {font:inherit;font-size:.72rem;color:#5b6670;margin-top:.3rem;}
        </style>
        <script>
        (function () {
          const W = window.parent || window;
          const ask = document.getElementById("ask"), msg = document.getElementById("msg");
          function paint() {
            if (!W.isSecureContext || !("Notification" in W)) {
              ask.style.display = "none";
              msg.textContent = "Notifications need https:// or localhost — not available on this address.";
              return;
            }
            const p = W.Notification.permission;
            if (p === "granted") { ask.style.display = "none"; msg.textContent = "Notifications are on."; }
            else if (p === "denied") { ask.style.display = "none"; msg.textContent = "Blocked — re-allow them in your browser's site settings."; }
            else { ask.style.display = ""; msg.textContent = "Get told when an answer lands while you are in another tab."; }
          }
          ask.onclick = function () {
            W.Notification.requestPermission().then(function (p) {
              paint();
              if (p === "granted") new W.Notification("Budget Query Assistant", {body: "Notifications are on."});
            });
          };
          paint();
        })();
        </script>
        """,
        height=74)


def browser_notify(title: str, body: str) -> None:
    """Fire one desktop notification. No-op when permission was never granted or the tab is
    already in front."""
    components.html(
        f"""
        <script>
        (function () {{
          try {{
            const W = window.parent || window;
            if (!("Notification" in W) || W.Notification.permission !== "granted") return;
            const doc = W.document || document;
            if (!doc.hidden) return;                       // they are already looking at it
            const n = new W.Notification({js_string(title)},
                                        {{body: {js_string(body)}, tag: "bqa-answer", renotify: true}});
            n.onclick = function () {{ W.focus(); n.close(); }};
          }} catch (e) {{ /* notifications are a convenience, never a failure path */ }}
        }})();
        </script>
        """,
        height=0)


@st.cache_resource(show_spinner="Reading the schema…", ttl=3600)
def get_assistant() -> Assistant:
    """Rebuilt hourly so a schema refreshed by the scheduled job reaches a long-running app
    without a restart. Rebuilding is cheap — it reads the cache file, it does not introspect."""
    return Assistant(settings)


def _schema_age_text() -> str:
    age = schema_cache_age(settings)
    if age is None:
        return "not cached — read directly from the database"
    if age < 3600:
        return f"{age / 60:.0f} minutes ago"
    if age < 86400 * 2:
        return f"{age / 3600:.0f} hours ago"
    return f"{age / 86400:.0f} days ago"


assistant = get_assistant()
ok, health = assistant.provider.healthy()
messages = st.session_state.setdefault("messages", [])
# The conversation the model is given. It lives in this browser session, not on the shared
# Assistant, so every open tab talks to the model with its own history and nobody sees another
# user's turns. "New conversation" empties it — that is what starts a fresh conversation.
turns = st.session_state.setdefault("turns", [])
# Every turn is logged under the conversation it belongs to, so a later review can read a whole
# exchange in order — the question, the clarification, the correction — not isolated records.
st.session_state.setdefault("conversation_id", uuid.uuid4().hex[:12])

# ---------------------------------------------------------------- access control
@st.cache_resource
def get_login_limiter() -> LoginLimiter:
    """One limiter for the whole process, shared by every browser session."""
    return LoginLimiter(max_failures=settings.login_max_failures, window=settings.login_window,
                        lockout=settings.login_lockout, max_lockout=settings.login_max_lockout,
                        global_max_failures=settings.login_global_max_failures,
                        global_lockout=settings.login_global_lockout)


@st.cache_resource
def get_question_limiter() -> QuestionLimiter:
    """One limiter for the whole process: what a visitor, and everyone together, may ask of the model."""
    return QuestionLimiter(per_ip=settings.questions_per_ip, global_max=settings.questions_global,
                           window=settings.questions_window, concurrent=settings.max_concurrent_questions)


def refuse_question(question: str) -> str:
    """Why this question will not be sent to the model, or '' — in which case a slot has been
    taken and get_question_limiter().release() must be called once the answer is back."""
    if settings.max_question_chars and len(question) > settings.max_question_chars:
        return (f"That question is {len(question):,} characters long; the limit is "
                f"{settings.max_question_chars:,}. Please shorten it.")
    ip = client_ip(st.context.headers, fallback=st.context.ip_address or "")
    return get_question_limiter().acquire(ip)


def _wait_text(seconds: float) -> str:
    m = int(seconds // 60)
    return f"{m} minute{'s' if m != 1 else ''}" if m else f"{int(seconds) + 1} seconds"


@st.cache_resource
def get_session_secret() -> bytes:
    return load_or_create_secret(settings.session_secret, ROOT / "logs" / "session_secret")


def write_session_cookie(token: str | None) -> None:
    """Set (or, with None, delete) the session cookie on the real page.

    The component runs in a same-origin iframe, so it reaches the page's cookie jar through
    window.parent. Scoped to the app's own path so the other dashboards on the host never see
    it; Secure whenever the page is on https, so it is never sent in the clear."""
    from streamlit import config as st_config
    path = "/" + st_config.get_option("server.baseUrlPath").strip("/") if st_config.get_option("server.baseUrlPath") else "/"
    max_age = settings.session_days * 86400 if token else 0
    components.html(
        f"""
        <script>
        (function () {{
          try {{
            const W = window.parent || window;
            const secure = W.location.protocol === "https:" ? "; Secure" : "";
            W.document.cookie = {json.dumps(COOKIE_NAME)} + "=" + {json.dumps(token or "")} +
              "; Max-Age=" + {max_age} + "; Path=" + {json.dumps(path)} + "; SameSite=Lax" + secure;
          }} catch (e) {{ /* staying signed in is a convenience, never a failure path */ }}
        }})();
        </script>
        """,
        height=0)


allowed_emails = settings.load_allowed_emails()
# A refresh starts a new Streamlit session with empty state. Before asking anyone to sign in
# again, see whether the browser holds a valid session cookie. Checked once per session, and
# not after a sign-out in this session (the cookie is deleted then, but this session already
# read the old cookie jar when it connected).
if allowed_emails is not None and not st.session_state.get("user_email") \
        and not st.session_state.get("cookie_checked"):
    st.session_state["cookie_checked"] = True
    restored = read_token(get_session_secret(), st.context.cookies.get(COOKIE_NAME), allowed_emails)
    if restored:
        st.session_state["user_email"] = restored
user_email = st.session_state.get("user_email", "")
if allowed_emails is not None and not user_email:
    if st.session_state.pop("clear_cookie", False):
        write_session_cookie(None)
    limiter = get_login_limiter()
    ip = client_ip(st.context.headers, fallback=st.context.ip_address or "")
    st.title(settings.app_title)
    with st.form("signin"):
        email = st.text_input("Your e-mail address")
        code = st.text_input("Access code", type="password") if settings.access_code else ""
        if st.form_submit_button("Sign in", type="primary"):
            email = email.strip().lower()
            wait = limiter.retry_after(ip, email)
            if wait > 0:
                st.error(f"Too many failed attempts. Try again in {_wait_text(wait)}.")
            else:
                # One message for both a wrong e-mail and a wrong code: saying which was wrong
                # would let anyone probe the access list one address at a time.
                listed = email in allowed_emails
                code_ok = (not settings.access_code) or hmac.compare_digest(code, settings.access_code)
                if listed and code_ok:
                    limiter.success(ip, email)
                    get_assistant().audit.login(email, ip, True)
                    st.session_state["user_email"] = email
                    # Written after the rerun, once the page is past this form (see below):
                    # a component emitted here would be torn down before it ran.
                    st.session_state["set_cookie"] = make_token(
                        get_session_secret(), email, settings.session_days * 86400)
                    st.rerun()
                else:
                    lock = limiter.failure(ip, email)
                    get_assistant().audit.login(email, ip, False, "not listed" if not listed else "wrong code")
                    time.sleep(1.5)          # every wrong guess costs the guesser a moment
                    if lock > 0:
                        st.error(f"Wrong e-mail address or access code. Too many failed attempts: "
                                 f"sign-in is blocked for {_wait_text(lock)}.")
                    else:
                        st.error("Wrong e-mail address or access code.")
    st.caption("Access is limited to a list of invited users. Questions are logged with your e-mail address.")
    st.stop()

_new_cookie = st.session_state.pop("set_cookie", None)
if _new_cookie:
    write_session_cookie(_new_cookie)

def start_new_conversation() -> None:
    """Forget the conversation (and any half-answered clarification) and start over. The user
    stays signed in; only the chat is reset."""
    st.session_state["messages"] = []
    st.session_state["turns"] = []
    st.session_state["conversation_id"] = uuid.uuid4().hex[:12]
    st.session_state.pop("pending_clarification", None)
    st.session_state.pop("pending_options", None)
    st.rerun()


# ---------------------------------------------------------------- sidebar
with st.sidebar:
    st.title(settings.app_title)
    st.markdown("🟢 Ready — ask a question in plain English." if ok else
                "🔴 The assistant cannot reach its model right now, so questions will fail.")
    st.markdown(settings.user_guide or DEFAULT_GUIDE)
    st.divider()
    # The summary is a second model call over the result rows — the one place data reaches the
    # model. A public deployment that has it switched off does not offer it either.
    want_obs = (st.toggle("Add a short written summary of the numbers", value=settings.observations,
                          help="A few plain-language observations under each table. Slower, because it "
                               "asks the model a second time.")
                if settings.show_sql or settings.observations else False)
    remember = st.toggle("Remember this conversation", value=settings.remember_conversation,
                         help="Earlier questions are sent along with each new one, so follow-ups like "
                              "'now split that by module' work.")
    notification_permission_button()
    # One accounting per rerun, for the gauge above the input.
    sent_turns = turns if remember else []
    usage = assistant.context_usage(sent_turns)
    full = usage["used"] + usage["reserve"] >= usage["limit"]
    # If even an empty conversation does not fit, the window setting is wrong, not the conversation:
    # blocking would be pointless (clearing changes nothing), so warn about CONTEXT_WINDOW instead.
    misconfigured = full and not sent_turns
    full = full and not misconfigured
    if st.button("New conversation", width="stretch", key="new-conv-sidebar",
                 help="Starts over. The current conversation is lost — download any tables you want to keep first."):
        start_new_conversation()
    if user_email:
        st.caption(f"Signed in as {user_email}")
        if st.button("Sign out", width="stretch"):
            st.session_state.clear()
            st.session_state["cookie_checked"] = True     # do not restore from the stale cookie jar
            st.session_state["clear_cookie"] = True       # the sign-in page deletes the cookie
            st.rerun()
    if settings.show_sql:
        with st.expander("Technical details"):
            st.markdown(
                f"**Model:** `{settings.model}` via `{settings.provider}`  \n"
                f"**SQL dialect:** `{settings.dialect}`  \n"
                f"**Guard:** one SELECT · no `*` · allow-listed tables · max {settings.row_limit:,} rows  \n"
                f"**Context window:** {usage['limit']:,} tokens  \n"
                f"**Schema read:** {_schema_age_text()}  \n"
                f"**Status:** {health}")
            st.caption("The model proposes a query, a guard checks and re-renders it, the database answers. "
                       "Every question, query and outcome goes to the audit log.")


def render_result(out, idx: int) -> None:
    """Everything an answer bubble holds: count, SQL, table/chart, download, observations, feedback."""
    df: pd.DataFrame = out.df
    note = (f" · corrected {out.repairs} time{'s' if out.repairs > 1 else ''} by the guard"
            if out.repairs and settings.show_sql else "")
    st.markdown(f"**{len(df):,} rows**" + note)
    if out.note:
        # The reading the model made, in its own words — the one place a confident answer says
        # what it assumed, so a wrong assumption is visible and can be corrected next question.
        st.info(out.note, icon="ℹ️")
    if out.row_limit_hit:
        # The rows below are real, but they are not the whole answer — and the share column is
        # computed on what came back, so on a truncated result it describes only these rows.
        share = (f" Because of this, **'% of total' is each row's share of these {len(df):,} rows "
                 "only — not of the full result** — so those percentages read too high."
                 if "% of total" in df.columns else "")
        st.warning(
            f"**Cut off at the {settings.row_limit:,}-row limit — this answer is incomplete.** "
            f"The query matched at least {len(df):,} rows and the guard returns no more than "
            f"{settings.row_limit:,}, so you are seeing the first {len(df):,} in the query's own "
            f"sort order (an arbitrary subset if it has no ORDER BY). Totals below are therefore "
            f"NOT the full totals.{share} Narrow the question — filter to a country, component or "
            "year, or group at a higher level — for a complete result.")
    numeric = df.select_dtypes(include="number").columns.tolist()
    value_cols = [c for c in numeric if c != "% of total"]
    label_cols = [c for c in df.columns if c not in numeric]
    fmt = {c: "{:,.0f}" for c in value_cols}
    if "% of total" in df.columns:
        fmt["% of total"] = "{:.1%}"
    chartable = len(value_cols) == 1 and label_cols and 1 < len(df) <= 40
    tabs = st.tabs(["Table"] + (["Chart"] if chartable else []) + (["SQL"] if settings.show_sql else []))
    with tabs[0]:
        st.dataframe(df.style.format(fmt), width="stretch", hide_index=True, key=f"df-{idx}")
    if chartable:
        with tabs[1]:
            try:
                # A NULL in a label column (e.g. a budget line with no component) must still
                # label its bar: pandas keeps missing values missing through astype(str).
                labels = df[label_cols].fillna("(blank)").astype(str)
                label = labels.iloc[:, 0] if len(label_cols) == 1 else labels.agg(" · ".join, axis=1)
                fig = px.bar(df, x=value_cols[0], y=label.values, orientation="h", text_auto=".3s")
                fig.update_layout(yaxis={"categoryorder": "total ascending", "title": ""}, xaxis_title=value_cols[0],
                                  margin=dict(l=10, r=10, t=10, b=10), height=max(300, 28 * len(df)))
                st.plotly_chart(fig, width="stretch", key=f"chart-{idx}")
            except Exception as e:  # noqa: BLE001 - a chart is a bonus; the table is the answer
                st.info(f"No chart for this result ({type(e).__name__}). The table and download still work.")
    if settings.show_sql:
        with tabs[-1]:
            st.code(out.sql, language="sql")
    if out.observations:
        st.markdown(out.observations.replace("\n", "  \n"))
    dl, fb, cm = st.columns([4, 2, 1], vertical_alignment="center")
    dl.download_button("Download Excel", data=to_xlsx(df), file_name="query_results.xlsx",
                       mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                       key=f"dl-{idx}", width="stretch")
    with fb:
        vote = st.feedback("thumbs", key=f"fb-{idx}")
        if vote is not None and st.session_state.get(f"fb-logged-{idx}") != vote:
            st.session_state[f"fb-logged-{idx}"] = vote
            assistant.audit.feedback(out.question, out.sql, vote == 1, "", user=st.session_state.get("user_email", ""),
                                     conversation_id=out.conversation_id)
            st.toast("Thanks — logged." if vote == 1 else "Logged — the query is kept for review.")
    with cm, st.popover("💬"):
        note = st.text_area("What should we know about this answer?", key=f"cm-{idx}",
                            placeholder="Wrong filter, missing rows, confusing wording…")
        if st.button("Send feedback", key=f"cm-send-{idx}") and note.strip():
            assistant.audit.feedback(out.question, out.sql, None, note.strip(),
                                     user=st.session_state.get("user_email", ""), conversation_id=out.conversation_id)
            st.toast("Feedback logged — thank you.")


def render_options(options: list[str], multi: bool, idx: int) -> None:
    """The options of the clarifying question that is waiting for its answer, as a control.

    Options that contradict each other (a module or an intervention, one cycle or another) are a
    pick-one list: choosing one sends it. Options that can be combined (interventions to include)
    are tick-boxes with a Send button. Either way the pick is queued as the next message and goes
    down the same path as a typed answer — and typing "2" or "1, 3" below still works, as does
    an answer in the user's own words. No st.rerun() here: the input section further down picks
    the queued answer up in this same run, and a rerun from inside a still-selected radio would
    queue it again.
    """
    labels = [f"{n}. {opt}" for n, opt in enumerate(options, 1)]
    if not multi:
        choice = st.radio("Pick one", labels, index=None, key=f"opt-{idx}", label_visibility="collapsed")
        if choice is not None:
            st.session_state["queued_question"] = options[labels.index(choice)]
        st.caption("Pick one — or type its number, or an answer in your own words, below.")
        return
    ticked = [opt for n, opt in enumerate(options, 1) if st.checkbox(labels[n - 1], key=f"opt-{idx}-{n}")]
    if st.button("Send selection", key=f"opt-send-{idx}", type="primary", disabled=not ticked):
        st.session_state["queued_question"] = "; ".join(ticked)
    st.caption("Tick all that apply and send — or type the numbers below (e.g. 1, 3), or answer in your own words.")


def render_message(msg: dict, idx: int, live: bool = False) -> None:
    """One chat bubble. 'live' marks the clarifying question still waiting for its answer: its
    options are a control then, and a plain numbered list once it has been answered."""
    with st.chat_message(msg["role"]):
        if msg.get("text"):
            st.markdown(msg["text"])
        options = msg.get("options") or []
        if options and live:
            render_options(options, bool(msg.get("multi")), idx)
        elif options:
            st.markdown("\n".join(f"{n}. {opt}" for n, opt in enumerate(options, 1)))
        out = msg.get("outcome")
        if out is not None and out.status == "sql":
            render_result(out, idx)
        elif out is not None and out.status in ("rejected", "error") and out.sql and settings.show_sql:
            with st.expander("The SQL that did not run"):
                st.code(out.sql, language="sql")


def render_context_meter(usage: dict, full: bool) -> None:
    """A slim gauge above the input: how much of the model's context this conversation now fills."""
    share = min(1.0, (usage["used"] + usage["reserve"]) / max(usage["limit"], 1))
    colour = "#B3261E" if full else ("#B26B00" if share > 0.75 else "#0F6E8C")
    label = (f"Context {share * 100:.0f}% · ~{usage['used']:,} of {usage['limit']:,} tokens · "
             f"{usage['turns']} turn{'s' if usage['turns'] != 1 else ''} remembered")
    st.markdown(
        f'''<div title="Schema and rules {usage["base"]:,} + conversation {usage["history"]:,} + '''
        f'''{usage["reserve"]:,} kept free for the answer" style="display:flex;align-items:center;'''
        f'''gap:.55rem;margin:.4rem 0 -.2rem 0;font-size:.76rem;color:#5b6670;">'''
        f'''<div style="flex:1;height:6px;border-radius:3px;background:#e6e9ec;overflow:hidden;">'''
        f'''<div style="width:{share * 100:.1f}%;height:100%;background:{colour};border-radius:3px;">'''
        f'''</div></div><span style="white-space:nowrap;">{label}</span></div>''',
        unsafe_allow_html=True)


# ---------------------------------------------------------------- conversation
if not messages:
    st.markdown("##### Ask the budget database anything — or try one:")
    for ex in EXAMPLES:
        if st.button(ex, key=f"ex-{ex}", width="stretch"):
            st.session_state["queued_question"] = ex
            st.rerun()

for i, msg in enumerate(messages):
    render_message(msg, i, live=(i == len(messages) - 1 and "pending_clarification" in st.session_state))

if messages:
    _, col = st.columns([3, 1])
    if col.button("New conversation", key="new-conv-main", width="stretch",
                  help="Starts over. The current conversation is lost — download any tables you want to keep first."):
        start_new_conversation()

if settings.show_sql or full or misconfigured:        # a public visitor only hears about the context when it is full
    render_context_meter(usage, full or misconfigured)
if misconfigured:
    st.warning(f"The schema and rules alone need ~{usage['base']:,} tokens, more than the "
               f"{usage['limit']:,}-token window in CONTEXT_WINDOW. Set CONTEXT_WINDOW to the context "
               "length your model server is actually loaded with — until then the gauge is wrong.")
if full:
    st.error("This conversation has filled the model's context window. Start a new one to keep asking — "
             "the answers so far stay on screen until you do.")
    if st.button("Start a new conversation", type="primary", width="stretch"):
        start_new_conversation()

prompt = st.chat_input("Ask about the budget data…" if not full else "Context full — clear the conversation to continue",
                       disabled=full)
if prompt is None and not full:
    prompt = st.session_state.pop("queued_question", None)

if prompt:
    refusal = refuse_question(prompt)          # length cap and question limits; '' = a slot is ours
    if refusal:
        # Nothing is popped or sent: a pending clarification stays pending, the model is not called.
        messages.append({"role": "user", "text": prompt[:300] + ("…" if len(prompt) > 300 else "")})
        messages.append({"role": "assistant", "text": f"⚠️ {refusal}"})
        st.rerun()
    pending = st.session_state.pop("pending_clarification", None)
    pending_options = st.session_state.pop("pending_options", None) or []
    if pending and pending_options:
        # "2" or "1, 3" answers the clarifying question by number: send — and show — the pick itself.
        prompt = expand_choice(prompt, pending_options) or prompt
    messages.append({"role": "user", "text": prompt})
    with st.chat_message("user"):
        st.markdown(prompt)
    with st.chat_message("assistant"):
        status = st.status("🧠 Thinking — the model is writing a query…", expanded=False)
        t0 = time.time()

        def on_event(phase: str, payload: str) -> None:
            if phase == "repairing":
                # Say what was wrong rather than just spinning: the guard caught the model
                # inventing something, and is making it try again.
                status.update(label=(f"↩️ {payload} — asking the model to correct it…" if settings.show_sql
                                     else "↩️ Checking the answer against the data…"), expanded=settings.show_sql)
            if phase == "executing":
                status.update(label=f"⚙️ Model done in {time.time() - t0:.0f}s — running the query on the database…",
                              expanded=settings.show_sql)
                if settings.show_sql:
                    with status:
                        st.code(payload, language="sql")

        where = {"conversation_id": st.session_state["conversation_id"], "turn_no": len(turns) + 1}
        try:
            if pending:
                out = assistant.ask(pending, clarification=prompt, with_observations=want_obs,
                                    user_email=user_email, on_event=on_event, turns=sent_turns, **where)
            else:
                out = assistant.ask(prompt, with_observations=want_obs, user_email=user_email,
                                    on_event=on_event, turns=sent_turns, **where)
        finally:
            get_question_limiter().release()   # the slot refuse_question() took
        if out.turn is not None:
            turns.append(out.turn)
        done = {"sql": "✅ Answered", "clarify": "❓ Needs one detail", "cannot": "🚫 Cannot answer",
                "rejected": "🛑 Query rejected by the guard", "error": "⚠️ Failed"}.get(out.status, out.status)
        status.update(label=f"{done} in {time.time() - t0:.0f}s", state="error" if out.status in ("rejected", "error") else "complete",
                      expanded=False)
    # Queue the notification rather than firing it here: st.rerun() below tears this block down
    # before the component would run. It is fired once, after the rerun, at the end of the script.
    took = f"{time.time() - t0:.0f}s"
    if out.status == "clarify":
        st.session_state["pending_clarification"] = out.question
        st.session_state["pending_options"] = out.options
        messages.append({"role": "assistant", "text": out.message, "options": out.options, "multi": out.multi})
        st.session_state["notify"] = ("❓ One detail needed", out.question or out.message)
    elif out.status == "cannot":
        messages.append({"role": "assistant", "text": f"**Cannot answer from these tables.** {out.message}"})
        st.session_state["notify"] = ("🚫 Cannot answer", out.message)
    elif out.status in ("rejected", "error"):
        shown = out.message if settings.show_sql else public_message(out.status, out.message)
        messages.append({"role": "assistant", "text": f"⚠️ {shown}", "outcome": out if out.sql else None})
        st.session_state["notify"] = ("⚠️ That question did not run", shown)
    else:
        messages.append({"role": "assistant", "outcome": out})
        rows = len(out.df) if out.df is not None else 0
        cut = " (cut off at the row limit)" if out.row_limit_hit else ""
        st.session_state["notify"] = (f"✅ Answer ready — {rows:,} rows{cut}", f"{out.question}  ·  {took}")
    st.rerun()

if allowed_emails is not None:
    st.caption("Every question, query, outcome and piece of feedback is logged **with your e-mail address** — "
               "this is a testing environment and we may follow up with you about what you tried. "
               "Only SELECT statements are permitted against the database.")
else:
    st.caption("Every question, query and outcome is written to the audit log (no user identifiers). "
               "Only SELECT statements are permitted against the database." if settings.show_sql else
               "Questions and answers are logged without anything that identifies you, to improve the demo.")

# Fired after the rerun that follows an answer, so the component actually mounts. Popping it
# means one notification per answer, never a repeat on later reruns.
_pending = st.session_state.pop("notify", None)
if _pending:
    browser_notify(*_pending)
