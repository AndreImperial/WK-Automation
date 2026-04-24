"""
MQL Lead Routing Assistant
--------------------------
Paste a lead ticket and get back a complete action packet:
  - Routing decision
  - SFDC checklist
  - Qualification note (ready to paste)
  - Email drafts (to rep + to lead if needed)

Usage:
  python process_lead.py                  # interactive prompt
  python process_lead.py lead.json        # from JSON file
  python process_lead.py --batch leads.csv  # batch mode (CSV)
"""

import sys
import json
import os
import re
from datetime import date, timedelta
from pathlib import Path
import pandas as pd
from dotenv import load_dotenv
import anthropic

load_dotenv()

DATA_DIR = Path(__file__).parent / "All files"
TODAY = date.today().strftime("%Y-%m-%d")
REMINDER_DATE = (date.today() + timedelta(days=3)).strftime("%Y-%m-%d")

# ─────────────────────────────────────────────────
# Load reference data once at startup
# ─────────────────────────────────────────────────

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
    # Columns: 0=Jill Grahn states, 2=Jay Carder states, 4=Sam Preetham states
    # Row 3 is the rep names row
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
    return {
        "Jill Grahn": jill,
        "Jay Carder": jay,
        "Sam Preetham": sam,
    }


def load_org_hierarchy():
    xl = pd.ExcelFile(DATA_DIR / "2025 PRO_Team Org Hierarchy.xlsx")
    summary = {}
    for sheet in xl.sheet_names:
        df = xl.parse(sheet)
        if "Account Owner" in df.columns and "WK CE Territory" in df.columns:
            reps = []
            for _, r in df.iterrows():
                owner = str(r.get("Account Owner", "")).strip()
                terr = str(r.get("WK CE Territory", "")).strip()
                director = str(r.get("Sales Region Director Assignment", "")).strip()
                if owner and owner not in ("nan", "NaN"):
                    reps.append(f"    {terr} | {owner} | Director: {director}")
            summary[sheet] = "\n".join(reps[:30])  # cap for prompt size
    return summary


# Pre-load data
TERRITORY_MAP = load_territory_map()
INDIVIDUAL_TERRITORIES = load_individual_territories()
ORG_HIERARCHY = load_org_hierarchy()


def build_individual_territory_text():
    lines = ["Lexidrug Individual Territory Assignments (CC Jeff Kelly on all):"]
    for rep, states in INDIVIDUAL_TERRITORIES.items():
        lines.append(f"  {rep}: {', '.join(states)}")
    return "\n".join(lines)


def build_org_hierarchy_text():
    lines = []
    for sheet, content in ORG_HIERARCHY.items():
        if content.strip():
            lines.append(f"\n[{sheet} accounts]")
            lines.append(content)
    return "\n".join(lines)


# ─────────────────────────────────────────────────
# Core routing prompt
# ─────────────────────────────────────────────────

SYSTEM_PROMPT = f"""You are an expert MQL (Marketing Qualified Lead) routing analyst for Wolters Kluwer Clinical Drug Information. Today is {TODAY}.

Your job: given a lead ticket, output a complete ACTION PACKET in the exact format below. No extra commentary outside that format.

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
ROUTING RULES
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

STEP 1 — VALIDITY & INTENT CHECK
- If email looks fake/gibberish or domain strongly contradicts a legitimate healthcare company → SCAM
- If comment mentions login/password/access issues/errors → SUPPORT
- If no comment → NEEDS_CLARIFICATION

STEP 2 — LOCATION GATE
- Not US or Canada → INTERNATIONAL (disqualify, no email to rep)
- Canada, province = Ontario → assign to Susan Roy
- Canada, other province → assign to Cheryl Leger
- Must be healthcare provider (hospital, clinic, pharmacy, academic, gov) to proceed

STEP 3 — COMMERCIAL GATE
- If company's primary business is NOT providing healthcare (e.g. insurance payer, software company, consulting firm, retail) → COMMERCIAL → assign to Michele Leoni (Commercial Segment Manager)
- Exception: retail pharmacy clinic = provider

STEP 4 — PRODUCT ROUTING THRESHOLDS (from 3/31/2026 update)

  UPTODATE:
  - Hospital → any size → IS sales rep for territory (or AM if existing account owner)
  - University/School → any size → IS sales rep for territory
  - Clinic/non-hospital, ≥11 clinician users → IS sales rep for territory
  - Clinic/non-hospital, ≤10 users → INDIVIDUAL SEGMENT → UTD Support (customerservice@uptodate.com)
  - US Federal Government / VA / Military / Tribal → Government team → Justin Schenker (GOVERNMENT_2)
  - Support request → UTD Support (customerservice@uptodate.com)

  LEXICOMP / LEXIDRUG:
  - Hospital with ≥401 beds → AM sales rep for territory + CC CDI product specialist
  - Hospital with ≤400 beds → IS sales rep (DI Specialist: Nikki/Andrew)
  - Clinic with ≥15 clinician users → IS sales rep (DI Specialist: Nikki/Andrew)
  - Clinic with ≤14 users → INDIVIDUAL SEGMENT → lookup state below → individual rep + CC Jeff Kelly
  - University/School → IS sales rep (DI Specialist: Nikki/Andrew)
  - US Federal Government / VA / Military / Tribal → Justin Schenker (GOVERNMENT_2) + CC CDI specialist
  - Support request → cs-cdi-support@wolterskluwer.com

  MEDI-SPAN / PRICE RX:
  - Hospital/Health System/Clinic (US) → DI Specialist (Nikki/Andrew) + Territory rep + CC Ron McBride + Jess Hissem
  - Hospital/Health System/Clinic (Canada) → Medi-Span Specialist (Andrea) + Territory rep + CC Andrea Cheshire
  - Non-healthcare primary business → Commercial Segment Manager (Michele Leoni)
  - University/School → DI Specialist + Territory rep
  - US Federal Government → Government team + CC CDI specialist
  - Home health → Medi-Span Specialist (Andrea) + Territory rep
  - Support → medispan-support@wolterskluwer.com
  - PriceRx: qualify via email first (ask about price type, use case, volume)

  EMMI / PATIENT ENGAGEMENT:
  - New Emmi → Emmi Sales Exec (New Business) + CC Emmi Exec (New Business) + Emmi Director
  - Emmi upsell → Emmi Sales Exec (Renewal) + CC Emmi Exec (Renewal) + Emmi Director
  - EmmiEducate with ≥101 beds → Emmi Sales Exec (New Business)
  - EmmiEducate with ≤100 beds → IS sales rep for territory + CC IS director + Emmi Sales Exec
  - Support → Emmi customer success

STEP 5 — TERRITORY LOOKUP (for IS/AM/Growth/Key reps)
Use the state and city to match the correct territory below.

[Growth and Key Territory Map]
{TERRITORY_MAP}

[Org Hierarchy by Tier]
{build_org_hierarchy_text()}

[Individual Territories for Lexidrug]
{build_individual_territory_text()}

STEP 6 — EXISTING ACCOUNT CHECK
- If lead says they are an existing customer or mentions current subscription → note this in the routing rationale; the account may have an existing Opportunity with a rep already assigned. Flag "CHECK SFDC FOR EXISTING OPPORTUNITY" if this is likely.

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
OUTPUT FORMAT — follow exactly
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

============================================================
MQL ACTION PACKET — [Company Name]
Generated: {TODAY}
============================================================

DECISION: [VALID SALES / INDIVIDUAL SEGMENT / SUPPORT / COMMERCIAL / INTERNATIONAL / SCAM / NEEDS_CLARIFICATION]

ASSIGNED TO: [Full Name — or "UTD Support" / "Lexicomp Support" etc.]
CC: [comma-separated names/emails, or "N/A"]
TERRITORY: [Territory code + name, or "N/A"]
DIRECTOR: [Director name, or "N/A"]

------------------------------------------------------------
SFDC CHECKLIST
------------------------------------------------------------
Use this to update the Task and Lead record in Salesforce.

Task updates:
  □ Task Owner       → [Rep Name]
  □ Product Interest → [Product]
  □ Set Reminder     → {REMINDER_DATE}
  □ Append Qualification Note (below)

Lead record updates:
  □ Lead Status      → [e.g. "Working - Contacted" or "Disqualified by segment marketing"]
  □ Sales Region     → [Region code]
  □ Title Category   → [e.g. Physician, Administrator, Pharmacist, etc.]
  □ Segment          → [Provider / Commercial / Government / Individual]
[IF INDIVIDUAL SEGMENT, add:]
  □ Mark Task as COMPLETED
  □ Lead Status      → "Disqualified by segment marketing"
  □ Reason           → "INDIVIDUAL SEGMENT"
  □ Do NOT change Task owner
[IF NEEDS_CLARIFICATION:]
  □ Set Reminder     → {REMINDER_DATE} (3 days to await reply)
  □ Attempt 2 reminder → {(date.today() + timedelta(days=5)).strftime('%Y-%m-%d')} (if no reply to first email)

------------------------------------------------------------
QUALIFICATION NOTE  (paste into SFDC Task Description/Comment)
------------------------------------------------------------
LEAD SOURCE: [LeadSource from SFDC or "Website form fill"]
COMPANY: [Company Name]
LOCATION: [City, State]
WEBSITE: [if known from comment/email domain, else "Research needed"]
TOTAL PROVIDERS/BEDS: [number from comment, or "Not stated — verify in Definitive"]
GOVERNMENT ENTITY: [Yes / No / Unknown]
CURRENT ACCOUNT STATUS: [New lead / Existing customer — check SFDC]
ASSIGNED REP: [Full Name]
TERRITORY: [Territory name]
ROUTING RATIONALE: [1–2 sentence explanation]
RESEARCH LINKS:
  - Definitive: https://app.definitivehc.com  (search company name)
  - LinkedIn: https://www.linkedin.com/company/[company-slug]
  - Website: [domain if determinable]

------------------------------------------------------------
EMAIL TO REP  (send from SFDC — only for VALID SALES and INDIVIDUAL SEGMENT)
------------------------------------------------------------
To: [rep email if known, else "[rep name]'s SFDC email"]
CC: [cc emails]
Subject: MQL – [Company Name] – [Product]

[Use the appropriate template below:]

[IF single product:]
Hi [Rep First Name],

Please see this MQL with the prospect's specific inquiry below that I'll be assigning over to you. I'm adding some additional information, which will be included in the MQL task in the Comments section.

Thanks,
Syamil

[IF multiple products:]
Hi All,

Please see this MQL with the prospect's specific inquiry below that I'll be assigning over to [Rep Name]. This prospect is interested in [Product List]. I'm adding some additional information, which will be included in the MQL task in the Comments section.

Thanks,
Syamil

------------------------------------------------------------
[IF NEEDS_CLARIFICATION — email to the LEAD]
------------------------------------------------------------
To: [lead email]
Subject: [Product] inquiry

Hello [Lead First Name],

[SELECT THE RIGHT TEMPLATE:]

[IF no comment at all:]
I wanted to make sure you found what you were looking for. Is there anything I can help you with?

[IF unclear whether provider or commercial:]
I received your inquiry regarding [Product]. I can connect you with someone who can assist you, but I need a little more information first.

I was unable to find information on the company you listed [Company] in [City/State]. Can you tell me if the subscriptions for [Product] would be used to provide direct care to patients in an inpatient, ambulatory, or other setting? Or would they be used for another purpose like research, consulting, claims processing, or another use not related to direct patient care?

Knowing this will allow me to connect you to the correct person to assist you as quickly as possible.

[IF unclear user count:]
I received your inquiry regarding [Product]. I can connect you with someone who can assist you, but I need a little information first. Can you tell me if you are inquiring regarding a subscription for your institution, or a personal subscription? If it is for your institution, approximately how many users would need access?

Thank you very much!
[Your signature]

------------------------------------------------------------
ROUTING NOTES (for your records)
------------------------------------------------------------
[Any flags, ambiguities, or things to double-check in SFDC or Definitive]
============================================================
"""


# ─────────────────────────────────────────────────
# Input helpers
# ─────────────────────────────────────────────────

def prompt_interactive():
    print("\n" + "="*60)
    print("MQL LEAD ROUTING ASSISTANT")
    print("="*60)
    print("Enter the lead details below. Press Enter to skip optional fields.\n")

    fields = {}
    fields["name"]           = input("Lead Name:        ").strip()
    fields["email"]          = input("Lead Email:       ").strip()
    fields["company"]        = input("Company Name:     ").strip()
    fields["title"]          = input("Title/Role:       ").strip()
    fields["city_state"]     = input("City, State:      ").strip()
    fields["country"]        = input("Country [US]:     ").strip() or "US"
    fields["product"]        = input("Product Interest: ").strip()
    fields["num_users"]      = input("# Users/Beds:     ").strip()
    fields["comment"]        = input("Comment/Inquiry:\n> ").strip()
    fields["lead_source"]    = input("Lead Source [Website form fill]: ").strip() or "Website form fill"
    fields["existing_customer"] = input("Existing customer? (y/n/unknown) [unknown]: ").strip() or "unknown"

    return fields


def load_from_json(path):
    with open(path) as f:
        return json.load(f)


def fields_to_user_message(fields):
    return f"""Please process this MQL lead:

Name: {fields.get('name', 'Unknown')}
Email: {fields.get('email', 'Unknown')}
Title: {fields.get('title', 'Not provided')}
Company: {fields.get('company', 'Unknown')}
Location: {fields.get('city_state', 'Unknown')}, {fields.get('country', 'US')}
Product Interest: {fields.get('product', 'Not specified')}
Number of Users/Beds: {fields.get('num_users', 'Not stated')}
Lead Source: {fields.get('lead_source', 'Website form fill')}
Existing Customer: {fields.get('existing_customer', 'Unknown')}

Comment/Inquiry:
{fields.get('comment') or '[NO COMMENT PROVIDED]'}
"""


# ─────────────────────────────────────────────────
# Claude call
# ─────────────────────────────────────────────────

def call_claude(user_message: str) -> str:
    api_key = os.getenv("ANTHROPIC_API_KEY")
    if not api_key:
        print("\nERROR: ANTHROPIC_API_KEY not set in .env file")
        sys.exit(1)

    client = anthropic.Anthropic(api_key=api_key)
    model = os.getenv("CLAUDE_MODEL", "claude-sonnet-4-6")

    print(f"\nProcessing with Claude ({model})...\n")

    message = client.messages.create(
        model=model,
        max_tokens=4096,
        system=SYSTEM_PROMPT,
        messages=[{"role": "user", "content": user_message}]
    )
    return message.content[0].text


# ─────────────────────────────────────────────────
# Output
# ─────────────────────────────────────────────────

def save_output(text: str, company: str) -> Path:
    out_dir = Path(__file__).parent / "output"
    out_dir.mkdir(exist_ok=True)
    safe = re.sub(r"[^a-zA-Z0-9_-]", "_", company)[:40]
    filename = out_dir / f"{TODAY}_{safe}.txt"
    with open(filename, "w", encoding="utf-8") as f:
        f.write(text)
    return filename


# ─────────────────────────────────────────────────
# Main
# ─────────────────────────────────────────────────

def run(fields: dict):
    user_msg = fields_to_user_message(fields)
    result = call_claude(user_msg)

    print("\n" + "="*60)
    print(result)
    print("="*60)

    company = fields.get("company", "lead")
    out_path = save_output(result, company)
    print(f"\nSaved to: {out_path}")


def main():
    args = sys.argv[1:]

    if not args:
        fields = prompt_interactive()
        run(fields)

    elif args[0] == "--batch" and len(args) > 1:
        csv_path = args[1]
        df = pd.read_csv(csv_path)
        for _, row in df.iterrows():
            fields = row.to_dict()
            print(f"\n{'='*60}")
            print(f"Processing: {fields.get('company', 'Unknown')}")
            run(fields)

    else:
        # JSON file input
        fields = load_from_json(args[0])
        run(fields)


if __name__ == "__main__":
    main()
