import os
import sys
import re
from datetime import date, timedelta
from pathlib import Path
from flask import Flask, render_template, request, jsonify
import time
from groq import Groq, APITimeoutError, AuthenticationError, RateLimitError
import pandas as pd
from dotenv import load_dotenv

# Load .env from parent directory
load_dotenv(Path(__file__).parent.parent / ".env")

app = Flask(__name__)
DATA_DIR = Path(__file__).parent.parent / "All files"

TODAY = date.today().strftime("%Y-%m-%d")
_d = date.today()
TODAY_DISPLAY = f"{_d.month}/{_d.day}/{_d.year}"
REMINDER_DATE = (date.today() + timedelta(days=3)).strftime("%Y-%m-%d")
REMINDER_DATE_5 = (date.today() + timedelta(days=5)).strftime("%Y-%m-%d")


# ── Load reference data once at startup ───────────────────────────────────────

def build_territory_lookup():
    """
    Builds a comprehensive state→territory→rep lookup table.
    Account Owners come from Excel. NB Manager assignments are hardcoded
    from the org hierarchy (they apply region-wide, not per territory row).

    CHANGE effective 4/27/2026: Nikki Calhoun → Andrew Yonke for ALL Lexi routing.
    """

    # Load Growth and Key Account Owners from Excel
    df = pd.read_excel(DATA_DIR / "PRO_KEY_GROWTH Mapping.xlsx")
    terr_to_growth = {}  # territory_name → (wk_code, am)
    terr_to_key    = {}  # territory_name → (wk_code, am)
    for _, r in df.iterrows():
        tier  = str(r.get("Sales Region", "")).strip()
        terr  = str(r.get("Legacy Territory", "")).strip()
        rep   = str(r.get("2025 Sales Rep", "")).strip()
        wk    = str(r.get("2025 WK CE Territory", "")).strip()
        if not terr or not wk or rep in ("nan", "NaN", ""):
            continue
        if tier == "Growth":
            terr_to_growth.setdefault(terr, (wk, rep))
        elif tier == "Key":
            terr_to_key.setdefault(terr, (wk, rep))

    # NB Managers by region (hardcoded — applies to all territories in that region)
    # Effective 4/27/2026: Andrew Yonke replaces Nikki Calhoun for Lexi everywhere.
    NB = {
        "EAST": {
            "utd":  "Alexey Fingado",
            "lexi": "Andrew Yonke",
            "pe":   "Steve Swope",
            "dir":  "Ben Ketchum",
        },
        "WEST": {
            "utd":  "Jerry McAuliffe",
            "lexi": "Andrew Yonke",   # was Nikki Calhoun — changed 4/27/2026
            "pe":   "Pete Runhaar",
            "dir":  "Traci Cornelison",
        },
    }

    # State/city → (territory name from Excel, region group)
    # Territory names must match exactly what's in the Excel file.
    STATES = [
        # State                 City/area hint          Territory name (Excel)                                                                          Group
        ("Alabama",             "any",                  "TENNESSEE - ALABAMA",                                                                          "EAST"),
        ("Alaska",              "any",                  "WASHINGTON STATE",                                                                             "WEST"),
        ("Arizona",             "any",                  "ARIZONA - NEW MEXICO",                                                                         "WEST"),
        ("Arkansas",            "any",                  "NEW ORLEANS",                                                                                  "EAST"),
        ("California",          "Sacramento area",      "SACRAMENTO, CA",                                                                               "WEST"),
        ("California",          "San Diego area",       "SAN DIEGO, CA",                                                                                "WEST"),
        ("California",          "Bay Area / N. CA",     "SAN JOSE, CA",                                                                                 "WEST"),
        ("Colorado",            "any",                  "UTAH - COLORADO",                                                                              "WEST"),
        ("Connecticut",         "any",                  "PHILLY-JERSEY",                                                                                "EAST"),
        ("DC / Washington",     "any",                  "CAPITAL",                                                                                      "EAST"),
        ("Delaware",            "any",                  "CAPITAL",                                                                                      "EAST"),
        ("Florida",             "any",                  "FLORIDA (+Puerto Rico & Bahamas & Guam, Jamaica, Saint Lucia, Saint Kitts and Nevis)",          "EAST"),
        ("Georgia",             "any",                  "GEORGIA",                                                                                      "EAST"),
        ("Hawaii",              "any",                  "WASHINGTON STATE",                                                                             "WEST"),
        ("Idaho",               "any",                  "OREGON",                                                                                       "WEST"),
        ("Illinois",            "Chicago / North IL",   "WISCONSIN - NORTH ILLINOIS",                                                                   "WEST"),
        ("Illinois",            "South IL",             "SOUTH ILLINOIS",                                                                               "WEST"),
        ("Indiana",             "any",                  "INDIANA",                                                                                      "EAST"),
        ("Iowa",                "any",                  "NEBRASKA - IOWA, DAKOTAS",                                                                     "WEST"),
        ("Kansas",              "any",                  "KANSAS CITY-ST LOUIS",                                                                         "WEST"),
        ("Kentucky",            "any",                  "LEXINGTON, KY",                                                                                "EAST"),
        ("Louisiana",           "any",                  "NEW ORLEANS",                                                                                  "EAST"),
        ("Maine",               "any",                  "NEW ENGLAND NORTH",                                                                            "EAST"),
        ("Maryland",            "any",                  "CAPITAL",                                                                                      "EAST"),
        ("Massachusetts",       "Boston area",          "BOSTON, MA",                                                                                   "EAST"),
        ("Massachusetts",       "Other MA",             "NEW ENGLAND NORTH",                                                                            "EAST"),
        ("Michigan",            "any",                  "MICHIGAN",                                                                                     "EAST"),
        ("Minnesota",           "any",                  "MINNESOTA",                                                                                    "WEST"),
        ("Mississippi",         "any",                  "NEW ORLEANS",                                                                                  "EAST"),
        ("Missouri",            "any",                  "KANSAS CITY-ST LOUIS",                                                                         "WEST"),
        ("Montana",             "any",                  "NEBRASKA - IOWA, DAKOTAS",                                                                     "WEST"),
        ("Nebraska",            "any",                  "NEBRASKA - IOWA, DAKOTAS",                                                                     "WEST"),
        ("Nevada",              "any",                  "ARIZONA - NEW MEXICO",                                                                         "WEST"),
        ("New Hampshire",       "any",                  "NEW ENGLAND NORTH",                                                                            "EAST"),
        ("New Jersey",          "any",                  "PHILLY-JERSEY",                                                                                "EAST"),
        ("New Mexico",          "any",                  "ARIZONA - NEW MEXICO",                                                                         "WEST"),
        ("New York",            "NYC / Long Island",    "NEW YORK CITY - LONG ISLAND",                                                                  "EAST"),
        ("New York",            "Upstate NY",           "NORTH NEW YORK",                                                                               "EAST"),
        ("North Carolina",      "any",                  "CAROLINAS",                                                                                    "EAST"),
        ("North Dakota",        "any",                  "NEBRASKA - IOWA, DAKOTAS",                                                                     "WEST"),
        ("Ohio",                "any",                  "OHIO",                                                                                         "EAST"),
        ("Oklahoma",            "any",                  "DALLAS TX",                                                                                    "WEST"),
        ("Oregon",              "any",                  "OREGON",                                                                                       "WEST"),
        ("Pennsylvania",        "Philadelphia area",    "PHILLY-JERSEY",                                                                                "EAST"),
        ("Pennsylvania",        "Pittsburgh area",      "PITTSBURGH, PA",                                                                               "EAST"),
        ("Puerto Rico",         "any",                  "FLORIDA (+Puerto Rico & Bahamas & Guam, Jamaica, Saint Lucia, Saint Kitts and Nevis)",          "EAST"),
        ("Rhode Island",        "any",                  "NEW ENGLAND NORTH",                                                                            "EAST"),
        ("South Carolina",      "any",                  "CAROLINAS",                                                                                    "EAST"),
        ("South Dakota",        "any",                  "NEBRASKA - IOWA, DAKOTAS",                                                                     "WEST"),
        ("Tennessee",           "any",                  "TENNESSEE - ALABAMA",                                                                          "EAST"),
        ("Texas",               "Dallas / Fort Worth",  "DALLAS TX",                                                                                    "WEST"),
        ("Texas",               "Houston / South TX",   "HOUSTON-SOUTH TEXAS",                                                                          "WEST"),
        ("Utah",                "any",                  "UTAH - COLORADO",                                                                              "WEST"),
        ("Vermont",             "any",                  "NEW ENGLAND NORTH",                                                                            "EAST"),
        ("Virginia",            "any",                  "CAPITAL",                                                                                      "EAST"),
        ("Washington State",    "any",                  "WASHINGTON STATE",                                                                             "WEST"),
        ("West Virginia",       "any",                  "CAPITAL",                                                                                      "EAST"),
        ("Wisconsin",           "any",                  "WISCONSIN - NORTH ILLINOIS",                                                                   "WEST"),
        ("Wyoming",             "any",                  "NEBRASKA - IOWA, DAKOTAS",                                                                     "WEST"),
    ]

    header = (
        f"{'State':<18} {'City/Area':<22} {'WK Code':<16} "
        f"{'UTD → Assign':<18} {'Lexi → Assign':<18} {'PE → Assign':<16} "
        f"{'AM (large Lexi ≥401)':<34} {'Director'}"
    )
    sep = "-" * 165

    lines = [
        "⚠️  CHANGE 4/27/2026: ALL Lexi/Medi-Span new MQL leads → Andrew Yonke (replaces Nikki Calhoun).",
        "",
        "HOW TO USE THIS TABLE:",
        "  • UTD lead            → 'UTD → Assign' column is the Assigned rep.",
        "  • Lexi/Medi lead <401 beds or <15 users → 'Lexi → Assign' column.",
        "  • Lexi/Medi lead ≥401 beds hospital     → 'AM' column as Assigned; CC Lexi→Assign rep + Don Piccano.",
        "  • PE / Emmi lead      → 'PE → Assign' column.",
        "  • CC rule (all valid sales emails): always include AM + Don Piccano.",
        "  • If zip code is provided, use it to confirm state/city before matching.",
        "",
        "GROWTH ACCOUNT TERRITORY TABLE:",
        header,
        sep,
    ]

    for state, city, terr, group in STATES:
        nb = NB[group]
        wk, am = terr_to_growth.get(terr, ("?", "check SFDC"))
        am_short = am[:33] if len(am) > 33 else am
        lines.append(
            f"{state:<18} {city:<22} {wk:<16} "
            f"{nb['utd']:<18} {nb['lexi']:<18} {nb['pe']:<16} "
            f"{am_short:<34} {nb['dir']}"
        )

    # Key Account AM table (same territories, different reps — for large established accounts)
    lines.append("")
    lines.append("KEY ACCOUNT AMs (same territories — verify in SFDC if the account may be Key tier):")
    lines.append(f"  {'Territory':<32} {'Key AM':<28} {'WK Code':<16} Note")
    lines.append("  " + "-" * 100)
    for terr, (wk, am) in sorted(terr_to_key.items()):
        lines.append(f"  {terr[:31]:<32} {am[:27]:<28} {wk:<16}")

    return "\n".join(lines)


def load_individual_territories():
    df = pd.read_excel(DATA_DIR / "Individual territories by state.xlsx", header=None)
    jill, jay, sam = [], [], []
    for i, row in df.iterrows():
        if i < 3:
            continue
        v0 = str(row[0]).strip() if pd.notna(row[0]) else ""
        v2 = str(row[2]).strip() if pd.notna(row[2]) else ""
        v4 = str(row[4]).strip() if pd.notna(row[4]) else ""
        if v0 and v0 != "nan":
            jill.append(v0)
        if v2 and v2 != "nan":
            jay.append(v2)
        if v4 and v4 != "nan":
            sam.append(v4)
    lines = [
        "LEXIDRUG INDIVIDUAL TERRITORY ASSIGNMENTS (CC Jeff Kelly + Don Piccano on all):",
        f"  Jill Grahn:   {', '.join(jill)}",
        f"  Jay Carder:   {', '.join(jay)}",
        f"  Sam Preetham: {', '.join(sam)}",
    ]
    return "\n".join(lines)


TERRITORY_LOOKUP      = build_territory_lookup()
INDIVIDUAL_TERRITORIES = load_individual_territories()


SYSTEM_PROMPT = f"""You are an expert MQL routing analyst for Wolters Kluwer Clinical Drug Information. Today is {TODAY}.

Given a lead ticket, output a complete ACTION PACKET in the exact format shown. No extra commentary outside that format.

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
ROUTING RULES — follow these steps in order
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

FIELD EXTRACTION
- Input may be a structured Salesforce export, a copy-paste of SF record view, or a free-text email.
- Treat labeled lines ("Company: Acme", "Email: x@y.com") as authoritative field values.
- If a field is absent, write "Not stated" in the QUALIFICATION NOTE — do not guess.

STEP 1 — VALIDITY & INTENT
- Fake/gibberish email or domain that contradicts a real healthcare company → SCAM
- Comment mentions login / password / access errors → SUPPORT
- No comment provided → NEEDS_CLARIFICATION

STEP 2 — LOCATION
- Not US or Canada → INTERNATIONAL (disqualify)
- Canada, Ontario → assign to Susan Roy (CANADA_2)
- Canada, other province → assign to Cheryl Leger (CANADA_1)
- If state/country is absent or ambiguous → add ROUTING NOTE: "Location not stated — assumed US; verify."
  then proceed with US routing.

STEP 3 — COMMERCIAL GATE
- Primary business is NOT healthcare provision (payer, software company, consulting firm) → COMMERCIAL → Michele Leoni
- Exception: retail pharmacy clinic = provider (proceed to Step 4)

DEFINITION — "clinician user": licensed/credentialed healthcare professional who directly uses the
software for patient care (physicians, pharmacists, nurses, PAs, NPs, dentists). Administrators, IT,
researchers, and billing staff do NOT count. When user count is ambiguous ("a few", "our team") →
route as NEEDS_CLARIFICATION rather than guessing.

STEP 4 — PRODUCT ROUTING

─── UPTODATE ───────────────────────────────────────────────────────────────
Government (US Fed / VA / Military / Tribal):
  Assign: Justin Schenker (GOVERNMENT_2)
  CC: Don Piccano

Support request only:
  Assign: customerservice@uptodate.com
  Do not assign a rep.

Hospital (any size):
  Assign: UTD IS Rep for territory (see territory table in Step 5)
  CC: Account Owner AM, Don Piccano

University / School (any size):
  Assign: UTD IS Rep for territory
  CC: Account Owner AM, Don Piccano

Clinic ≥11 clinician users:
  Assign: UTD IS Rep for territory
  CC: Account Owner AM, Don Piccano

Clinic ≤10 clinician users → INDIVIDUAL SEGMENT:
  Assign: customerservice@uptodate.com (UTD Support queue)
  CC: Don Piccano

─── LEXICOMP / LEXIDRUG ────────────────────────────────────────────────────
Government (US Fed / VA / Military / Tribal):
  Assign: Justin Schenker (GOVERNMENT_2)
  CC: CDI product specialist, Don Piccano

Support request only:
  Assign: cs-cdi-support@wolterskluwer.com
  Do not assign a rep.

Hospital ≥401 beds:
  Assign: Account Owner AM for territory (see territory table — "AM" column)
  CC: Andrew Yonke (Lexi IS Rep / DI Specialist), Don Piccano

Hospital ≤400 beds:
  Assign: Andrew Yonke (Lexi IS Rep — see territory table "Lexi → Assign" column)
  CC: Account Owner AM, Don Piccano

Clinic ≥15 clinician users:
  Assign: Andrew Yonke (Lexi IS Rep)
  CC: Account Owner AM, Don Piccano

Clinic ≤14 clinician users → INDIVIDUAL SEGMENT:
  Look up state in individual territory table (Step 5) → assign Jill / Jay / Sam
  CC: Jeff Kelly, Don Piccano

University / School:
  Assign: Andrew Yonke (Lexi IS Rep)
  CC: Account Owner AM, Don Piccano

─── MEDI-SPAN / PRICE RX ───────────────────────────────────────────────────
US Hospital / Health System / Clinic:
  Assign: Andrew Yonke (Lexi/Medi DI Specialist — same rep)
  CC: Account Owner AM, Ron McBride, Jess Hissem, Don Piccano

Canada:
  Assign: Andrea (Medi-Span Specialist) + territory rep
  CC: Andrea Cheshire, Don Piccano

Non-healthcare business: Michele Leoni (COMMERCIAL)

US Government:
  Assign: Government team
  CC: CDI, Don Piccano

Home health:
  Assign: Andrea (Medi-Span Specialist) + territory rep
  CC: Don Piccano

Support request only: medispan-support@wolterskluwer.com

PriceRx: Qualify via email first (ask price type, use case, volume); attach SKU brochure.

─── EMMI / PATIENT ENGAGEMENT ──────────────────────────────────────────────
Support request only: Emmi customer success team

New Emmi:
  Assign: Emmi Sales Exec (New Business)
  CC: Emmi Exec (New Business), Emmi Director, Don Piccano

Emmi upsell:
  Assign: Emmi Sales Exec (Renewal)
  CC: Emmi Exec (Renewal), Emmi Director, Don Piccano

EmmiEducate ≥101 beds:
  Assign: Emmi Sales Exec (New Business)
  CC: Don Piccano

EmmiEducate ≤100 beds:
  Assign: PE IS Rep for territory (see territory table "PE → Assign" column)
  CC: IS Director, Emmi Sales Exec, Don Piccano

STEP 5 — TERRITORY & REP LOOKUP

{TERRITORY_LOOKUP}

[Individual Territories for Lexidrug Individual Segment]
{INDIVIDUAL_TERRITORIES}

STEP 6 — EXISTING ACCOUNT CHECK
If the lead says they are a current customer → flag "CHECK SFDC FOR EXISTING OPPORTUNITY" in Routing Notes.

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
SUPPORT EMAIL REFERENCE (always include all three in ROUTING NOTES when decision = SUPPORT)
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
  UpToDate Support:   customerservice@uptodate.com
  Lexicomp/CDI:       cs-cdi-support@wolterskluwer.com
  Medi-Span:          medispan-support@wolterskluwer.com
  Emmi:               Emmi customer success team (look up in SFDC)

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
OUTPUT FORMAT (follow exactly — the web app parses these section headers)
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

## DECISION
[VALID SALES / INDIVIDUAL SEGMENT / SUPPORT / COMMERCIAL / INTERNATIONAL / SCAM / NEEDS_CLARIFICATION]

**Assigned to:** [Full Name or support email]
**CC:** [comma-separated list, or N/A]
**Territory:** [WK territory code + territory name, or N/A]
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
- [ ] Attempt 2 reminder → {REMINDER_DATE_5} (if no reply to first email)

---

## QUALIFICATION NOTE
*(Paste this into the SFDC Task Description / Comment)*

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
ROUTING RATIONALE: [1–2 sentences explaining why this rep was chosen]
RESEARCH LINKS:
  - Definitive: https://app.definitivehc.com (search company name)
  - LinkedIn: https://www.linkedin.com/company/[slug]
  - Website: [domain]
```

---

## SALESFORCE NOTE
*(Paste this into the Salesforce Comment / Activity field)*

```
{TODAY_DISPLAY} Syamil:
- This is a lead for [Product] from Website.
- This is a [Ambulatory Clinic / Hospital / University / Health System / Other]. Headquartered in [Real HQ City], [Real HQ State][; if lead-stated location differs from HQ: " — Lead listed [lead city/state]; verify HQ"]. Website: [domain or "Research needed"]
- Lead Title: [Title from lead]. LinkedIn: [https://www.linkedin.com/company/slug — verify]
- SF Status: Checked ([New Account / Existing Customer — verify in SFDC]).
- Size: [number of beds OR clinician user count, or "Not stated — verify in Definitive"] ([Qualified / Not yet verified]).
- Routing: Based on HQ [City], [State] (zip: [zip if provided in lead, else "verify"]), mapped to [Territory code + name]. Assigned to [Rep Full Name].
```

---

## EMAIL TO REP
*(Send from SFDC — only for VALID SALES)*

**To:** [rep email or "[Rep Name]'s SFDC email"]
**CC:** [cc list including Don Piccano]
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
*(Only include this section if NEEDS_CLARIFICATION or SUPPORT)*

**To:** [lead email]
**Subject:** [Product] inquiry

[No comment provided:]
Hello [First Name],

I wanted to make sure you found what you were looking for. Is there anything I can help you with?

Thank you,
[Your Name]

[Unclear provider vs commercial:]
Hello [First Name],

I received your inquiry regarding [Product]. I can connect you with someone who can assist you, but I need a little more information first.

I was unable to find information on the company you listed ([Company]) in [City/State]. Can you tell me if the subscriptions for [Product] would be used to provide direct care to patients in an inpatient, ambulatory, or other setting? Or would they be used for another purpose like research, consulting, claims processing, or another use not related to direct patient care?

Knowing this will allow me to connect you to the correct person as quickly as possible.

Thank you very much!
[Your Name]

[Unclear user count:]
Hello [First Name],

I received your inquiry regarding [Product]. I can connect you with someone who can assist you, but I need a little information first. Can you tell me if you are inquiring regarding a subscription for your institution, or a personal subscription? If it is for your institution, approximately how many users would need access?

Thank you very much!
[Your Name]

[Support routing — include all support contacts:]
Hello [First Name],

Thank you for reaching out. Based on your inquiry, please contact the appropriate support team directly:

  UpToDate Support:  customerservice@uptodate.com
  Lexicomp/CDI:      cs-cdi-support@wolterskluwer.com
  Medi-Span:         medispan-support@wolterskluwer.com
  Emmi:              Please visit the Emmi support portal or contact your customer success manager.

Thank you,
[Your Name]

---

## ROUTING NOTES

[Any flags, edge cases, ambiguities, or things to verify in SFDC / Definitive.
If SUPPORT: list all support emails above.
If lead is a current customer: flag CHECK SFDC FOR EXISTING OPPORTUNITY.]
"""


# ── Flask routes ──────────────────────────────────────────────────────────────

@app.route("/")
def index():
    return render_template("index.html")


@app.route("/process", methods=["POST"])
def process():
    data = request.json

    api_key = os.getenv("GROQ_API_KEY", "")
    if not api_key or api_key == "your-api-key-here":
        return jsonify({"error": "Groq API key not configured. Add GROQ_API_KEY to the .env file."}), 500

    raw_text = data.get('raw_text', '').strip()
    if not raw_text or len(raw_text) < 10:
        return jsonify({"error": "Lead text is too short. Paste the full Salesforce record.", "error_type": "input"}), 400
    if len(raw_text) > 8000:
        return jsonify({"error": f"Input is {len(raw_text):,} characters — limit is 8,000. Trim the record and try again.", "error_type": "input"}), 400

    user_message = (
        "The following text was copied directly from a Salesforce lead record or a form-fill email. "
        "Salesforce records typically contain labeled fields like 'First Name:', 'Last Name:', 'Email:', "
        "'Company:', 'Title:', 'City:', 'State/Province:', 'Zip/Postal Code:', 'Lead Source:', "
        "'Number of Users:', 'Comments:', 'Existing Customer:'. "
        "Extract ALL field values (including zip code if present) from whatever format is provided, "
        "then apply the routing rules step by step.\n\n"
        f"{raw_text}"
    )

    messages = [
        {"role": "system", "content": SYSTEM_PROMPT},
        {"role": "user",   "content": user_message}
    ]

    def call_groq(client):
        return client.chat.completions.create(
            model="llama-3.3-70b-versatile",
            max_tokens=4096,
            temperature=0,
            timeout=30,
            messages=messages
        )

    try:
        client = Groq(api_key=api_key)
        try:
            message = call_groq(client)
        except APITimeoutError:
            time.sleep(2)
            try:
                message = call_groq(client)
            except APITimeoutError:
                return jsonify({"error": "The AI took too long to respond. Groq may be under load — try again in a moment.", "error_type": "timeout"}), 504

        result = message.choices[0].message.content
        return jsonify({"result": result})

    except AuthenticationError:
        return jsonify({"error": "Groq API key is invalid or expired. Check the GROQ_API_KEY value in the .env file.", "error_type": "auth"}), 401
    except RateLimitError:
        return jsonify({"error": "Groq rate limit reached. Wait 60 seconds and try again.", "error_type": "rate_limit"}), 429
    except Exception as e:
        return jsonify({"error": f"Unexpected error: {str(e)}", "error_type": "unknown"}), 500


if __name__ == "__main__":
    port = int(os.getenv("PORT", 5000))
    app.run(host="0.0.0.0", port=port, debug=False)
