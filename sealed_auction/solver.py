"""Menu validation and winner determination for the joint package tender.

Given per-resource supply and each buyer's menu of mutually exclusive
package options, choose at most one option per buyer maximising the sum of
package prices subject to resource capacity.  Ties are broken by the
lexicographically smallest ascending tuple of selected option labels.

The search is a dynamic program over resource-usage vectors.  Keeping only
the best ``(value, labels)`` partial solution per usage vector is exact
here: the extensions available from a usage vector are identical for every
partial reaching it, prices are positive, labels are unique, and two
partials in the same vector can never be prefix-comparable (every option
consumes at least one unit, so a strict prefix would use strictly fewer
resources).  Merging the same future options into both therefore preserves
their lexicographic order, so a dominated partial can never come back.
"""

from __future__ import annotations

from typing import Any

from .core import DomainError, require_int, require_nonempty

MAX_OPTIONS_PER_MENU = 4


def validate_options(
    supply: dict[str, int], options: Any, max_options: int = MAX_OPTIONS_PER_MENU
) -> tuple[list[dict[str, Any]], int]:
    """Check a menu's option list against the tender's resource supply.

    Returns ``(normalized_options, max_single_payment_cents)`` where the
    latter is the highest option price -- the amount one guarantee must
    cover, since the options are mutually exclusive.
    """
    if not isinstance(options, list) or not 1 <= len(options) <= max_options:
        raise DomainError(
            f"a menu needs between 1 and {max_options} mutually exclusive options"
        )
    labels: set[str] = set()
    normalized: list[dict[str, Any]] = []
    for opt in options:
        if not isinstance(opt, dict):
            raise DomainError(f"each option must be an object, got {opt!r}")
        label = require_nonempty(opt.get("label"), "option label")
        if label in labels:
            raise DomainError(f"duplicate option label {label!r} within the menu")
        labels.add(label)
        resources = opt.get("resources")
        if not isinstance(resources, dict) or not resources:
            raise DomainError(
                f"option {label!r}: resources must be a non-empty object"
            )
        norm_res: dict[str, int] = {}
        for resource, qty in resources.items():
            if resource not in supply:
                raise DomainError(
                    f"option {label!r}: unknown resource {resource!r} "
                    "(not offered by this tender)"
                )
            require_int(qty, f"option {label!r} resources[{resource!r}]", minimum=1)
            if qty > supply[resource]:
                raise DomainError(
                    f"option {label!r}: asks {qty} of {resource!r}, "
                    f"supply is only {supply[resource]}"
                )
            norm_res[resource] = qty
        price = require_int(opt.get("price_cents"), f"option {label!r} price_cents",
                            minimum=1)
        normalized.append(
            {"label": label, "resources": norm_res, "price_cents": price}
        )
    max_payment = max(o["price_cents"] for o in normalized)
    return normalized, max_payment


def solve_package_tender(
    supply: dict[str, int], menus: list[dict[str, Any]]
) -> dict[str, Any]:
    """Optimal legal allocation for one tender.

    ``menus`` items carry ``menu_id``, ``buyer`` and validated ``options``.
    Returns the winning total, the sorted winning labels, per-resource
    usage and the winner list.  At most one option per menu is selected.
    """
    resource_ids = sorted(supply)
    index = {r: i for i, r in enumerate(resource_ids)}
    caps = tuple(int(supply[r]) for r in resource_ids)

    by_label: dict[str, dict[str, Any]] = {}
    prepared: list[list[dict[str, Any]]] = []
    for menu in menus:
        opts: list[dict[str, Any]] = []
        for opt in menu["options"]:
            vec = [0] * len(resource_ids)
            for resource, qty in opt["resources"].items():
                vec[index[resource]] += qty
            entry = {
                "menu_id": menu["menu_id"],
                "buyer": menu["buyer"],
                "label": opt["label"],
                "resources": {r: opt["resources"][r] for r in sorted(opt["resources"])},
                "price_cents": opt["price_cents"],
                "vector": tuple(vec),
            }
            if entry["label"] in by_label:
                raise DomainError(
                    f"option label {entry['label']!r} is not unique in this tender"
                )
            by_label[entry["label"]] = entry
            opts.append(entry)
        prepared.append(opts)

    zero = (0,) * len(caps)
    # usage vector -> (total price, sorted winning-label tuple)
    states: dict[tuple[int, ...], tuple[int, tuple[str, ...]]] = {zero: (0, ())}
    for opts in prepared:
        nxt = dict(states)  # the "choose nothing from this menu" branch
        for usage, (value, labels) in states.items():
            for o in opts:
                nu = tuple(u + v for u, v in zip(usage, o["vector"]))
                if any(nu[i] > caps[i] for i in range(len(caps))):
                    continue
                nv = value + o["price_cents"]
                nl = tuple(sorted(labels + (o["label"],)))
                cur = nxt.get(nu)
                if cur is None or nv > cur[0] or (nv == cur[0] and nl < cur[1]):
                    nxt[nu] = (nv, nl)
        states = nxt

    best_usage = zero
    best_value, best_labels = 0, ()
    for usage, (value, labels) in states.items():
        if value > best_value or (value == best_value and labels < best_labels):
            best_usage, best_value, best_labels = usage, value, labels

    winners = [by_label[label] for label in best_labels]
    return {
        "total_cents": best_value,
        "labels": list(best_labels),
        "usage": {r: best_usage[index[r]] for r in resource_ids},
        "winners": [
            {
                "menu_id": w["menu_id"],
                "buyer": w["buyer"],
                "label": w["label"],
                "resources": w["resources"],
                "price_cents": w["price_cents"],
            }
            for w in winners
        ],
    }
