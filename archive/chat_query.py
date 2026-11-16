"""
Conversational interface over the scraped USYD Software Engineering
stream table data (stream_table_units.json) and any semester data
already gathered by scale_live_fetch.py (handbook_results.json).

Design: retrieval stays deterministic (fast, precise, no hallucination
on facts you've already scraped), but every reply is PHRASED by
Qwen3-8B rather than printed as a raw template — the retrieved data is
passed to the model as grounding context, and it writes the actual
conversational response. This is the standard retrieval-augmented-
generation (RAG) pattern: retrieve first, generate second, so the
conversation feels natural without the model ever inventing facts.

If Ollama is unreachable, each handler falls back to the raw
deterministic text rather than failing outright.

Handles questions like:
  - "give me the details on INFO1110"
  - "which units have INFO1113 as a prerequisite?"
  - "does any unit have COMP2017 as a prerequisite or a prohibition?"
  - "which COMP units are available in semester 1?"
  - "what are the total degree/stream requirements?"
  - general chat / unclear questions — handled conversationally too,
    rather than a fixed "not sure what you mean" message every time.
"""

import json
import os
import re

import requests

from scale_live_fetch import fetch_unit_page, OLLAMA_URL, MODEL_NAME

STREAM_TABLE_FILE = "stream_table_units.json"
HANDBOOK_RESULTS_FILE = "handbook_results.json"

UNIT_CODE_PATTERN = re.compile(r"\b([A-Za-z]{2,5}\d{3,4})\b")
SEMESTER_PATTERN = re.compile(r"semester\s*(1|2|one|two)", re.IGNORECASE)

RELATIONSHIP_KEYWORDS = {
    "assumed knowledge": "assumed_knowledge_codes",
    "assumed": "assumed_knowledge_codes",
    "prerequisite": "prerequisite_codes",
    "prereq": "prerequisite_codes",
    "corequisite": "corequisite_codes",
    "coreq": "corequisite_codes",
    "prohibition": "prohibition_codes",
    "prohibit": "prohibition_codes",
}

RELATIONSHIP_FIELD_LABELS = {
    "assumed_knowledge_codes": "assumed knowledge",
    "prerequisite_codes": "prerequisite",
    "corequisite_codes": "corequisite",
    "prohibition_codes": "prohibition",
}

ABOUT_KEYWORDS = ["degree", "requirement", "stream", "overview", "how many credit points does the"]

CAPABILITIES_DESCRIPTION = """You are a conversational assistant for a University of Sydney \
Software Engineering student, backed by scraped handbook data for units in the \
Software Engineering stream. You can help with:
- Details on a specific unit (code, name, credit points, prerequisites, corequisites,
  prohibitions, semester availability)
- Which units require a given unit as a prerequisite / corequisite / prohibition /
  assumed knowledge
- Which units in a subject area (e.g. COMP, INFO, ELEC) are offered in a given semester
- The overall stream's credit point structure"""


# ---------------------------------------------------------------------
# Data loading
# ---------------------------------------------------------------------

def load_units():
    """
    Load the stream table data and merge in any semester data already
    gathered. Returns (units_by_code, stream_info).
    """
    if not os.path.exists(STREAM_TABLE_FILE):
        print(f"[ERROR] {STREAM_TABLE_FILE} not found — run scale_live_fetch.py first.")
        return {}, None

    with open(STREAM_TABLE_FILE) as f:
        data = json.load(f)

    if isinstance(data, list):
        stream_info = None
        table_units = data
    else:
        stream_info = data.get("stream_info")
        table_units = data.get("units", [])

    units_by_code = {u["code"]: dict(u) for u in table_units}

    if os.path.exists(HANDBOOK_RESULTS_FILE):
        with open(HANDBOOK_RESULTS_FILE) as f:
            handbook_results = json.load(f)
        for r in handbook_results:
            code = r.get("code")
            if code in units_by_code and r.get("semester_availability"):
                units_by_code[code]["semester_availability"] = r["semester_availability"]

    return units_by_code, stream_info


def known_prefixes(units_by_code):
    prefixes = set()
    for code in units_by_code:
        match = re.match(r"([A-Za-z]+)\d+", code)
        if match:
            prefixes.add(match.group(1).upper())
    return prefixes


def detect_relationship_fields(q_lower):
    fields = []
    for keyword, field in RELATIONSHIP_KEYWORDS.items():
        if keyword in q_lower and field not in fields:
            fields.append(field)
    return fields


# ---------------------------------------------------------------------
# Formatting (used as LLM grounding context AND as a fallback if Ollama
# is unreachable — so the tool degrades gracefully rather than failing)
# ---------------------------------------------------------------------

def format_unit(unit):
    lines = [f"{unit['code']} — {unit.get('name', 'Unknown name')}"]
    if unit.get("credit_points") is not None:
        lines.append(f"Credit points: {unit['credit_points']}")
    if unit.get("assumed_knowledge_codes"):
        lines.append(f"Assumed knowledge: {', '.join(unit['assumed_knowledge_codes'])}")
    if unit.get("prerequisite_codes"):
        lines.append(f"Prerequisites: {', '.join(unit['prerequisite_codes'])}")
    if unit.get("corequisite_codes"):
        lines.append(f"Corequisites: {', '.join(unit['corequisite_codes'])}")
    if unit.get("prohibition_codes"):
        lines.append(f"Prohibitions: {', '.join(unit['prohibition_codes'])}")
    if unit.get("semester_availability"):
        lines.append(f"Semester availability: {unit['semester_availability']}")
    else:
        lines.append("Semester availability: not looked up yet")
    return "\n".join(lines)


# ---------------------------------------------------------------------
# LLM synthesis — turns retrieved facts into a natural reply
# ---------------------------------------------------------------------

def synthesize_response(question, context, fallback_text, timeout=60):
    """
    Ask Qwen3-8B to phrase a natural, conversational reply using ONLY
    the retrieved context as its factual basis. Falls back to the raw
    context text if Ollama is unreachable, so the tool stays usable.
    """
    prompt = f"""You are a friendly, knowledgeable assistant helping a University of \
Sydney student with Software Engineering stream unit information.

The student asked: "{question}"

Here is the factual data retrieved for this question. Treat it as ground
truth — don't add, guess, or invent anything beyond what's here:
{context}

Write a natural, conversational reply using only this information. Be
warm, direct, and concise — a couple of sentences is usually enough.
"""
    try:
        response = requests.post(
            OLLAMA_URL,
            json={"model": MODEL_NAME, "prompt": prompt, "stream": False},
            timeout=timeout,
        )
        response.raise_for_status()
        return response.json().get("response", "").strip()
    except requests.exceptions.RequestException as e:
        print(f"  [WARN] Couldn't reach Ollama to phrase this conversationally ({e}) — showing raw data instead.")
        return fallback_text


# ---------------------------------------------------------------------
# Live LLM fallback — for unit codes not already in the table
# ---------------------------------------------------------------------

def live_fetch_and_summarise(code):
    print(f"  ({code} isn't in the stream table — fetching its handbook page live...)")
    handbook_content, fetch_error = fetch_unit_page(code)
    if fetch_error:
        return f"I couldn't fetch a handbook page for {code} ({fetch_error}) — double check the unit code."

    prompt = f"""The following is content fetched live from the University of Sydney
handbook for unit {code}:

{handbook_content}

Based only on the information above, tell the student in a natural,
conversational way: the unit name, credit points, prerequisites,
corequisites, prohibitions (if any), and semester availability. If a
field isn't stated, say so.
"""
    try:
        response = requests.post(
            OLLAMA_URL,
            json={"model": MODEL_NAME, "prompt": prompt, "stream": False},
            timeout=120,
        )
        response.raise_for_status()
        return response.json().get("response", "").strip()
    except requests.exceptions.RequestException as e:
        return f"Couldn't reach Ollama to summarise {code}: {e}"


# ---------------------------------------------------------------------
# Query handlers — deterministic retrieval, LLM-phrased output
# ---------------------------------------------------------------------

def handle_details(question, code, units_by_code):
    code = code.upper()
    if code in units_by_code:
        context = format_unit(units_by_code[code])
        return synthesize_response(question, context, fallback_text=context)
    # Not in the table — live_fetch_and_summarise already produces a
    # conversational answer in one LLM call, so no second synthesis pass.
    return live_fetch_and_summarise(code)


def handle_relationship_search(question, code, fields, units_by_code):
    code = code.upper()
    matches = []
    for u in units_by_code.values():
        via = [RELATIONSHIP_FIELD_LABELS[f] for f in fields if code in u.get(f, [])]
        if via:
            matches.append((u, via))

    field_labels = " or ".join(RELATIONSHIP_FIELD_LABELS[f] for f in fields)
    if not matches:
        context = f"No units in the scraped stream table list {code} as a {field_labels}."
    else:
        lines = [f"Units with {code} as a {field_labels}:"]
        for u, via in sorted(matches, key=lambda pair: pair[0]["code"]):
            lines.append(f"- {u['code']} ({u.get('name', 'Unknown name')}) — via {', '.join(via)}")
        context = "\n".join(lines)

    return synthesize_response(question, context, fallback_text=context)


def handle_prefix_semester_search(question, prefix, semester_query, units_by_code):
    prefix = prefix.upper()
    prefix_units = [u for u in units_by_code.values() if u["code"].startswith(prefix)]
    matches = [
        u for u in prefix_units
        if u.get("semester_availability")
        and semester_query.lower() in u["semester_availability"].lower()
    ]

    if not matches:
        has_semester_data = sum(1 for u in prefix_units if u.get("semester_availability"))
        context = (f"No {prefix} units matched '{semester_query}'. "
                   f"({len(prefix_units)} {prefix} units in the table, {has_semester_data} have "
                   f"semester data so far.)")
    else:
        lines = [f"{prefix} units available in {semester_query}:"]
        for u in sorted(matches, key=lambda u: u["code"]):
            lines.append(f"- {u['code']} ({u.get('name', 'Unknown name')})")
        context = "\n".join(lines)

    return synthesize_response(question, context, fallback_text=context)


def handle_about(question, stream_info):
    if not stream_info:
        context = "No stream overview was captured (rerun scale_live_fetch.py to get it)."
    else:
        lines = []
        if stream_info.get("heading"):
            lines.append(stream_info["heading"])
        lines.extend(stream_info.get("description_lines", []))
        context = "\n".join(lines) if lines else "No stream overview text found."

    return synthesize_response(question, context, fallback_text=context)


def handle_general_chat(question):
    """For questions that don't match a data pattern — respond naturally rather than a fixed message."""
    prompt = f"""{CAPABILITIES_DESCRIPTION}

A student just said: "{question}"

This doesn't match a specific unit lookup. Respond naturally and briefly.
If it's small talk, respond warmly. If they seem to be trying to ask
about units but were unclear, help by suggesting 1-2 example questions
you could answer, based on what you can help with above.
"""
    try:
        response = requests.post(
            OLLAMA_URL,
            json={"model": MODEL_NAME, "prompt": prompt, "stream": False},
            timeout=60,
        )
        response.raise_for_status()
        return response.json().get("response", "").strip()
    except requests.exceptions.RequestException:
        return ("Not sure what you're asking — Ollama's also unreachable right now so I "
                "can't improvise. Try things like:\n"
                "  - give me the details on INFO1110\n"
                "  - which units have INFO1113 as a prerequisite?\n"
                "  - which COMP units are available in semester 1?")


# ---------------------------------------------------------------------
# Intent parsing — routes a question to one of the handlers above
# ---------------------------------------------------------------------

def parse_and_answer(question, units_by_code, stream_info):
    q = question.strip()
    q_lower = q.lower()
    codes_found = UNIT_CODE_PATTERN.findall(q)

    relationship_fields = detect_relationship_fields(q_lower)
    if relationship_fields:
        if codes_found:
            return handle_relationship_search(q, codes_found[0], relationship_fields, units_by_code)
        return handle_general_chat(q)

    semester_match = SEMESTER_PATTERN.search(q_lower)
    if semester_match and not codes_found:
        sem_word = semester_match.group(1)
        sem_number = {"one": "1", "two": "2"}.get(sem_word, sem_word)
        semester_query = f"semester {sem_number}"

        prefixes = known_prefixes(units_by_code)
        prefix_found = None
        for word in re.findall(r"[A-Za-z]+", q):
            if word.upper() in prefixes:
                prefix_found = word.upper()
                break

        if prefix_found:
            return handle_prefix_semester_search(q, prefix_found, semester_query, units_by_code)
        return handle_general_chat(q)

    if any(k in q_lower for k in ABOUT_KEYWORDS) and not codes_found:
        return handle_about(q, stream_info)

    if codes_found:
        return handle_details(q, codes_found[0], units_by_code)

    return handle_general_chat(q)


# ---------------------------------------------------------------------
# REPL
# ---------------------------------------------------------------------

def main():
    print("Loading unit data...")
    units_by_code, stream_info = load_units()
    if not units_by_code:
        return

    print(f"Loaded {len(units_by_code)} units from {STREAM_TABLE_FILE}.")
    with_semester = sum(1 for u in units_by_code.values() if u.get("semester_availability"))
    print(f"({with_semester} of them have semester availability data so far.)")
    print("\nAsk me anything about the units, or type 'quit' to exit.\n")

    while True:
        try:
            question = input("> ").strip()
        except (EOFError, KeyboardInterrupt):
            print("\nBye.")
            break

        if not question:
            continue
        if question.lower() in ("quit", "exit"):
            print("Bye.")
            break

        print(parse_and_answer(question, units_by_code, stream_info))
        print()


if __name__ == "__main__":
    main()