"""Deterministic document corpus for `fetch_doc` (20k-40k tokens each), no LLM needed.

Each document is a stack of themed sections built from sentence templates. Key facts are
planted verbatim at fixed places (e.g. the Q3 launch date deep inside `Q3-plan`) so scenarios
can check answers exactly. Distractor dates sit nearby so a vague answer doesn't pass by luck.
"""

import random
from dataclasses import dataclass, field
from pathlib import Path

# Facts scenarios assert on. Keep in sync with the planted text below.
Q3_LAUNCH_DATE = "September 29, 2026"
Q3_PRODUCT = "Lumen"


@dataclass
class DocSpec:
    doc_id: str
    title: str
    sections: list[str]
    planted: dict[int, list[str]] = field(default_factory=dict)  # section index -> paragraphs
    # Leave headroom above 20k with the local model's tokenizer, rather than relying
    # on the reader's conservative character-based context estimate.
    paragraphs_per_section: int = 14


_SUBJECTS = [
    "The team",
    "Our platform group",
    "The program office",
    "Product marketing",
    "Engineering",
    "The data science pod",
    "Customer success",
    "The design guild",
    "Finance",
    "Leadership",
]
_VERBS = [
    "will prioritize",
    "is expected to deliver",
    "has committed to",
    "plans to review",
    "will coordinate",
    "aims to reduce",
    "will measure",
    "intends to expand",
    "will document",
    "is accountable for",
]
_OBJECTS = [
    "onboarding friction for new workspaces",
    "latency on the reporting dashboards",
    "the quarterly hiring pipeline",
    "migration of legacy accounts",
    "the partner integration backlog",
    "support ticket deflection",
    "accessibility gaps in the editor",
    "the pricing experiment",
    "localization for three new markets",
    "observability across services",
]
_QUALIFIERS = [
    "before the end of the quarter",
    "in close partnership with sales",
    "using the new planning cadence",
    "with weekly check-ins",
    "while keeping headcount flat",
    "subject to the budget review",
    "as outlined in the previous section",
    "with clear owners for each workstream",
    "based on customer interviews",
    "with a rollback plan for every change",
]
_CONNECTORS = [
    "In addition,",
    "As a result,",
    "To support this,",
    "In parallel,",
    "Where possible,",
    "Notably,",
    "For context,",
    "Going forward,",
    "At the same time,",
    "Importantly,",
]


def _sentence(rng: random.Random) -> str:
    return (
        f"{rng.choice(_SUBJECTS)} {rng.choice(_VERBS)} {rng.choice(_OBJECTS)} "
        f"{rng.choice(_QUALIFIERS)}."
    )


def _paragraph(rng: random.Random) -> str:
    sentences = [_sentence(rng)]
    for _ in range(rng.randint(5, 8)):
        sentences.append(
            f"{rng.choice(_CONNECTORS)} {_sentence(rng)[0].lower()}{_sentence(rng)[1:]}"
        )
    return " ".join(sentences)


SPECS = [
    DocSpec(
        "Q3-plan",
        "Q3 2026 Plan: Lumen Launch",
        [
            "Executive summary",
            "Market context",
            "Goals and success metrics",
            "Product scope",
            "Engineering plan",
            "Launch timeline",
            "Go-to-market",
            "Pricing and packaging",
            "Support readiness",
            "Risks and mitigations",
            "Budget",
            "Hiring",
            "Dependencies",
            "Open questions",
        ],
        planted={
            3: [
                f"{Q3_PRODUCT} is our new AI meeting assistant: it joins calls, writes live notes, "
                "and turns decisions into tracked action items across Slack and email. The Q3 "
                "release focuses on three capabilities: live multilingual notes, automatic "
                "follow-up drafts, and a searchable decision log."
            ],
            5: [
                "Milestones for the quarter: private beta opens on August 18, 2026; marketing "
                "kickoff is on September 1, 2026; code freeze is on September 15, 2026.",
                f"The public launch date for {Q3_PRODUCT} is Tuesday, {Q3_LAUNCH_DATE}. "
                "All teams should treat this date as fixed; any change requires sign-off from "
                "the launch steering group.",
                "A post-launch review is scheduled for October 13, 2026.",
            ],
            10: ["The total Q3 budget for the launch is $1.2M, of which $450K is for marketing."],
        },
    ),
    DocSpec(
        "brand-guidelines",
        "Brand Guidelines 2026",
        [
            "Purpose",
            "Voice and tone",
            "Logo usage",
            "Color palette",
            "Typography",
            "Imagery",
            "Iconography",
            "Writing for product",
            "Writing for social",
            "Accessibility",
            "Co-branding",
            "Approvals",
        ],
        planted={
            3: [
                "The primary brand color is Harbor Blue (#1F4E79); "
                "the accent is Signal Coral (#FF6F59)."
            ],
            8: [
                "Social posts use at most three hashtags, placed at the end, "
                "and never include links in the body."
            ],
        },
    ),
    DocSpec(
        "eng-onboarding",
        "Engineering Onboarding Handbook",
        [
            "Welcome",
            "First week",
            "Accounts and access",
            "Development environment",
            "Code review",
            "Testing",
            "Deploying",
            "On-call",
            "Incident response",
            "Architecture overview",
            "Data handling",
            "Learning resources",
        ],
        planted={7: ["New engineers join the on-call rotation after their sixth week."]},
    ),
    DocSpec(
        "security-review",
        "Annual Security Review 2026",
        [
            "Scope",
            "Methodology",
            "Asset inventory",
            "Identity and access",
            "Network",
            "Application security",
            "Third-party risk",
            "Findings",
            "Remediation plan",
            "Metrics",
            "Training",
            "Next review",
        ],
        planted={
            7: [
                "Two high-severity findings remain open; "
                "both are due for remediation by November 30, 2026."
            ]
        },
    ),
]


def render(spec: DocSpec, seed: int = 11) -> str:
    rng = random.Random(f"{seed}-{spec.doc_id}")
    lines = [f"# {spec.title}", ""]
    for i, section in enumerate(spec.sections):
        lines += [f"## {i + 1}. {section}", ""]
        paragraphs = [_paragraph(rng) for _ in range(spec.paragraphs_per_section)]
        for j, planted in enumerate(spec.planted.get(i, [])):
            paragraphs.insert(min(2 + 2 * j, len(paragraphs)), planted)
        for p in paragraphs:
            lines += [p, ""]
    return "\n".join(lines)


def write_all(out_dir: Path) -> list[Path]:
    out_dir.mkdir(parents=True, exist_ok=True)
    paths = []
    for spec in SPECS:
        path = out_dir / f"{spec.doc_id}.md"
        path.write_text(render(spec), encoding="utf-8")
        paths.append(path)
    return paths
