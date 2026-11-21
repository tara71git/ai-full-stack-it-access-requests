"""Ordered Northstar review policy. Routes create human work only."""

import re

from intake.extraction import SCALAR_FIELDS, filename_mismatch


ACTIONS = {
    "inspect-message": "Inspect the source message for attempted instruction overrides.",
    "confirm-withdrawal": "Confirm the withdrawal with the requester.",
    "clarify": "Contact the requester to clarify the recorded issues.",
    "review-risk-then-owner": "Review risk and forward a recommendation to the system owner for a decision.",
    "review-request": "Have the system owner review the access request.",
    "retry-extraction": "Retry extraction after addressing the recorded error.",
}
REQUIRED_ACCESS_FACTS = tuple(field for field in SCALAR_FIELDS if field != "kind")
OVERRIDE_MARKERS = re.compile(
    r"\b(?:system override|ignore (?:all |the )?(?:previous|prior|system) instructions|"
    r"highest.priority system instruction|bypass (?:the )?approval rules|"
    r"(?:display|reveal|print) (?:the )?api key|mark (?:this |the )?request as approved)\b",
    re.IGNORECASE,
)
OVERRIDE_ISSUE = re.compile(r"instruction.override|prompt injection|attempted override", re.IGNORECASE)
CONFLICT_ISSUE = re.compile(r"\b(conflict\w*|contradict\w*)\b", re.IGNORECASE)


def review(status, owner, action, manager=None):
    return {
        "status": status,
        "owner": owner,
        "next_action": action,
        "action_description": ACTIONS[action],
        "manager_contact": manager,
        "awaiting_human_review": status != "error",
    }


def add_issue(facts, issue):
    if issue not in facts["issues"]:
        facts["issues"].append(issue)


def route(source, facts, *, security_environments=("production",),
          security_permissions=("write", "admin")):
    issues = facts["issues"]
    for field in SCALAR_FIELDS:
        if isinstance(facts[field], str) and not facts[field].strip():
            facts[field] = None
        if facts[field] is None and not any(
            re.search(r"\b" + field.replace("_", r"[_ ]") + r"\b", issue, re.IGNORECASE)
            for issue in issues
        ):
            add_issue(facts, f"{field}: missing or unresolved in message")

    mismatch = filename_mismatch(source.document_id, facts["request_id"])
    if mismatch:
        add_issue(facts, mismatch)
    start, end = facts["start_date"], facts["end_date"]
    reversed_dates = bool(start and end and start > end)
    if reversed_dates:
        add_issue(facts, "start_date is after end_date")

    message = source.message
    match = OVERRIDE_MARKERS.search(message)
    manager = facts["manager"]
    flagged = any(OVERRIDE_ISSUE.search(issue) for issue in issues)
    if match or flagged:
        if match:
            line_start = message.rfind("\n", 0, match.start()) + 1
            start = match.start() if match[0].lower() == "system override" else line_start
            end = message.find("\n", match.end())
            quote = message[start:end if end != -1 else len(message)].strip()
        else:
            quote = message
        if quote and quote not in facts["supporting_quotes"]:
            facts["supporting_quotes"].append(quote)
        add_issue(facts, "instruction-override attempt requires source inspection")
        return review("review", "IT Service Desk", "inspect-message", manager)
    if facts["kind"] == "withdrawal" and facts["request_id"] and facts["request_date"]:
        return review("review", "IT Service Desk", "confirm-withdrawal", manager)
    if (facts["kind"] not in ("new", "change") or mismatch or reversed_dates
            or any(CONFLICT_ISSUE.search(issue) for issue in issues)
            or any(facts[field] is None for field in REQUIRED_ACCESS_FACTS)):
        return review("needs-information", "IT Service Desk", "clarify", manager)
    if facts["environment"] in security_environments or facts["permission"] in security_permissions:
        return review("review", "Security Review", "review-risk-then-owner", manager)
    return review("review", "System Owner", "review-request", manager)
