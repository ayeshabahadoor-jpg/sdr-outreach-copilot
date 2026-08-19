#!/usr/bin/env python3
"""SDR Outreach Copilot.

Turn a single prospect's details into a complete, personalized, ready-to-send
multi-touch outreach sequence for Intercom's Sales Development Reps (SDRs).

For each prospect the tool produces a 4-touch sequence:
    1. Cold email        (subject line + <90-word body)
    2. LinkedIn note      (<300 chars connection request)
    3. Follow-up "bump"   (<70-word email)
    4. Cold-call opener   (3-4 conversational sentences)

Every touch opens by referencing the prospect's intent signal, ties to a pain
that is specific to the prospect's role, and includes exactly one real
Intercom/Fin proof point.

Two generation modes are available:
    * Offline (default) : a deterministic personalization engine. No API key,
                          no network, no external dependencies. Always works.
    * --ai              : drafts via the Anthropic API when ANTHROPIC_API_KEY is
                          set. If the key is missing or the call fails, the tool
                          prints a clear notice and falls back to the offline
                          engine. It never crashes.

Usage examples:
    python sdr_outreach_copilot.py                      # interactive, offline
    python sdr_outreach_copilot.py --file prospects.csv # batch from CSV
    python sdr_outreach_copilot.py --file leads.json --ai
    python sdr_outreach_copilot.py --help

Author: Intercom SDR team (AI Fluency Level 1 certification project)
"""

from __future__ import annotations

import argparse
import csv
import json
import os
import re
import sys
import textwrap
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

# ---------------------------------------------------------------------------
# Domain knowledge: proof points and role -> pain mapping
# ---------------------------------------------------------------------------

#: Real Intercom / Fin proof points. Keyed so the engine can pick the most
#: relevant one for a given prospect. Each value is a ready-to-drop-in clause.
PROOF_POINTS: Dict[str, str] = {
    "fin_resolution": (
        "Fin resolves 59% of support conversations instantly and averages "
        "67% resolution across 12,000+ businesses"
    ),
    "copilot_efficiency": (
        "Intercom's AI-first Helpdesk with Copilot drove a 31% jump in agent "
        "efficiency at Lightspeed"
    ),
    "fin_lead_qualification": (
        "Fin qualifies inbound leads conversationally and books meetings "
        "straight into your calendar via Calendly or Chili Piper"
    ),
}

#: Ordered role -> pain rules. The first matching keyword wins, so more
#: specific roles are listed before broader ones.
ROLE_PAIN_RULES: List[Dict[str, Any]] = [
    {
        "keywords": ("founder", "ceo", "co-founder", "cofounder", "owner"),
        "label": "Founder/CEO",
        "peer": "founders",
        "pain": "scaling support without scaling headcount",
        "default_proof": "fin_resolution",
    },
    {
        "keywords": ("support", "cx", "customer experience", "customer service",
                     "customer success", "success"),
        "label": "Support/CX lead",
        "peer": "support and CX leaders",
        "pain": "ticket volume outpacing your headcount",
        "default_proof": "fin_resolution",
    },
    {
        "keywords": ("ops", "operations", "revops", "rev ops"),
        "label": "Ops",
        "peer": "ops leaders",
        "pain": "repetitive manual work eating your team's time",
        "default_proof": "copilot_efficiency",
    },
    {
        "keywords": ("vp", "vice president", "director", "head", "chief"),
        "label": "VP/Director/Head",
        "peer": "leaders in your seat",
        "pain": "hitting efficiency targets without hurting CSAT",
        "default_proof": "copilot_efficiency",
    },
    {
        "keywords": ("marketing", "demand", "growth", "sales", "revenue"),
        "label": "Marketing/Revenue",
        "peer": "revenue leaders",
        "pain": "converting inbound interest before it goes cold",
        "default_proof": "fin_lead_qualification",
    },
]

#: Fallback pain when a role doesn't match any rule above.
DEFAULT_PAIN = "doing more with the team you already have"
DEFAULT_PEER = "teams like yours"
DEFAULT_PROOF_KEY = "fin_resolution"

#: Product focus -> proof point preference. Lets product_focus override the
#: role-derived proof choice when it is clearly set.
PRODUCT_PROOF_MAP: Dict[str, str] = {
    "fin": "fin_resolution",
    "helpdesk": "copilot_efficiency",
    "copilot": "copilot_efficiency",
}

SENDER_PLACEHOLDER = "{{Your Name}}"


# ---------------------------------------------------------------------------
# Prospect model
# ---------------------------------------------------------------------------

@dataclass
class Prospect:
    """A single sales prospect. Any field may be missing; defaults fill in."""

    name: str = "there"
    company: str = "your company"
    role: str = "your team"
    industry: str = "your industry"
    intent_signal: str = "showed interest in Intercom"
    product_focus: str = "Fin"

    #: Populated during resolution; not user-supplied.
    pain: str = field(default="", init=False)
    role_label: str = field(default="", init=False)
    peer: str = field(default="", init=False)
    proof_key: str = field(default="", init=False)

    @classmethod
    def from_dict(cls, raw: Dict[str, Any]) -> "Prospect":
        """Build a Prospect from a loose dict, tolerating missing/blank fields.

        Recognizes several common column aliases (e.g. ``title`` for role) so
        real-world CSV/JSON files import cleanly.
        """
        def pick(*keys: str) -> Optional[str]:
            for key in keys:
                for candidate in (key, key.lower(), key.upper(), key.title()):
                    if candidate in raw:
                        value = raw[candidate]
                        if value is not None and str(value).strip():
                            return str(value).strip()
            return None

        prospect = cls(
            name=pick("name", "first_name", "full_name", "contact") or "there",
            company=pick("company", "organization", "org", "account")
            or "your company",
            role=pick("role", "title", "job_title", "position") or "your team",
            industry=pick("industry", "vertical", "sector") or "your industry",
            intent_signal=pick("intent_signal", "intent", "signal", "trigger")
            or "showed interest in Intercom",
            product_focus=pick("product_focus", "product", "focus") or "Fin",
        )
        prospect.resolve()
        return prospect

    def resolve(self) -> None:
        """Derive role label, pain point, and best proof point for this prospect."""
        role_lower = self.role.lower()
        matched = None
        for rule in ROLE_PAIN_RULES:
            if any(keyword in role_lower for keyword in rule["keywords"]):
                matched = rule
                break

        if matched:
            self.role_label = matched["label"]
            self.peer = matched["peer"]
            self.pain = matched["pain"]
            self.proof_key = matched["default_proof"]
        else:
            self.role_label = self.role.title()
            self.peer = DEFAULT_PEER
            self.pain = DEFAULT_PAIN
            self.proof_key = DEFAULT_PROOF_KEY

        # Product focus, when explicit, overrides the role-derived proof choice.
        focus_key = self.product_focus.strip().lower()
        if focus_key in PRODUCT_PROOF_MAP:
            self.proof_key = PRODUCT_PROOF_MAP[focus_key]

    @property
    def proof_point(self) -> str:
        """The chosen proof point clause for this prospect."""
        return PROOF_POINTS.get(self.proof_key, PROOF_POINTS[DEFAULT_PROOF_KEY])

    @property
    def first_name(self) -> str:
        """Best-effort first name for greetings."""
        return self.name.split()[0] if self.name.strip() else "there"


# ---------------------------------------------------------------------------
# Outreach sequence model
# ---------------------------------------------------------------------------

@dataclass
class OutreachSequence:
    """A complete 4-touch outreach sequence for one prospect."""

    email_subject: str
    email_body: str
    linkedin_note: str
    follow_up: str
    call_opener: str
    generator: str  # "offline" or "ai"

    def to_markdown(self, prospect: Prospect) -> str:
        """Render the sequence as a self-contained Markdown document.

        Built line-by-line (no source indentation) so multi-line touch bodies
        never get interpreted as Markdown code blocks.
        """
        lines = [
            f"# Outreach sequence — {prospect.name}, {prospect.company}",
            "",
            "| Field | Value |",
            "| --- | --- |",
            f"| Name | {prospect.name} |",
            f"| Company | {prospect.company} |",
            f"| Role | {prospect.role} ({prospect.role_label}) |",
            f"| Industry | {prospect.industry} |",
            f"| Intent signal | {prospect.intent_signal} |",
            f"| Product focus | {prospect.product_focus} |",
            f"| Role pain targeted | {prospect.pain} |",
            f"| Generated by | {self.generator} engine |",
            "",
            "---",
            "",
            "## 1. Cold email",
            "",
            f"**Subject:** {self.email_subject}",
            "",
            self.email_body,
            "",
            "---",
            "",
            "## 2. LinkedIn connection note",
            "",
            self.linkedin_note,
            "",
            "---",
            "",
            '## 3. Follow-up email ("bump")',
            "",
            self.follow_up,
            "",
            "---",
            "",
            "## 4. Cold-call opener",
            "",
            self.call_opener,
            "",
        ]
        return "\n".join(lines)


# ---------------------------------------------------------------------------
# Offline deterministic personalization engine
# ---------------------------------------------------------------------------

def _clamp_words(text: str, max_words: int) -> str:
    """Trim ``text`` to at most ``max_words`` words without cutting mid-sentence
    awkwardly. Used as a safety net so generated copy always respects limits."""
    words = text.split()
    if len(words) <= max_words:
        return text
    return " ".join(words[:max_words]).rstrip(",;: ") + "."


def generate_offline(prospect: Prospect) -> OutreachSequence:
    """Generate a full 4-touch sequence using the deterministic offline engine.

    The copy is templated but personalized: every touch references the intent
    signal, ties to the role-specific pain, and uses exactly one proof point.
    """
    fn = prospect.first_name
    company = prospect.company
    role = prospect.role
    peer = prospect.peer
    proof = prospect.proof_point
    pain = prospect.pain
    signal = prospect.intent_signal

    # --- 1. Cold email ------------------------------------------------------
    email_subject = f"{company} + Fin: {signal.rstrip('.')}"
    email_body = _clamp_words(
        textwrap.fill(
            f"Hi {fn}, I saw you {signal}. As {role} at {company}, you're "
            f"likely feeling {pain} — that's exactly where teams turn to "
            f"Intercom. {proof}. "
            f"Worth a quick 15-minute look at what that could mean for "
            f"{company}? Happy to share numbers from {peer}.\n\n"
            f"Best,\n{SENDER_PLACEHOLDER}",
            width=1000,
        ),
        max_words=89,
    )

    # --- 2. LinkedIn connection note ---------------------------------------
    # Try the full note first; if it exceeds 300 chars, drop to a compact
    # variant that still leads with the intent signal and keeps the SAME
    # proof point rather than swapping in a generic one.
    linkedin_note = (
        f"Hi {fn} — noticed you {signal}. Most {peer} I talk to are wrestling "
        f"with {pain}. {proof}. Would love to connect and swap notes. "
        f"— {SENDER_PLACEHOLDER}"
    )
    if len(linkedin_note) > 300:
        linkedin_note = (
            f"Hi {fn} — saw you {signal}. Given {pain}, Intercom may be worth a "
            f"look: {proof}. Let's connect. — {SENDER_PLACEHOLDER}"
        )
    if len(linkedin_note) > 300:
        # Last resort: keep signal + proof only, trimmed to the hard limit.
        linkedin_note = (
            f"Hi {fn} — saw you {signal}. {proof}. "
            f"Let's connect. — {SENDER_PLACEHOLDER}"
        )[:299].rstrip()

    # --- 3. Follow-up "bump" ------------------------------------------------
    follow_up = _clamp_words(
        f"Hi {fn}, bumping this in case it slipped by. Since you {signal}, I "
        f"figured {pain} is top of mind. {proof}. "
        f"Open to a quick chat this week?\n\n{SENDER_PLACEHOLDER}",
        max_words=69,
    )

    # --- 4. Cold-call opener ------------------------------------------------
    call_opener = (
        f"Hi {fn}, this is {SENDER_PLACEHOLDER} from Intercom — I know I'm "
        f"catching you out of the blue, so I'll be quick. I saw that you "
        f"{signal}, and I work with {peer} who are dealing with {pain}. "
        f"The reason I'm reaching out: {proof}. "
        f"Do you have 30 seconds for me to explain why I called?"
    )

    return OutreachSequence(
        email_subject=email_subject,
        email_body=email_body,
        linkedin_note=linkedin_note,
        follow_up=follow_up,
        call_opener=call_opener,
        generator="offline",
    )


# ---------------------------------------------------------------------------
# AI generation (Anthropic API) with graceful fallback
# ---------------------------------------------------------------------------

def build_llm_prompt(prospect: Prospect) -> str:
    """Build the instruction prompt for the Anthropic API.

    The prompt supplies full role context, a strict JSON output schema, and the
    word-count constraints for each touch. It is deliberately explicit so the
    model returns machine-parseable output that mirrors the offline engine.
    """
    return textwrap.dedent(
        f"""\
        You are an elite Intercom Sales Development Rep writing a personalized,
        4-touch outbound sequence for one prospect. Write copy that is warm,
        specific, and human — never generic or spammy.

        PROSPECT CONTEXT
        - Name: {prospect.name}
        - Company: {prospect.company}
        - Role / title: {prospect.role} (category: {prospect.role_label})
        - Industry: {prospect.industry}
        - Intent signal (what they just did): {prospect.intent_signal}
        - Product focus: {prospect.product_focus}
        - Role-specific pain to target: {prospect.pain}

        PROOF POINTS (use EXACTLY ONE per touch, pick the most relevant; the
        suggested best fit for this prospect is listed first):
        1. {prospect.proof_point}.
        2. {PROOF_POINTS['fin_resolution']}.
        3. {PROOF_POINTS['copilot_efficiency']}.
        4. {PROOF_POINTS['fin_lead_qualification']}.

        HARD RULES FOR EVERY TOUCH
        - Open by referencing the intent signal: "{prospect.intent_signal}".
        - Tie explicitly to this role's pain: "{prospect.pain}".
        - Include exactly ONE proof point (a real number/fact from the list).
        - Use the literal token {SENDER_PLACEHOLDER} as the sender's name.
        - No made-up statistics. No emojis. Plain, confident language.

        LENGTH CONSTRAINTS
        - cold_email_body: fewer than 90 words.
        - linkedin_note: fewer than 300 characters.
        - follow_up: fewer than 70 words.
        - call_opener: 3 to 4 conversational sentences.

        OUTPUT FORMAT
        Return ONLY valid minified JSON, no markdown fences, matching exactly:
        {{
          "email_subject": "string",
          "email_body": "string",
          "linkedin_note": "string",
          "follow_up": "string",
          "call_opener": "string"
        }}
        """
    )


def generate_ai(prospect: Prospect) -> OutreachSequence:
    """Generate a sequence via the Anthropic API.

    Raises an exception on any failure (missing key, missing SDK, network or
    parsing error). Callers are expected to catch and fall back to the offline
    engine — this function never silently degrades.
    """
    api_key = os.environ.get("ANTHROPIC_API_KEY")
    if not api_key:
        raise RuntimeError("ANTHROPIC_API_KEY is not set")

    try:
        import anthropic  # type: ignore
    except ImportError as exc:  # pragma: no cover - depends on environment
        raise RuntimeError(
            "the 'anthropic' package is not installed (pip install anthropic)"
        ) from exc

    client = anthropic.Anthropic(api_key=api_key)
    prompt = build_llm_prompt(prospect)

    message = client.messages.create(
        model="claude-sonnet-5",
        max_tokens=1024,
        messages=[{"role": "user", "content": prompt}],
    )

    # Concatenate any text blocks returned by the model.
    raw = "".join(
        block.text for block in message.content if getattr(block, "type", "") == "text"
    ).strip()

    # Be forgiving if the model wrapped the JSON in fences or prose.
    match = re.search(r"\{.*\}", raw, re.DOTALL)
    if not match:
        raise ValueError(f"could not find JSON in model response: {raw[:200]!r}")
    data = json.loads(match.group(0))

    required = {"email_subject", "email_body", "linkedin_note", "follow_up",
                "call_opener"}
    missing = required - data.keys()
    if missing:
        raise ValueError(f"model response missing fields: {sorted(missing)}")

    return OutreachSequence(
        email_subject=str(data["email_subject"]).strip(),
        email_body=str(data["email_body"]).strip(),
        linkedin_note=str(data["linkedin_note"]).strip(),
        follow_up=str(data["follow_up"]).strip(),
        call_opener=str(data["call_opener"]).strip(),
        generator="ai",
    )


def generate_sequence(prospect: Prospect, use_ai: bool) -> OutreachSequence:
    """Generate a sequence, honoring ``use_ai`` and falling back gracefully."""
    if not use_ai:
        return generate_offline(prospect)

    try:
        return generate_ai(prospect)
    except Exception as exc:  # noqa: BLE001 - intentional broad fallback
        print(
            f"  [notice] AI generation unavailable ({exc}); "
            f"falling back to the offline engine.",
            file=sys.stderr,
        )
        return generate_offline(prospect)


# ---------------------------------------------------------------------------
# Input loading
# ---------------------------------------------------------------------------

def load_prospects_from_file(path: Path) -> List[Prospect]:
    """Load prospects from a CSV or JSON file. Never crashes on missing fields."""
    if not path.exists():
        raise FileNotFoundError(f"input file not found: {path}")

    suffix = path.suffix.lower()
    raw_rows: List[Dict[str, Any]]

    if suffix == ".json":
        with path.open(encoding="utf-8") as handle:
            data = json.load(handle)
        if isinstance(data, dict):
            # Accept either a single prospect or {"prospects": [...]}.
            data = data.get("prospects", [data])
        if not isinstance(data, list):
            raise ValueError("JSON must be a list of prospects or a single object")
        raw_rows = [row for row in data if isinstance(row, dict)]
    elif suffix == ".csv":
        with path.open(newline="", encoding="utf-8") as handle:
            reader = csv.DictReader(handle)
            raw_rows = [dict(row) for row in reader]
    else:
        raise ValueError(f"unsupported file type '{suffix}' (use .csv or .json)")

    if not raw_rows:
        raise ValueError(f"no prospects found in {path}")

    return [Prospect.from_dict(row) for row in raw_rows]


def prompt_for_prospect() -> Prospect:
    """Interactively prompt the user for one prospect's fields."""
    print("\nEnter prospect details (press Enter to accept the default).\n")

    def ask(label: str, default: str) -> str:
        try:
            answer = input(f"  {label} [{default}]: ").strip()
        except EOFError:
            answer = ""
        return answer or default

    raw = {
        "name": ask("Name", "there"),
        "company": ask("Company", "your company"),
        "role": ask("Role / title", "Head of Support"),
        "industry": ask("Industry", "SaaS"),
        "intent_signal": ask("Intent signal", "requested a Fin demo"),
        "product_focus": ask("Product focus (Fin/Helpdesk/Copilot)", "Fin"),
    }
    return Prospect.from_dict(raw)


# ---------------------------------------------------------------------------
# Output
# ---------------------------------------------------------------------------

def safe_filename(prospect: Prospect) -> str:
    """Build a filesystem-safe Markdown filename for a prospect."""
    base = f"{prospect.name}_{prospect.company}".lower()
    base = re.sub(r"[^a-z0-9]+", "_", base).strip("_")
    return f"{base or 'prospect'}.md"


def save_markdown(prospect: Prospect, sequence: OutreachSequence,
                  output_dir: Path) -> Path:
    """Write the sequence to ``output_dir`` and return the path written."""
    output_dir.mkdir(parents=True, exist_ok=True)
    path = output_dir / safe_filename(prospect)
    path.write_text(sequence.to_markdown(prospect), encoding="utf-8")
    return path


def print_sequence(prospect: Prospect, sequence: OutreachSequence) -> None:
    """Pretty-print a sequence to the terminal."""
    rule = "=" * 72
    print(f"\n{rule}")
    print(f"  {prospect.name}  |  {prospect.company}  |  {prospect.role}")
    print(f"  Intent: {prospect.intent_signal}  |  Pain: {prospect.pain}")
    print(f"  Engine: {sequence.generator}")
    print(rule)

    print("\n--- 1. COLD EMAIL ---")
    print(f"Subject: {sequence.email_subject}\n")
    print(sequence.email_body)

    print("\n--- 2. LINKEDIN NOTE ---")
    print(f"{sequence.linkedin_note}   ({len(sequence.linkedin_note)} chars)")

    print("\n--- 3. FOLLOW-UP (BUMP) ---")
    print(sequence.follow_up)

    print("\n--- 4. COLD-CALL OPENER ---")
    print(sequence.call_opener)


def print_summary(rows: List[Dict[str, str]]) -> None:
    """Print a compact summary table of everything generated."""
    if not rows:
        return
    headers = ("Prospect", "Company", "File saved")
    widths = [len(h) for h in headers]
    for row in rows:
        widths[0] = max(widths[0], len(row["prospect"]))
        widths[1] = max(widths[1], len(row["company"]))
        widths[2] = max(widths[2], len(row["file"]))

    def fmt(cols: tuple) -> str:
        return " | ".join(str(c).ljust(widths[i]) for i, c in enumerate(cols))

    print("\n" + "=" * 72)
    print("  SUMMARY")
    print("=" * 72)
    print(fmt(headers))
    print("-+-".join("-" * w for w in widths))
    for row in rows:
        print(fmt((row["prospect"], row["company"], row["file"])))
    print(f"\n{len(rows)} sequence(s) generated. Files saved under ./output/")


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def build_arg_parser() -> argparse.ArgumentParser:
    """Construct the argparse CLI."""
    parser = argparse.ArgumentParser(
        prog="sdr_outreach_copilot.py",
        description=(
            "Turn prospect details into a personalized 4-touch Intercom "
            "outreach sequence (cold email, LinkedIn note, follow-up, call opener)."
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=textwrap.dedent(
            """\
            examples:
              %(prog)s                        interactive mode (offline engine)
              %(prog)s --file prospects.csv   batch mode from a CSV
              %(prog)s --file leads.json --ai  batch mode, AI drafts w/ fallback
            """
        ),
    )
    parser.add_argument(
        "--file", "-f", type=Path, default=None,
        help="path to a CSV or JSON file of prospects (batch mode)",
    )
    parser.add_argument(
        "--ai", action="store_true",
        help="use the Anthropic API for drafts (needs ANTHROPIC_API_KEY); "
             "falls back to the offline engine on any failure",
    )
    parser.add_argument(
        "--output-dir", "-o", type=Path, default=Path("output"),
        help="directory to save Markdown files (default: ./output)",
    )
    return parser


def main(argv: Optional[List[str]] = None) -> int:
    """Entry point. Returns a process exit code."""
    parser = build_arg_parser()
    args = parser.parse_args(argv)

    print("=" * 72)
    print("  SDR OUTREACH COPILOT  —  Intercom")
    print("  Personalized 4-touch sequences in seconds")
    print("=" * 72)

    # Gather prospects.
    try:
        if args.file:
            print(f"\nLoading prospects from {args.file} ...")
            prospects = load_prospects_from_file(args.file)
            print(f"Loaded {len(prospects)} prospect(s).")
        else:
            prospects = [prompt_for_prospect()]
    except (FileNotFoundError, ValueError, json.JSONDecodeError) as exc:
        print(f"\nError loading prospects: {exc}", file=sys.stderr)
        return 1

    if args.ai:
        print("\nAI mode requested. Will use the Anthropic API if available, "
              "otherwise fall back to the offline engine.")

    # Generate + output.
    summary_rows: List[Dict[str, str]] = []
    for prospect in prospects:
        sequence = generate_sequence(prospect, use_ai=args.ai)
        print_sequence(prospect, sequence)
        saved = save_markdown(prospect, sequence, args.output_dir)
        summary_rows.append({
            "prospect": prospect.name,
            "company": prospect.company,
            "file": str(saved),
        })

    print_summary(summary_rows)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
