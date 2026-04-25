import os
import re
import json
from datetime import date, timedelta
from pathlib import Path
from flask import Flask, render_template, request, jsonify, Response, stream_with_context
import time
from groq import Groq, APITimeoutError, AuthenticationError, RateLimitError
import pandas as pd
from dotenv import load_dotenv

load_dotenv(Path(__file__).parent.parent / ".env")

app = Flask(__name__)
DATA_DIR = Path(__file__).parent.parent / "All files"

TODAY = date.today().strftime("%Y-%m-%d")
_d = date.today()
TODAY_DISPLAY = f"{_d.month}/{_d.day}/{_d.year}"
REMINDER_DATE  = (date.today() + timedelta(days=3)).strftime("%Y-%m-%d")
REMINDER_DATE_5 = (date.today() + timedelta(days=5)).strftime("%Y-%m-%d")

MODEL = "llama-3.3-70b-versatile"


# ── Reference data ─────────────────────────────────────────────────────────────

def build_territory_lookup():
    """
    Builds the territory reference in two parts:
    Part A — 2-row product rep lookup (by EAST / WEST region).
    Part B — per-state table with territory code and Account Owner.
    Keeping these separate makes it easy for the model to:
      1. Identify region from state (EAST vs WEST list).
      2. Read the correct rep name for the product from Part A.
      3. Look up the WK code and Account Owner (AM) from Part B.
    """
    df = pd.read_excel(DATA_DIR / "PRO_KEY_GROWTH Mapping.xlsx")
    terr_to_growth, terr_to_key = {}, {}
    for _, r in df.iterrows():
        tier = str(r.get("Sales Region", "")).strip()
        terr = str(r.get("Legacy Territory", "")).strip()
        rep  = str(r.get("2025 Sales Rep", "")).strip()
        wk   = str(r.get("2025 WK CE Territory", "")).strip()
        if not terr or not wk or rep in ("nan", "NaN", ""):
            continue
        if tier == "Growth":
            terr_to_growth.setdefault(terr, (wk, rep))
        elif tier == "Key":
            terr_to_key.setdefault(terr, (wk, rep))

    # Part A — which rep to assign, by product and region
    # Effective 4/27/2026: Andrew Yonke replaces Nikki Calhoun for ALL Lexi routing.
    part_a = """
━━━ PART A — PRODUCT REP LOOKUP (read this FIRST to find who to assign) ━━━

Step 1: Determine the lead's region.
  EAST region states: AL AR CT DC DE FL GA IN KY LA MA MD ME MI MS NC NH NJ NY OH PA PR RI SC TN VA VT WV (and Canadian province Ontario)
  WEST region states: AK AZ CA CO HI IA ID IL KS MN MO MT ND NE NM NV OK OR SD TX UT WA WI WY

Step 2: Use the region to find the exact rep name to ASSIGN (not a description — the actual person):

  REGION    | PRODUCT          | ASSIGN THIS PERSON      | CC ALSO (besides AM and Don Piccano)
  ----------|------------------|-------------------------|--------------------------------------
  EAST      | UpToDate         | Alexey Fingado          | —
  WEST      | UpToDate         | Jerry McAuliffe         | —
  EAST      | Lexi/Medi-Span   | Andrew Yonke            | — (changed from Nikki Calhoun 4/27/2026)
  WEST      | Lexi/Medi-Span   | Andrew Yonke            | — (changed from Nikki Calhoun 4/27/2026)
  EAST      | Emmi / PE        | Steve Swope             | —
  WEST      | Emmi / PE        | Pete Runhaar            | —

  Exception — Lexi HOSPITAL >=401 beds: Assign = Account Owner (AM) from Part B, CC Andrew Yonke + Don Piccano.
  Exception — Government leads: Assign = Justin Schenker regardless of region.
  Exception — Canada: Ontario → Susan Roy; other provinces → Cheryl Leger.

  Director: EAST = Ben Ketchum | WEST = Traci Cornelison

━━━ PART B — STATE → TERRITORY CODE → ACCOUNT OWNER (AM) ━━━
(AM goes in CC for most leads; AM is the Assigned rep only for Lexi >=401-bed hospitals)
"""

    # Per-state data
    STATES = [
        # (state_label, city_hint, territory_name, group)
        ("Alabama",          "any",                 "TENNESSEE - ALABAMA",                                                                         "EAST"),
        ("Alaska",           "any",                 "WASHINGTON STATE",                                                                            "WEST"),
        ("Arizona",          "any",                 "ARIZONA - NEW MEXICO",                                                                        "WEST"),
        ("Arkansas",         "any",                 "NEW ORLEANS",                                                                                 "EAST"),
        ("California",       "Sacramento/Central",  "SACRAMENTO, CA",                                                                              "WEST"),
        ("California",       "San Diego area",      "SAN DIEGO, CA",                                                                               "WEST"),
        ("California",       "Bay Area/N. CA",      "SAN JOSE, CA",                                                                                "WEST"),
        ("Colorado",         "any",                 "UTAH - COLORADO",                                                                             "WEST"),
        ("Connecticut",      "any",                 "PHILLY-JERSEY",                                                                               "EAST"),
        ("DC/Washington DC", "any",                 "CAPITAL",                                                                                     "EAST"),
        ("Delaware",         "any",                 "CAPITAL",                                                                                     "EAST"),
        ("Florida",          "any",                 "FLORIDA (+Puerto Rico & Bahamas & Guam, Jamaica, Saint Lucia, Saint Kitts and Nevis)",         "EAST"),
        ("Georgia",          "any",                 "GEORGIA",                                                                                     "EAST"),
        ("Hawaii",           "any",                 "WASHINGTON STATE",                                                                            "WEST"),
        ("Idaho",            "any",                 "OREGON",                                                                                      "WEST"),
        ("Illinois",         "Chicago/North",       "WISCONSIN - NORTH ILLINOIS",                                                                  "WEST"),
        ("Illinois",         "South IL",            "SOUTH ILLINOIS",                                                                              "WEST"),
        ("Indiana",          "any",                 "INDIANA",                                                                                     "EAST"),
        ("Iowa",             "any",                 "NEBRASKA - IOWA, DAKOTAS",                                                                    "WEST"),
        ("Kansas",           "any",                 "KANSAS CITY-ST LOUIS",                                                                        "WEST"),
        ("Kentucky",         "any",                 "LEXINGTON, KY",                                                                               "EAST"),
        ("Louisiana",        "any",                 "NEW ORLEANS",                                                                                 "EAST"),
        ("Maine",            "any",                 "NEW ENGLAND NORTH",                                                                           "EAST"),
        ("Maryland",         "any",                 "CAPITAL",                                                                                     "EAST"),
        ("Massachusetts",    "Boston area",         "BOSTON, MA",                                                                                  "EAST"),
        ("Massachusetts",    "other MA cities",     "NEW ENGLAND NORTH",                                                                           "EAST"),
        ("Michigan",         "any",                 "MICHIGAN",                                                                                    "EAST"),
        ("Minnesota",        "any",                 "MINNESOTA",                                                                                   "WEST"),
        ("Mississippi",      "any",                 "NEW ORLEANS",                                                                                 "EAST"),
        ("Missouri",         "any",                 "KANSAS CITY-ST LOUIS",                                                                        "WEST"),
        ("Montana",          "any",                 "NEBRASKA - IOWA, DAKOTAS",                                                                    "WEST"),
        ("Nebraska",         "any",                 "NEBRASKA - IOWA, DAKOTAS",                                                                    "WEST"),
        ("Nevada",           "any",                 "ARIZONA - NEW MEXICO",                                                                        "WEST"),
        ("New Hampshire",    "any",                 "NEW ENGLAND NORTH",                                                                           "EAST"),
        ("New Jersey",       "any",                 "PHILLY-JERSEY",                                                                               "EAST"),
        ("New Mexico",       "any",                 "ARIZONA - NEW MEXICO",                                                                        "WEST"),
        ("New York",         "NYC/Long Island",     "NEW YORK CITY - LONG ISLAND",                                                                 "EAST"),
        ("New York",         "Upstate NY",          "NORTH NEW YORK",                                                                              "EAST"),
        ("North Carolina",   "any",                 "CAROLINAS",                                                                                   "EAST"),
        ("North Dakota",     "any",                 "NEBRASKA - IOWA, DAKOTAS",                                                                    "WEST"),
        ("Ohio",             "any",                 "OHIO",                                                                                        "EAST"),
        ("Oklahoma",         "any",                 "DALLAS TX",                                                                                   "WEST"),
        ("Oregon",           "any",                 "OREGON",                                                                                      "WEST"),
        ("Pennsylvania",     "Philadelphia area",   "PHILLY-JERSEY",                                                                               "EAST"),
        ("Pennsylvania",     "Pittsburgh area",     "PITTSBURGH, PA",                                                                              "EAST"),
        ("Puerto Rico",      "any",                 "FLORIDA (+Puerto Rico & Bahamas & Guam, Jamaica, Saint Lucia, Saint Kitts and Nevis)",         "EAST"),
        ("Rhode Island",     "any",                 "NEW ENGLAND NORTH",                                                                           "EAST"),
        ("South Carolina",   "any",                 "CAROLINAS",                                                                                   "EAST"),
        ("South Dakota",     "any",                 "NEBRASKA - IOWA, DAKOTAS",                                                                    "WEST"),
        ("Tennessee",        "any",                 "TENNESSEE - ALABAMA",                                                                         "EAST"),
        ("Texas",            "Dallas/Fort Worth",   "DALLAS TX",                                                                                   "WEST"),
        ("Texas",            "Houston/South TX",    "HOUSTON-SOUTH TEXAS",                                                                         "WEST"),
        ("Utah",             "any",                 "UTAH - COLORADO",                                                                             "WEST"),
        ("Vermont",          "any",                 "NEW ENGLAND NORTH",                                                                           "EAST"),
        ("Virginia",         "any",                 "CAPITAL",                                                                                     "EAST"),
        ("Washington State", "any",                 "WASHINGTON STATE",                                                                            "WEST"),
        ("West Virginia",    "any",                 "CAPITAL",                                                                                     "EAST"),
        ("Wisconsin",        "any",                 "WISCONSIN - NORTH ILLINOIS",                                                                  "WEST"),
        ("Wyoming",          "any",                 "NEBRASKA - IOWA, DAKOTAS",                                                                    "WEST"),
    ]

    part_b_lines = [f"  {'State':<20} {'City hint':<22} {'WK Code':<18} AM (Account Owner)"]
    part_b_lines.append("  " + "-" * 82)
    for state, city, terr, group in STATES:
        wk, am = terr_to_growth.get(terr, ("?", "check SFDC"))
        part_b_lines.append(f"  {state:<20} {city:<22} {wk:<18} {am}")

    return part_a + "\n".join(part_b_lines)


def load_individual_territories():
    df = pd.read_excel(DATA_DIR / "Individual territories by state.xlsx", header=None)
    jill, jay, sam = [], [], []
    for i, row in df.iterrows():
        if i < 3:
            continue
        v0 = str(row[0]).strip() if pd.notna(row[0]) else ""
        v2 = str(row[2]).strip() if pd.notna(row[2]) else ""
        v4 = str(row[4]).strip() if pd.notna(row[4]) else ""
        if v0 and v0 != "nan": jill.append(v0)
        if v2 and v2 != "nan": jay.append(v2)
        if v4 and v4 != "nan": sam.append(v4)
    return "\n".join([
        "LEXIDRUG INDIVIDUAL TERRITORY ASSIGNMENTS (CC Jeff Kelly + Don Piccano on all):",
        f"  Jill Grahn:   {', '.join(jill)}",
        f"  Jay Carder:   {', '.join(jay)}",
        f"  Sam Preetham: {', '.join(sam)}",
    ])


TERRITORY_LOOKUP       = build_territory_lookup()
INDIVIDUAL_TERRITORIES = load_individual_territories()


SYSTEM_PROMPT = f"""You are an expert MQL routing analyst for Wolters Kluwer Clinical Drug Information. Today is {TODAY}.

Output a complete ACTION PACKET in the exact format shown. No commentary outside the format. No preamble.

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
ROUTING RULES
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

FIELD EXTRACTION
- Input may be a Salesforce export, copy-paste of SF record view, or free-text email.
- Treat labeled lines ("Company: Acme", "Email: x@y.com") as authoritative.
- If a field is absent → write "Not stated" in the QUALIFICATION NOTE. Do not guess.
- If a zip code is present, use it to confirm the state/city before territory lookup.

STEP 1 — VALIDITY & INTENT
- Fake/gibberish email or domain that contradicts a real healthcare company → SCAM
- Comment mentions login / password / access errors → SUPPORT
- No comment provided → NEEDS_CLARIFICATION

STEP 2 — LOCATION
- Not US or Canada → INTERNATIONAL (disqualify)
- Canada, Ontario → Susan Roy (CANADA_2)
- Canada, other province → Cheryl Leger (CANADA_1)
- Missing state/country → add ROUTING NOTE: "Location not stated — assumed US; verify." then proceed.

STEP 3 — COMMERCIAL GATE
- Primary business is NOT healthcare provision (payer, software co., consulting) → COMMERCIAL → Michele Leoni
- Exception: retail pharmacy clinic = provider (proceed)

DEFINITION — "clinician user": licensed/credentialed healthcare professional who directly uses the
software for patient care (physicians, pharmacists, nurses, PAs, NPs, dentists). Admins, IT,
researchers, billing staff do NOT count. Ambiguous count ("a few", "our team") → NEEDS_CLARIFICATION.

STEP 4 — PRODUCT ROUTING

─── UPTODATE ──────────────────────────────────────────────
Government (US Fed/VA/Military/Tribal):
  Assign: Justin Schenker (GOVERNMENT_2) | CC: Don Piccano

Support request only:
  Send to: customerservice@uptodate.com (do not assign a rep)

Hospital (any size):
  Assign: UTD IS Rep from territory table | CC: Account Owner AM + Don Piccano

University / School (any size):
  Assign: UTD IS Rep from territory table | CC: Account Owner AM + Don Piccano

Clinic ≥11 clinician users:
  Assign: UTD IS Rep from territory table | CC: Account Owner AM + Don Piccano

Clinic ≤10 clinician users → INDIVIDUAL SEGMENT:
  Assign: customerservice@uptodate.com | CC: Don Piccano

─── LEXICOMP / LEXIDRUG ────────────────────────────────────
Government (US Fed/VA/Military/Tribal):
  Assign: Justin Schenker (GOVERNMENT_2) | CC: CDI product specialist + Don Piccano

Support request only:
  Send to: cs-cdi-support@wolterskluwer.com

Hospital ≥401 beds:
  Assign: Account Owner AM from territory table | CC: Andrew Yonke + Don Piccano

Hospital ≤400 beds:
  Assign: Andrew Yonke (Lexi IS Rep) | CC: Account Owner AM + Don Piccano

Clinic ≥15 clinician users:
  Assign: Andrew Yonke (Lexi IS Rep) | CC: Account Owner AM + Don Piccano

Clinic ≤14 clinician users → INDIVIDUAL SEGMENT:
  Look up state → Jill / Jay / Sam | CC: Jeff Kelly + Don Piccano

University / School:
  Assign: Andrew Yonke (Lexi IS Rep) | CC: Account Owner AM + Don Piccano

─── MEDI-SPAN / PRICE RX ───────────────────────────────────
US Hospital / Health System / Clinic:
  Assign: Andrew Yonke (Lexi/Medi DI Specialist) | CC: Account Owner AM + Ron McBride + Jess Hissem + Don Piccano

Canada:
  Assign: Andrea (Medi-Span Specialist) + territory rep | CC: Andrea Cheshire + Don Piccano

Non-healthcare → Michele Leoni (COMMERCIAL)
Home health: Andrea (Medi-Span Specialist) + territory rep | CC: Don Piccano
Support only: medispan-support@wolterskluwer.com
PriceRx: Qualify via email first (price type, use case, volume); attach SKU brochure.

─── EMMI / PATIENT ENGAGEMENT ──────────────────────────────
Support only: Emmi customer success team

New Emmi:
  Assign: Emmi Sales Exec (New Business) | CC: Emmi Exec (New Business) + Emmi Director + Don Piccano

Emmi upsell:
  Assign: Emmi Sales Exec (Renewal) | CC: Emmi Exec (Renewal) + Emmi Director + Don Piccano

EmmiEducate ≥101 beds:
  Assign: Emmi Sales Exec (New Business) | CC: Don Piccano

EmmiEducate ≤100 beds:
  Assign: PE IS Rep from territory table | CC: IS Director + Emmi Sales Exec + Don Piccano

STEP 5 — TERRITORY & REP LOOKUP

{TERRITORY_LOOKUP}

[Lexidrug Individual Segment]
{INDIVIDUAL_TERRITORIES}

STEP 6 — EXISTING ACCOUNT
If lead says they are a current customer → flag "CHECK SFDC FOR EXISTING OPPORTUNITY" in Routing Notes.

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
SUPPORT EMAIL REFERENCE (list ALL in Routing Notes when decision = SUPPORT)
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
  UpToDate:     customerservice@uptodate.com
  Lexicomp/CDI: cs-cdi-support@wolterskluwer.com
  Medi-Span:    medispan-support@wolterskluwer.com
  Emmi:         Emmi customer success team (look up in SFDC)

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
OUTPUT FORMAT — follow exactly, web app parses these headers
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

## DECISION
[VALID SALES / INDIVIDUAL SEGMENT / SUPPORT / COMMERCIAL / INTERNATIONAL / SCAM / NEEDS_CLARIFICATION]

**Assigned to:** [Full Name or support email]
**CC:** [comma-separated, or N/A]
**Territory:** [WK code + territory name, or N/A]
**Director:** [name, or N/A]

---

## SFDC CHECKLIST

**Task updates:**
- [ ] Task Owner → [Rep Name]
- [ ] Product Interest → [Product]
- [ ] Set Reminder → {REMINDER_DATE}
- [ ] Append Qualification Note (see below)

**Lead record updates:**
- [ ] Lead Status → [Status]
- [ ] Sales Region → [Region code]
- [ ] Title Category → [e.g. Physician / Administrator / Pharmacist]
- [ ] Segment → [Provider / Commercial / Government / Individual]

[Add these lines ONLY if INDIVIDUAL SEGMENT:]
- [ ] Mark Task as COMPLETED
- [ ] Lead Status → "Disqualified by segment marketing"
- [ ] Reason → "INDIVIDUAL SEGMENT"
- [ ] Do NOT change Task owner

[Add these lines ONLY if NEEDS_CLARIFICATION:]
- [ ] Set Reminder → {REMINDER_DATE} (await reply — 3 days)
- [ ] Attempt 2 reminder → {REMINDER_DATE_5} (if no reply to first)

---

## QUALIFICATION NOTE
*(Paste into SFDC Task Description / Comment)*

```
LEAD SOURCE: [value]
COMPANY: [value]
LOCATION: [City, State]
WEBSITE: [url or "Research needed"]
TOTAL PROVIDERS/BEDS: [number or "Not stated — verify in Definitive"]
GOVERNMENT ENTITY: [Yes / No / Unknown]
CURRENT ACCOUNT STATUS: [New lead / Existing customer — verify in SFDC]
ASSIGNED REP: [Full Name]
TERRITORY: [WK code + territory name]
ROUTING RATIONALE: [1–2 sentences]
RESEARCH LINKS:
  - Definitive: https://app.definitivehc.com
  - LinkedIn: https://www.linkedin.com/company/[slug]
  - Website: [domain]
```

---

## SALESFORCE NOTE
*(Paste into Salesforce Comment / Activity field)*

```
{TODAY_DISPLAY} Syamil:
- This is a lead for [Product] from Website.
- This is a [Ambulatory Clinic / Hospital / University / Health System / Other]. Headquartered in [City], [State][; note if lead-stated location differs from HQ]. Website: [domain or "Research needed"]
- Lead Title: [Title]. LinkedIn: [https://www.linkedin.com/company/slug — verify]
- SF Status: Checked ([New Account / Existing Customer — verify in SFDC]).
- Size: [beds OR clinician user count, or "Not stated — verify in Definitive"] ([Qualified / Not yet verified]).
- Routing: Based on HQ [City], [State] (zip: [zip if in lead, else "verify"]), mapped to [WK Territory code]. Assigned to [Rep Full Name].
```

---

## EMAIL TO REP
*(Send from SFDC — VALID SALES only)*

**To:** [rep email or "[Rep Name]'s SFDC email"]
**CC:** [list including Don Piccano]
**Subject:** MQL – [Company Name] – [Product]

[Single rep:]
Hi [Rep First Name],

Please see this MQL with the prospect's specific inquiry below that I'll be assigning over to you. I'm adding some additional information, which will be included in the MQL task in the Comments section.

Thanks,
Syamil

[Multiple reps:]
Hi All,

Please see this MQL with the prospect's specific inquiry below that I'll be assigning over to [Rep Name]. This prospect is interested in [Product List]. I'm adding some additional information, which will be included in the MQL task in the Comments section.

Thanks,
Syamil

---

## EMAIL TO LEAD
*(NEEDS_CLARIFICATION or SUPPORT only)*

**To:** [lead email]
**Subject:** [Product] inquiry

[No comment:]
Hello [First Name],

I wanted to make sure you found what you were looking for. Is there anything I can help you with?

Thank you,
[Your Name]

[Unclear provider vs commercial:]
Hello [First Name],

I received your inquiry regarding [Product]. I can connect you with someone who can assist, but I need a little more information first.

I was unable to find information on the company you listed ([Company]) in [City/State]. Can you tell me if the subscriptions for [Product] would be used to provide direct care to patients in an inpatient, ambulatory, or other clinical setting? Or for another purpose such as research, consulting, or claims processing?

Thank you very much!
[Your Name]

[Unclear user count:]
Hello [First Name],

I received your inquiry regarding [Product]. Can you tell me if you are inquiring for your institution, or a personal subscription? If for your institution, approximately how many users would need access?

Thank you very much!
[Your Name]

[Support:]
Hello [First Name],

Thank you for reaching out. Please contact the appropriate support team:

  UpToDate:     customerservice@uptodate.com
  Lexicomp/CDI: cs-cdi-support@wolterskluwer.com
  Medi-Span:    medispan-support@wolterskluwer.com
  Emmi:         Contact your customer success manager or visit the Emmi support portal.

Thank you,
[Your Name]

---

## ROUTING NOTES

[Flags, edge cases, ambiguities, things to verify in SFDC / Definitive.
If SUPPORT: list all support emails.
If existing customer: flag CHECK SFDC FOR EXISTING OPPORTUNITY.]
"""


# ── Flask routes ───────────────────────────────────────────────────────────────

@app.route("/")
def index():
    resp = app.make_response(render_template("index.html"))
    resp.headers["Cache-Control"] = "no-store"
    return resp


@app.route("/health")
def health():
    return jsonify({"status": "ok", "model": MODEL, "date": TODAY})


@app.route("/process", methods=["POST"])
def process():
    data = request.json

    api_key = os.getenv("GROQ_API_KEY", "")
    if not api_key or api_key == "your-api-key-here":
        return jsonify({"error": "Groq API key not configured.", "error_type": "auth"}), 500

    raw_text = data.get("raw_text", "").strip()
    if not raw_text or len(raw_text) < 10:
        return jsonify({"error": "Lead text is too short. Paste the full Salesforce record.", "error_type": "input"}), 400
    if len(raw_text) > 8000:
        return jsonify({"error": f"Input is {len(raw_text):,} characters — limit is 8,000.", "error_type": "input"}), 400

    user_message = (
        "The following text is from a Salesforce lead record or form-fill email. "
        "Extract ALL fields (including zip code if present) then apply the routing rules step by step.\n\n"
        f"{raw_text}"
    )

    messages = [
        {"role": "system", "content": SYSTEM_PROMPT},
        {"role": "user",   "content": user_message},
    ]

    def strip_think(text):
        """Remove <think>...</think> blocks that reasoning models output."""
        return re.sub(r"<think>[\s\S]*?</think>", "", text).lstrip("\n")

    def generate():
        try:
            client = Groq(api_key=api_key)
            stream = client.chat.completions.create(
                model=MODEL,
                max_tokens=4096,
                temperature=0,
                timeout=90,
                messages=messages,
                stream=True,
            )

            # Buffer for stripping <think> blocks mid-stream
            buf = ""
            in_think = False
            emitted_any = False

            for chunk in stream:
                delta = chunk.choices[0].delta.content
                if not delta:
                    continue
                buf += delta

                out = ""
                while buf:
                    if in_think:
                        end = buf.find("</think>")
                        if end >= 0:
                            buf = buf[end + 8:]
                            in_think = False
                        else:
                            buf = ""
                    else:
                        start = buf.find("<think>")
                        if start >= 0:
                            out += buf[:start]
                            buf = buf[start + 7:]
                            in_think = True
                        else:
                            # Safe to flush up to last 7 chars (possible partial <think>)
                            safe = buf[:-7] if len(buf) > 7 else ""
                            out += safe
                            buf = buf[len(safe):]
                            break

                if out:
                    if not emitted_any:
                        out = out.lstrip("\n")
                    if out:
                        emitted_any = True
                        yield f"data: {json.dumps({'chunk': out})}\n\n"

            # Flush remaining
            if buf and not in_think:
                yield f"data: {json.dumps({'chunk': buf})}\n\n"

            yield f"data: {json.dumps({'done': True})}\n\n"

        except APITimeoutError:
            yield f"data: {json.dumps({'error': 'The AI took too long. Try again.', 'error_type': 'timeout'})}\n\n"
        except AuthenticationError:
            yield f"data: {json.dumps({'error': 'API key invalid or expired.', 'error_type': 'auth'})}\n\n"
        except RateLimitError:
            yield f"data: {json.dumps({'error': 'Rate limit reached. Wait 60 seconds and retry.', 'error_type': 'rate_limit'})}\n\n"
        except Exception as e:
            yield f"data: {json.dumps({'error': str(e), 'error_type': 'unknown'})}\n\n"

    return Response(
        stream_with_context(generate()),
        content_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


if __name__ == "__main__":
    port = int(os.getenv("PORT", 5000))
    app.run(host="0.0.0.0", port=port, debug=False)
