"""Model providers. All of them expose one method: complete(system, user, history=...) -> str.

- OllamaProvider          local model, nothing leaves the machine (default)
- OpenAICompatibleProvider any /v1/chat/completions endpoint (OpenAI, Azure OpenAI, Gemini's compat endpoint, vLLM, ...)
- MockProvider            deterministic answers for tests and for a no-model demo
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, field

import requests

from .config import Settings


@dataclass
class Decision:
    status: str                 # sql | clarify | cannot
    sql: str = ""
    question: str = ""
    reason: str = ""
    note: str = ""              # sql only: an assumption the model made, shown with the answer
    options: list[str] = field(default_factory=list)   # clarify only: answers the user can pick by number
    multi: bool = False         # clarify only: several options may be combined (else they contradict)
    raw: str = ""


Message = dict          # {"role": "user" | "assistant", "content": str}


def build_messages(system: str, user: str, history: list[Message] | None = None) -> list[Message]:
    """system + the earlier turns of this conversation + the question being asked now."""
    return [{"role": "system", "content": system}, *(history or []), {"role": "user", "content": user}]


def estimate_tokens(text: str, chars_per_token: float = 4.0) -> int:
    """Approximate token count. The served model's tokenizer is not available to the app, so
    this is a characters-per-token heuristic — good enough to drive a fullness meter, not exact.
    Calibrate with CHARS_PER_TOKEN if your model runs consistently over or under."""
    if not text:
        return 0
    return max(1, round(len(text) / max(chars_per_token, 1.0)))


def estimate_messages(messages: list[Message], chars_per_token: float = 4.0) -> int:
    """Same heuristic over a message list, plus a few tokens of per-message role framing."""
    return sum(estimate_tokens(m.get("content", ""), chars_per_token) + 4 for m in messages)


class LLMProvider:
    name = "base"

    def complete(self, system: str, user: str, *, json_mode: bool = False,
                 history: list[Message] | None = None) -> str:  # pragma: no cover - interface
        raise NotImplementedError

    def healthy(self) -> tuple[bool, str]:
        return True, "ok"


class OllamaProvider(LLMProvider):
    name = "ollama"

    def __init__(self, base_url: str, model: str, timeout: int = 120, max_tokens: int = 2000):
        self.base_url, self.model, self.timeout, self.max_tokens = base_url.rstrip("/"), model, timeout, max_tokens

    def complete(self, system: str, user: str, *, json_mode: bool = False,
                 history: list[Message] | None = None) -> str:
        payload = {
            "model": self.model,
            "messages": build_messages(system, user, history),
            "stream": False,
            "think": False,  # hybrid-thinking models (qwen3.x) emit a <think> block unless told not to
            "options": {"temperature": 0, "num_predict": self.max_tokens},
        }
        if json_mode:
            payload["format"] = "json"
        r = requests.post(f"{self.base_url}/api/chat", json=payload, timeout=self.timeout)
        if r.status_code == 400:  # model without a thinking mode rejects the key; retry without it
            payload.pop("think")
            r = requests.post(f"{self.base_url}/api/chat", json=payload, timeout=self.timeout)
        r.raise_for_status()
        return r.json()["message"]["content"]

    def healthy(self) -> tuple[bool, str]:
        try:
            r = requests.get(f"{self.base_url}/api/tags", timeout=3)
            r.raise_for_status()
            names = [m["name"] for m in r.json().get("models", [])]
            if self.model in names or any(n.split(":")[0] == self.model.split(":")[0] for n in names):
                return True, f"Ollama reachable, model '{self.model}' available"
            return False, f"Ollama reachable but model '{self.model}' not pulled (ollama pull {self.model})"
        except Exception as e:  # noqa: BLE001
            return False, f"Ollama not reachable at {self.base_url} ({e.__class__.__name__})"


class OpenAICompatibleProvider(LLMProvider):
    name = "openai"

    def __init__(self, base_url: str, api_key: str, model: str, timeout: int = 120, max_tokens: int = 2000,
                 reasoning_effort: str = ""):
        self.base_url, self.api_key, self.model, self.timeout = base_url.rstrip("/"), api_key, model, timeout
        self.max_tokens, self.reasoning_effort = max_tokens, reasoning_effort

    def complete(self, system: str, user: str, *, json_mode: bool = False,
                 history: list[Message] | None = None) -> str:
        payload = {
            "model": self.model,
            "messages": build_messages(system, user, history),
            "temperature": 0,
            "max_tokens": self.max_tokens,
        }
        if self.reasoning_effort:
            payload["reasoning_effort"] = self.reasoning_effort
        if json_mode:
            payload["response_format"] = {"type": "json_object"}
        headers = {"Authorization": f"Bearer {self.api_key}"}

        def post() -> requests.Response:
            return requests.post(f"{self.base_url}/chat/completions", json=payload,
                                 timeout=self.timeout, headers=headers)

        r = post()
        # A rejected optional parameter is named in the 400 body, whose shape varies by server
        # ({"error": {"param": ...}} on OpenAI-style servers, a bare {"error": "..."} string on
        # LM Studio), so fall back to looking for the parameter name in the raw text. Servers
        # also disagree on how to spell "do not think": current LM Studio builds take "none",
        # older gemma builds took "off". A rejected reasoning_effort is RE-SPELLED, never
        # dropped — dropping it restores the model's default of thinking ON, which is exactly
        # what runs to the token cap and comes back with an empty answer.
        for _ in range(3):
            if r.status_code != 400:
                break
            try:
                err = r.json().get("error")
                named = err.get("param") if isinstance(err, dict) else None
            except ValueError:
                named = None
            bad = named or next((k for k in ("response_format", "reasoning_effort") if k in r.text), None)
            if bad == "reasoning_effort" and "reasoning_effort" in payload:
                nxt = next((s for s in ("none", "off") if s != payload["reasoning_effort"]), None)
                if nxt is None:
                    break
                payload["reasoning_effort"] = nxt
            elif "response_format" in payload:   # this server wants another JSON-mode shape
                payload.pop("response_format")
            else:
                break
            r = post()
        r.raise_for_status()
        content = r.json()["choices"][0]["message"]["content"]
        # An empty answer means reasoning ran to the token cap without ever replying. Retry
        # with thinking off, trying each spelling this server has not already been given.
        for spelling in ("none", "off"):
            if (content or "").strip() or payload.get("reasoning_effort") == spelling:
                break
            payload["reasoning_effort"] = spelling
            r = post()
            if r.status_code == 400:          # this server rejects that spelling; try the next
                continue
            r.raise_for_status()
            content = r.json()["choices"][0]["message"]["content"]
        return content

    def healthy(self) -> tuple[bool, str]:
        local = any(h in self.base_url for h in ("localhost", "127.0.0.1"))
        if not self.api_key and not local:
            return False, "OPENAI_API_KEY is not set"
        where = "local endpoint (LM Studio / vLLM / ...)" if local else self.base_url
        return True, f"API endpoint configured ({where}), model '{self.model}'"


class MockProvider(LLMProvider):
    """Deterministic stand-in. Good enough to exercise every path of the app without a model."""
    name = "mock"

    COUNTRIES = ["Kenya", "Uganda", "Nigeria", "Mozambique", "Ethiopia", "Malawi", "Zambia", "Tanzania",
                 "Ghana", "Cameroon", "Senegal", "Ukraine", "Viet Nam", "Indonesia", "Philippines", "Peru"]

    def complete(self, system: str, user: str, *, json_mode: bool = False,
                 history: list[Message] | None = None) -> str:
        q = user.lower()
        if "observations" in system.lower() and "results" in q:
            return "• The largest line dominates the total.\n• The remaining rows are of comparable size."
        if any(w in q for w in ("weather", "population of", "who is", "salary of")):
            return json.dumps({"status": "cannot", "reason": "The tables only hold budget lines and geography."})
        if "compare" in q and not any(c.lower() in q for c in self.COUNTRIES):
            return json.dumps({"status": "clarify", "question": "Which countries should the comparison cover?",
                               "options": ["Kenya", "Uganda", "Nigeria", "Mozambique"], "multi": True})
        if "prevention" in q and "for key populations" not in q and "clarification" not in q:
            return json.dumps({"status": "clarify",
                               "question": "Do you mean the 'Prevention programs for key populations' module, or all interventions with 'prevention' in the name?",
                               "options": ["The module 'Prevention programs for key populations'",
                                           "All interventions with 'prevention' in the name"],
                               "multi": False})
        dims = []
        if "region" in q:
            dims.append(("g.region", "region"))
        if "module" in q:
            dims.append(("b.module", "module"))
        if "cost category" in q or "cost categories" in q:
            dims.append(("b.cost_category", "cost_category"))
        if "period" in q:
            dims.append(("b.implementation_period", "implementation_period"))
        if not dims:
            dims.append(("b.country", "country"))
        where = []
        for c in self.COUNTRIES:
            if c.lower() in q:
                where.append(f"b.country = '{c}'")
        join = " LEFT JOIN geography g ON b.country = g.country" if any(d[0].startswith("g.") for d in dims) else ""
        select = ", ".join(f"{a} AS {b}" if a.split('.')[1] != b else a for a, b in dims)
        group = ", ".join(a for a, _ in dims)
        sql = f"SELECT {select}, SUM(b.amount_usd) AS total_amount FROM budget_lines b{join}"
        if where:
            sql += " WHERE (" + " OR ".join(where) + ")"
        sql += f" GROUP BY {group} ORDER BY total_amount DESC"
        return json.dumps({"status": "sql", "sql": sql})

    def healthy(self) -> tuple[bool, str]:
        return True, "mock provider (no model — canned answers for demos and tests)"


def get_provider(s: Settings) -> LLMProvider:
    if s.provider == "ollama":
        return OllamaProvider(s.ollama_url, s.model, timeout=s.llm_timeout, max_tokens=s.llm_max_tokens)
    if s.provider == "openai":
        return OpenAICompatibleProvider(s.openai_base_url, s.openai_api_key, s.model, timeout=s.llm_timeout,
                                        max_tokens=s.llm_max_tokens, reasoning_effort=s.llm_reasoning_effort)
    if s.provider == "mock":
        return MockProvider()
    raise ValueError(f"Unknown LLM_PROVIDER '{s.provider}' (use ollama, openai or mock)")


_FENCE = re.compile(r"```(?:json|sql)?\s*(.*?)```", re.S)
_THINK = re.compile(r"<think>.*?</think>\s*", re.S)


_ESCAPE = re.compile(r'\\(["\\/bfnrtu])|\\')


def _drop_stray_backslashes(text: str) -> str:
    """Keep every valid JSON escape, drop every other backslash."""
    return _ESCAPE.sub(lambda m: m.group(0) if m.group(1) else "", text)


def _first_object(text: str) -> dict | None:
    """The first JSON object in the text, read as leniently as a model's output requires.

    The FIRST complete object wins: a model that changes its mind while writing can close the
    object early and append more keys or prose after it ('..."} , "note": "..."}'), and an object
    that parses is worth more than the tail that does not — a messy query inside it still goes
    through the guard, which sends it back for repair. Two ways a good answer can still fail to
    parse are forgiven: a raw newline inside the SQL string (strict=False), and a stray backslash
    that is no JSON escape (')\\ AS gc7' once cost a correct answer) — SQL has no use for
    backslashes, so dropping the stray ones changes nothing the database will see.
    """
    start = text.find("{")
    if start == -1:
        return None
    candidates = [text[start:]]
    cleaned = _drop_stray_backslashes(text[start:])
    if cleaned != candidates[0]:
        candidates.append(cleaned)
    for strict in (True, False):
        for cand in candidates:
            try:
                obj, _ = json.JSONDecoder(strict=strict).raw_decode(cand)
                return obj
            except json.JSONDecodeError:
                pass
    end = text.rfind("}")                  # a stray '{' before the object: try the outermost braces
    if end > start:
        try:
            return json.loads(cleaned[:end - start + 1] if len(cleaned) == len(text) - start else text[start:end + 1],
                              strict=False)
        except json.JSONDecodeError:
            return None
    return None


def parse_decision(text: str) -> Decision:
    """Tolerant parser: JSON object first; then the legacy prefix protocol; then bare SQL."""
    raw = _THINK.sub("", text).strip()  # belt and braces: drop a <think> block if one slipped through
    m = _FENCE.search(raw)
    body = m.group(1).strip() if m else raw
    obj = _first_object(body)
    if isinstance(obj, dict):
        status = str(obj.get("status", "")).lower()
        if status in ("sql", "clarify", "cannot"):
            raw_options = obj.get("options") if status == "clarify" else None
            options = ([str(o).strip() for o in raw_options if str(o).strip()][:8]
                       if isinstance(raw_options, list) else [])
            return Decision(status=status, sql=str(obj.get("sql", "")).strip(),
                            question=str(obj.get("question", "")).strip(),
                            reason=str(obj.get("reason", "")).strip(),
                            note=str(obj.get("note") or "").strip() if status == "sql" else "",
                            options=options, multi=bool(raw_options) and bool(obj.get("multi")), raw=raw)
    upper = body.upper()
    if upper.startswith("CLARIFICATION_NEEDED"):
        return Decision(status="clarify", question=body.split(":", 1)[-1].strip(), raw=raw)
    if "CANNOT_ANSWER" in upper:
        return Decision(status="cannot", reason="The model could not answer this from the available tables.", raw=raw)
    if upper.lstrip().startswith(("SELECT", "WITH")):
        return Decision(status="sql", sql=body.strip().rstrip(";"), raw=raw)
    return Decision(status="cannot", reason="The model returned an unusable answer.", raw=raw)
