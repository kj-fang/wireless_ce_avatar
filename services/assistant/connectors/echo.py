"""
Echo connector: Intel's wireless knowledge base as something the assistant
can ask.

Echo (services/handsfree/echo_client) answers grounded documentation
questions over MCP. One question takes about a minute, so answers are kept
for a few hours: asking the same thing twice in a morning costs nothing.

Everything Echo returns is Intel-internal engineering information. The
connector is marked `internal`, which labels the assistant's answer on the
page; nothing here ever writes an Echo answer into a customer reply.
"""

from __future__ import annotations

import hashlib
import threading
import time

from services.assistant.registry import Connector, Ctx, object_schema

ANSWER_CHARS = 5500            # leaves room for the JSON around it (registry cap 6000)
CACHE_TTL_S = 6 * 3600
CACHE_MAX = 64
ECHO_TIMEOUT_S = 100           # echo_client's own limit for one question
QUESTION_CHARS = 4000

_cache: dict[str, tuple[float, str]] = {}
_cache_lock = threading.Lock()


def _ask(question: str) -> str:
    """The one place Echo is actually called (tests replace this)."""
    from services.handsfree.echo_client import ask_echo_kb
    return ask_echo_kb(question, timeout=ECHO_TIMEOUT_S)


def _key(question: str) -> str:
    return hashlib.sha256(" ".join(question.split()).casefold().encode("utf-8")).hexdigest()


def cached_answer(question: str) -> str:
    """The stored answer to this question, or "" when there is none (or it
    is older than CACHE_TTL_S)."""
    with _cache_lock:
        hit = _cache.get(_key(question))
        if hit and time.time() - hit[0] <= CACHE_TTL_S:
            return hit[1]
    return ""


def remember(question: str, answer: str) -> None:
    with _cache_lock:
        if len(_cache) >= CACHE_MAX:
            for old in sorted(_cache, key=lambda k: _cache[k][0])[: CACHE_MAX // 4]:
                _cache.pop(old, None)
        _cache[_key(question)] = (time.time(), answer)


def clear_cache() -> None:
    with _cache_lock:
        _cache.clear()


def ask(question: str, ctx: Ctx) -> dict:
    """Ask Echo one question; the shared path of every Echo connector.

    {"internal": True, "source": "Echo KB", "answer", "cached", "elapsed_s"}
    or {"error": ...} when Echo cannot be reached or has no usable answer."""
    question = str(question or "").strip()[:QUESTION_CHARS]
    if not question:
        return {"error": "empty question"}
    answer = cached_answer(question)
    if answer:
        return _result(answer, cached=True, elapsed=0.0)
    ctx.emit("⏳ Asking Echo (the knowledge base usually needs about a minute) ...")
    started = time.monotonic()
    try:
        answer = str(_ask(question) or "").strip()
    except Exception as e:           # EchoUnavailable, or the mcp package missing
        return {"error": f"Echo is not available: {e}", "source": "Echo KB"}
    if not answer:
        return {"error": "Echo returned an empty answer", "source": "Echo KB"}
    remember(question, answer)
    return _result(answer, cached=False, elapsed=time.monotonic() - started)


def _result(answer: str, *, cached: bool, elapsed: float) -> dict:
    cut = len(answer) > ANSWER_CHARS
    return {"internal": True, "source": "Echo KB",
            "note": "Intel-internal. Do not forward to customers.",
            "answer": answer[:ANSWER_CHARS] + (" ...[answer cut]" if cut else ""),
            "cached": cached, "elapsed_s": round(elapsed, 1),
            "summary": f"an Echo answer of {len(answer)} characters"
                       + (" (kept from an earlier question)" if cached else f" in {elapsed:.0f} s")}


def _run_ask_echo(args: dict, ctx: Ctx) -> dict:
    return ask(args.get("question", ""), ctx)


def _run_about_case(args: dict, ctx: Ctx) -> dict:
    """Echo's view on one case. Beacon already asks Echo "what next?" while
    it analyses a case; with no question of the user's own, that stored
    answer is returned (no second minute of waiting). Otherwise the question
    is built from Beacon's understanding of the case and asked now."""
    from services.assistant.connectors import cases
    case_nbr = cases._normalise(args.get("case_nbr"))
    if not case_nbr:
        return {"error": f"{args.get('case_nbr')!r} is not an IPS case number"}
    question = str(args.get("question") or "").strip()
    record, note = None, ""
    try:
        record = cases.latest_draft(case_nbr)
    except Exception as e:
        note = f"Beacon's analysis could not be read ({type(e).__name__}: {str(e)[:120]})."
    analysis = (record or {}).get("analysis") or {}
    advice = analysis.get("echo_advice") or {}
    if not question and str(advice.get("answer") or "").strip():
        out = _result(str(advice["answer"]).strip(), cached=True, elapsed=0.0)
        out.update(case=case_nbr, origin="asked by Beacon while it analysed the case",
                   summary=f"Echo's stored advice for case {case_nbr}")
        return out

    from services.handsfree.echo_client import build_next_steps_question
    if analysis:
        built = build_next_steps_question(
            subject=str(record.get("subject") or analysis.get("subject") or ""),
            description=str(analysis.get("clean_description") or analysis.get("description") or ""),
            domain=str(analysis.get("issue_domain") or analysis.get("issue_type") or ""),
            subcategory=str(analysis.get("issue_subcategory") or ""),
            issue_times=analysis.get("issue_times") or [],
            missing_info=analysis.get("missing_info") or [],
            reader_next_action=str(analysis.get("next_action") or ""))
    else:
        subject = ""
        try:
            subject = str((cases._ips().find_case(case_nbr) or {}).get("subject") or "")
        except Exception:
            pass
        if not subject and not question:
            return {"error": f"Beacon has not analysed case {case_nbr} and IPS gave no subject, so "
                             "there is nothing to ask Echo about. Start an analysis first, or ask "
                             "Echo a complete question with ask_echo." + (" " + note if note else "")}
        built = build_next_steps_question(subject=subject)
    if question:
        built += "\n\nThe engineer's specific question about this case:\n" + question[:1500]
    out = ask(built, ctx)
    out["case"] = case_nbr
    if note:
        out["warning"] = note
    return out


MAIL_EXCERPT_CHARS = 1200


def _run_about_mail(args: dict, ctx: Ctx) -> dict:
    """Echo's view on the technical problem a mail describes. Only the
    subject and the start of the text are sent, with the engineer's question."""
    from services.assistant.connectors import mail
    message = mail.read_handle(ctx, args.get("id"))
    if message.get("error"):
        return message
    question = str(args.get("question") or "").strip()
    subject = str(message.get("subject") or "").strip()
    excerpt = " ".join(str(message.get("body") or "").split())[:MAIL_EXCERPT_CHARS]
    if not (subject or excerpt):
        return {"error": "that mail has no text to ask Echo about"}
    parts = ["An Intel Wi-Fi / Bluetooth support engineer received the following mail and needs "
             "technical guidance on the problem it describes.",
             "", f"Mail subject: {subject[:200]}", "", "Mail text (excerpt):", excerpt, ""]
    if question:
        parts += ["The engineer's question:", question[:1500]]
    else:
        parts += ["From the driver / firmware documentation and past cases, answer concisely:",
                  "1. What is the likely technical area and known causes of this problem?",
                  "2. What should the engineer ask for or check next?",
                  "3. Any known similar issues or workarounds? Cite what you rely on."]
    out = ask("\n".join(parts), ctx)
    out["mail"] = str(args.get("id") or "").strip().lower()
    return out


def connectors() -> list[Connector]:
    return [
        Connector(
            name="ask_echo",
            description=(
                "Ask Echo, Intel's internal Wi-Fi / Bluetooth knowledge base (driver and firmware "
                "documentation, known issues, past cases), one technical question and get its "
                "grounded answer. Use it for technical questions about Intel wireless products: "
                "root causes, debug steps, what a log or an assert means, known issues. Write the "
                "question in English, self-contained, with the relevant details (adapter, driver "
                "version, OS, symptom, error codes). Slow: about one minute per question, so ask "
                "once with a complete question rather than several small ones. The answer is "
                "Intel-internal."),
            parameters=object_schema(
                {"question": {"type": "string",
                              "description": "The complete technical question, in English."}},
                ["question"]),
            run=_run_ask_echo,
            timeout_s=ECHO_TIMEOUT_S + 40,   # echo_client allows itself timeout + 30
            internal=True,
        ),
        Connector(
            name="ask_echo_about_case",
            description=(
                "Ask Echo about one IPS case: recommended next steps, what to request from the "
                "customer, known similar issues. The question is built from Beacon's analysis of "
                "the case, so you only pass the case number (and optionally the engineer's own "
                "extra question). Returns Beacon's stored Echo advice at once when there is one "
                "and no extra question; otherwise takes about a minute. The answer is "
                "Intel-internal."),
            parameters=object_schema(
                {"case_nbr": {"type": "string",
                              "description": "IPS case number, six to eight digits."},
                 "question": {"type": "string",
                              "description": "Optional: a specific question about this case, in English."}},
                ["case_nbr"]),
            run=_run_about_case,
            timeout_s=ECHO_TIMEOUT_S + 60,
            internal=True,
        ),
        Connector(
            name="ask_echo_about_mail",
            description=(
                "Ask Echo about the technical problem described in one mail from list_mail: "
                "likely causes, what to check or request next, known similar issues. Pass the "
                "mail's short id (m1, m2, ...) and optionally the engineer's own question. About "
                "a minute. The answer is Intel-internal."),
            parameters=object_schema(
                {"id": {"type": "string", "description": "The mail's short id from list_mail."},
                 "question": {"type": "string",
                              "description": "Optional: a specific question, in English."}},
                ["id"]),
            run=_run_about_mail,
            timeout_s=ECHO_TIMEOUT_S + 60,
            internal=True,
            untrusted=True,
        ),
    ]
