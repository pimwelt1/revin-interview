"""Extracted proposals are checked here before they become usable customer facts."""

import json
import re

from voice_agent.agent.services.maps import AddressError, Maps

DETAILS = {
    "customer_name": "The caller's full name. Combine a supplied last name with their earlier first name.",
    "phone": "Callback number, with digits exactly as supplied, even when incomplete.",
    "address": "Street number and street name only. No apartment or unit, city, state, or ZIP code.",
    "city": "City explicitly supplied by the caller. Never infer it from a street or ZIP code.",
    "zip_code": "ZIP code explicitly supplied by the caller, as a string preserving leading zeros.",
    "issue": "One short line about the equipment and what is wrong.",
}
ADDRESS_FIELDS = ("address", "city", "zip_code")
LABELS = {**{name: name.replace("_", " ") for name in DETAILS}, "address": "street address", "zip_code": "ZIP code"}

DETAILS_FORMAT = {
    "type": "json_schema",
    "json_schema": {
        "name": "customer_details",
        "strict": True,
        "schema": {
            "title": "customer_details",
            "type": "object",
            "properties": {
                name: {"type": ["string", "null"], "description": description} for name, description in DETAILS.items()
            },
            "required": list(DETAILS),
            "additionalProperties": False,
        },
    },
}


def read_details(text: str) -> dict[str, str]:
    """Null means no update; an empty string explicitly withdraws a previously supplied value."""
    data = json.loads(text)
    if not isinstance(data, dict) or set(data) - DETAILS.keys():
        raise ValueError("Invalid customer details object")
    if any(value is not None and not isinstance(value, str) for value in data.values()):
        raise ValueError("Customer details must be strings or null")
    return {name: " ".join(value.split()) for name, value in data.items() if value is not None}


def normalize_phone(value: str) -> str:
    """A US callback number, or an empty string when its format is invalid."""
    if not re.fullmatch(r"\+?[0-9 ()\-.]+", value.strip()):
        return ""
    digits = re.sub(r"[^0-9]", "", value)
    if len(digits) == 10 and not value.strip().startswith("+"):
        return "+1" + digits
    if len(digits) == 11 and digits.startswith("1"):
        return "+" + digits
    return ""

def problems(proposed: dict[str, str]) -> dict[str, str]:
    """Validation reasons, not separate spoken prompts."""
    errors = {}
    name = proposed.get("customer_name", "")
    if name:
        if any(not (c.isalpha() or c in " '-’.") for c in name):
            errors["customer_name"] = "the name contains characters other than letters or name punctuation"
        elif len(name.split()) < 2 or any(not any(c.isalpha() for c in part) for part in name.split()):
            errors["customer_name"] = "the full name is incomplete; the last name is missing"
    phone = proposed.get("phone", "")
    if phone and not normalize_phone(phone):
        digits = re.sub(r"[^0-9]", "", phone)
        errors["phone"] = (
            f"the phone number has {len(digits)} digits; a US callback number needs 10 digits or 11 beginning with 1, "
            "with no letters or extension"
        )
    address = proposed.get("address", "")
    if address and not re.match(r"^[0-9]+[A-Za-z]?(?:-[0-9]+)?\s+.*[^\W\d_]", address, re.UNICODE):
        errors["address"] = "the street address needs a street number followed by a street name"
    city = proposed.get("city", "")
    if city and (not any(c.isalpha() for c in city) or any(not (c.isalpha() or c in " '-’.") for c in city)):
        errors["city"] = "the city needs a city name, without a ZIP code or street address"
    zip_code = proposed.get("zip_code", "")
    if zip_code and not re.fullmatch(r"[0-9]{5}(?:-[0-9]{4})?", zip_code):
        errors["zip_code"] = "the ZIP code needs five digits, optionally followed by a hyphen and four digits"
    return errors


def full_address(details: dict[str, str]) -> str:
    return ", ".join(details[name] for name in ADDRESS_FIELDS if details.get(name))


def address_corrected(proposed: dict[str, str], facts: dict[str, str], pending: dict[str, str]) -> bool:
    """True when this turn supplies an address part that differs from the one already held."""
    return any(
        value != (pending.get(name) if name in pending else facts.get(name, ""))
        for name, value in proposed.items()
        if name in ADDRESS_FIELDS
    )


def settle_address(facts: dict, pending: dict, errors: dict, local_errors: dict, maps: Maps) -> None:
    """Accept the three address parts together, and only once the lookup confirms them as one place."""
    if not any(name in pending for name in ADDRESS_FIELDS):
        return
    for name in ADDRESS_FIELDS:
        errors.pop(name, None)
    errors.update({name: reason for name, reason in local_errors.items() if name in ADDRESS_FIELDS})
    if not all(pending.get(name) for name in ADDRESS_FIELDS) or any(name in errors for name in ADDRESS_FIELDS):
        return
    try:
        place = maps.validate(pending["address"], pending["city"], pending["zip_code"])
    except AddressError as error:
        errors["address"] = str(error)
    else:
        facts.update(address=place.address, city=place.city, zip_code=place.zip_code)
        for name in ADDRESS_FIELDS:
            pending.pop(name, None)


def accept_details(state: dict, proposed: dict[str, str], maps: Maps) -> dict:
    """Validate new proposals and pending address retries without changing the supplied state.

    Three passes over copies of the call's three detail dicts: park every proposal in `pending`, then
    promote the individually valid fields into `facts`, then settle the address as a whole. A field is
    usable only once it reaches `facts`; anything left in `pending` carries a reason in `errors`.
    """
    facts = dict(state.get("facts", {}))
    pending = dict(state.get("pending_details", {}))
    errors = dict(state.get("detail_errors", {}))
    proposed = {name: " ".join(value.split()) for name, value in proposed.items() if name in DETAILS}
    changed_address = address_corrected(proposed, facts, pending)

    # 1. Park the proposals. A corrected address moves as a whole, so an old one is never left usable.
    if changed_address:
        pending.update({name: pending.get(name, facts.get(name, "")) for name in ADDRESS_FIELDS})
        for name in ADDRESS_FIELDS:
            facts.pop(name, None)
            errors.pop(name, None)
    for name, value in proposed.items():
        if name in ADDRESS_FIELDS and not changed_address:
            continue
        facts.pop(name, None)
        errors.pop(name, None)
        pending[name] = value

    # 2. Promote the fields that stand on their own; the address waits for its lookup in pass 3.
    local_errors = problems(pending)
    for name in tuple(pending):
        if name in ADDRESS_FIELDS:
            continue
        if name in local_errors:
            errors[name] = local_errors[name]
        elif pending[name]:
            facts[name] = normalize_phone(pending[name]) if name == "phone" else pending[name]
            pending.pop(name)
            errors.pop(name, None)

    # 3. Settle the address, retrying a pending one even when this turn changed nothing.
    settle_address(facts, pending, errors, local_errors, maps)

    update = {"facts": facts, "pending_details": pending, "detail_errors": errors}
    if changed_address:
        update["offered_slots"] = []
    return update


def details_needed(state: dict) -> str:
    """The one feedback instruction shared by the agent prompt and every tool guard, or "" when nothing is owed."""
    facts = state.get("facts", {})
    supplied = {**facts, **state.get("pending_details", {})}
    missing = [LABELS[name] for name in DETAILS if not supplied.get(name)]
    reasons = [f"still missing {', '.join(missing)}"] if missing else []
    reasons += list(state.get("detail_errors", {}).values())
    if not reasons and any(not facts.get(name) for name in DETAILS):
        reasons.append("the supplied details are still awaiting validation")
    if not reasons:
        return ""
    return (
        "; ".join(reasons)
        + ". Ask only for the missing or invalid parts, one question at a time, briefly explaining the reason. "
        "Keep the parts already supplied. If address checking is unavailable, explain the temporary problem "
        "instead of asking the caller to repeat correct information. Do not act on these details yet."
    )


def describe(state: dict) -> str:
    accepted = "; ".join(f"{LABELS[name]}: {value}" for name, value in state.get("facts", {}).items()) or "nothing yet"
    pending = "; ".join(f"{LABELS[name]}: {value}" for name, value in state.get("pending_details", {}).items() if value)
    return (
        f"Accepted details: {accepted}."
        + (f" Pending, not accepted: {pending}." if pending else "")
        + " "
        + details_needed(state)
    )
