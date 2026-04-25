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

def load_territory_map():
    df = pd.read_excel(DATA_DIR / "PRO_KEY_GROWTH Mapping.xlsx")
    rows = []
    for _, r in df.iterrows():
        tier = str(r.get("Sales Region", "")).strip()
        territory = str(r.get("Legacy Territory", "")).strip()
        rep = str(r.get("2025 Sales Rep", "")).strip()
        wk_territory = str(r.get("2025 WK CE Territory", "")).strip()
        region = str(r.get("2025 Sales Region", "")).strip()
        if territory and rep and tier in ("Growth", "Key"):
            rows.append(f"  {tier} | {territory} | Rep: {rep} | {wk_territory} | {region}")
    return "\n".join(rows)


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
    lines = ["Lexidrug Individual Territory Assignments (CC Jeff Kelly on all):"]
    lines.append(f"  Jill Grahn: {', '.join(jill)}")
    lines.append(f"  Jay Carder: {', '.join(jay)}")
    lines.append(f"  Sam Preetham: {', '.join(sam)}")
    return "\n".join(lines)


def load_org_hierarchy():
    xl = pd.ExcelFile(DATA_DIR / "2025 PRO_Team Org Hierarchy.xlsx")
    lines = []
    for sheet in xl.sheet_names:
        df = xl.parse(sheet)
        if "Account Owner" not in df.columns:
            continue
        lines.append(f"\n[{sheet} accounts]")
        for _, r in df.iterrows():
            owner = str(r.get("Account Owner", "")).strip()
            terr = str(r.get("WK CE Territory", "")).strip()
            director = str(r.get("Sales Region Director Assignment", "")).strip()
            if owner and owner not in ("nan", "NaN", ""):
                lines.append(f"  {terr} | {owner} | Director: {director}")
    return "\n".join(lines)


TERRITORY_MAP = load_territory_map()
INDIVIDUAL_TERRITORIES = load_individual_territories()
ORG_HIERARCHY = load_org_hierarchy()

SYSTEM_PROMPT = f"""You are an expert MQL routing analyst for Wolters Kluwer Clinical Drug Information. Today is {TODAY}.

Given a lead ticket, output a complete ACTION PACKET in the exact format shown. No extra commentary outside that format.

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
ROUTING RULES
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

FIELD EXTRACTION (for Salesforce / form-fill input)
- Input may be a structured SF export, a copy-paste of SF record view, or a free-text email.
- Treat labeled lines ("Company: Acme", "Email: x@y.com") as authoritative field values.
- If a field is absent, note "Not stated" in the QUALIFICATION NOTE — do not guess or hallucinate values.

STEP 1 — VALIDITY & INTENT
- Fake/gibberish email or domain strongly contradicts a real healthcare company → SCAM
- Comment mentions login/password/access errors → SUPPORT
- No comment provided → NEEDS_CLARIFICATION

STEP 2 — LOCATION
- Not US or Canada → INTERNATIONAL (disqualify)
- Canada, Ontario → assign to Susan Roy
- Canada, other province → assign to Cheryl Leger
- Must be a healthcare provider to proceed
- If state/country is absent or ambiguous, add a ROUTING NOTE: "Location not stated — assumed US; verify." and proceed with US routing logic.

STEP 3 — COMMERCIAL GATE
- Primary business is NOT healthcare provision (payer, software co., consulting) → COMMERCIAL → Michele Leoni
- Exception: retail pharmacy clinic = provider

DEFINITION — "clinician user": any licensed or credentialed healthcare professional who directly uses the software for patient care (physicians, pharmacists, nurses, PAs, NPs, dentists). Administrators, IT staff, researchers, and billing staff do NOT count toward user-count thresholds. When user count is ambiguous (e.g. "a few", "our team"), route as NEEDS_CLARIFICATION rather than guessing.

STEP 4 — PRODUCT ROUTING (updated 3/31/2026)

  UPTODATE:
  - Hospital (any size) → IS sales rep for territory
  - University/School (any size) → IS sales rep for territory
  - Clinic ≥11 clinician users → IS sales rep for territory
  - Clinic ≤10 users → INDIVIDUAL SEGMENT → UTD Support (customerservice@uptodate.com)
  - US Federal Gov / VA / Military / Tribal → Justin Schenker (GOVERNMENT_2)
  - Support → customerservice@uptodate.com

  LEXICOMP / LEXIDRUG:
  - Hospital ≥401 beds → AM sales rep for territory + CC CDI product specialist
  - Hospital ≤400 beds → IS sales rep (DI Specialist: Nikki/Andrew)
  - Clinic ≥15 clinician users → IS sales rep (DI Specialist: Nikki/Andrew)
  - Clinic ≤14 users → INDIVIDUAL SEGMENT → lookup state → Jill/Jay/Sam + CC Jeff Kelly
  - University/School → IS sales rep (DI Specialist: Nikki/Andrew)
  - US Federal Gov / VA / Military / Tribal → Justin Schenker (GOVERNMENT_2) + CC CDI
  - Support → cs-cdi-support@wolterskluwer.com

  MEDI-SPAN / PRICE RX:
  - Hospital/Health System/Clinic (US) → DI Specialist (Nikki/Andrew) + Territory rep + CC Ron McBride + Jess Hissem
  - Hospital/Health System/Clinic (Canada) → Medi-Span Specialist (Andrea) + Territory rep + CC Andrea Cheshire
  - Non-healthcare primary business → Michele Leoni (Commercial Segment Manager)
  - University/School → DI Specialist + Territory rep
  - US Federal Gov → Government team + CC CDI
  - Home health → Medi-Span Specialist (Andrea) + Territory rep
  - Support → medispan-support@wolterskluwer.com
  - PriceRx: always qualify via email first (ask price type, use case, volume); attach SKU brochure

  EMMI / PATIENT ENGAGEMENT:
  - New Emmi → Emmi Sales Exec (New Business) + CC Emmi Exec (New Business) + Emmi Director
  - Emmi upsell → Emmi Sales Exec (Renewal) + CC Emmi Exec (Renewal) + Emmi Director
  - EmmiEducate ≥101 beds → Emmi Sales Exec (New Business)
  - EmmiEducate ≤100 beds → IS sales rep for territory + CC IS director + Emmi Sales Exec
  - Support → Emmi customer success

STEP 5 — TERRITORY LOOKUP
Match the lead's state/city to the correct territory and look up the rep.

[Growth & Key Territory Map — by Legacy Territory]
{TERRITORY_MAP}

[Full Org Hierarchy by Tier]
{ORG_HIERARCHY}

[Individual Territories for Lexidrug]
{INDIVIDUAL_TERRITORIES}

STEP 6 — EXISTING ACCOUNT
If the lead says they are a current customer, flag "CHECK SFDC FOR EXISTING OPPORTUNITY" — there may already be an assigned rep.

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
OUTPUT FORMAT (follow exactly — the web app parses these section headers)
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

## DECISION
[VALID SALES / INDIVIDUAL SEGMENT / SUPPORT / COMMERCIAL / INTERNATIONAL / SCAM / NEEDS_CLARIFICATION]

**Assigned to:** [Full Name or team]
**CC:** [comma-separated, or N/A]
**Territory:** [territory code + name, or N/A]
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
TERRITORY: [Territory name]
ROUTING RATIONALE: [1–2 sentences]
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
*(Send from SFDC — only for VALID SALES and INDIVIDUAL SEGMENT)*

**To:** [rep email or "[Rep Name]'s SFDC email"]
**CC:** [cc list]
**Subject:** MQL – [Company Name] – [Product]

[Single product — use this:]
Hi [Rep First Name],

Please see this MQL with the prospect's specific inquiry below that I'll be assigning over to you. I'm adding some additional information, which will be included in the MQL task in the Comments section.

Thanks,
Syamil

[Multiple products — use this instead:]
Hi All,

Please see this MQL with the prospect's specific inquiry below that I'll be assigning over to [Rep Name]. This prospect is interested in [Product List]. I'm adding some additional information, which will be included in the MQL task in the Comments section.

Thanks,
Syamil

---

## EMAIL TO LEAD
*(Only include this section if NEEDS_CLARIFICATION or SUPPORT)*

**To:** [lead email]
**Subject:** [Product] inquiry

[No comment — use:]
Hello [First Name],

I wanted to make sure you found what you were looking for. Is there anything I can help you with?

Thank you,
[Your Name]

[Unclear provider vs commercial — use:]
Hello [First Name],

I received your inquiry regarding [Product]. I can connect you with someone who can assist you, but I need a little more information first.

I was unable to find information on the company you listed ([Company]) in [City/State]. Can you tell me if the subscriptions for [Product] would be used to provide direct care to patients in an inpatient, ambulatory, or other setting? Or would they be used for another purpose like research, consulting, claims processing, or another use not related to direct patient care?

Knowing this will allow me to connect you to the correct person as quickly as possible.

Thank you very much!
[Your Name]

[Unclear user count — use:]
Hello [First Name],

I received your inquiry regarding [Product]. I can connect you with someone who can assist you, but I need a little information first. Can you tell me if you are inquiring regarding a subscription for your institution, or a personal subscription? If it is for your institution, approximately how many users would need access?

Thank you very much!
[Your Name]

---

## ROUTING NOTES

[Any flags, edge cases, ambiguities, or things to verify in SFDC / Definitive]
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
        "'Company:', 'Title:', 'City:', 'State/Province:', 'Lead Source:', 'Number of Users:', "
        "'Comments:', 'Existing Customer:'. Extract the relevant values from whatever format is provided "
        "(labeled fields, free text, or mixed), then apply the routing rules.\n\n"
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
    # host=0.0.0.0 lets coworkers on the same network reach it
    app.run(host="0.0.0.0", port=port, debug=False)
