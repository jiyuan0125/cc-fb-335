#!/usr/bin/env python3
"""Order quality-rule engine.

Every run re-reads the rules file (ops replaces it wholesale) and evaluates
the rules against a CSV of orders.  No rule content is hard-coded here: all
rids, fields, operators, thresholds, severities and priorities come from the
rules file, so swapping the file changes the results.

Outputs:
  violations.csv  -- row,rid,severity,actual ; one line per emitted violation
  rule_stats.txt  -- per-rule hit count and share of data rows

Concepts:
  * A rule's op/value describe the condition a value must SATISFY; a value
    that fails the condition is a violation.
  * Rules on the same field are arbitrated by priority (larger wins).  If
    any top-priority rule on that field is violated, all violations on the
    field are reported; if the top priority passes, lower-priority
    violations are suppressed.  Equal-priority conflicting rules report the
    violating side(s).
  * Empty states:
      - short row (cell absent): skip that field's judgement entirely
      - empty string: a violation only for NOTNULL checks, otherwise no
        violation
      - literal "null": a violation under every condition
  * Rules whose rid starts with SUM (or whose op is SUM) apply to the total
    of a column.  Totals tolerate float error at the minimum accounting
    unit, 0.01.
"""

import csv
import json
import sys
from decimal import Decimal, InvalidOperation

TOLERANCE = Decimal("0.01")
TOTAL_ROW = "TOTAL"
NULL_TOKEN = "null"
MISSING = object()


def to_number(value):
    """Return a Decimal when the value is numeric, else None."""
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, Decimal):
        return value if value.is_finite() else None
    if isinstance(value, int):
        return Decimal(value)
    if isinstance(value, float):
        return Decimal(str(value))
    try:
        number = Decimal(str(value).strip())
    except (InvalidOperation, ValueError):
        return None
    return number if number.is_finite() else None


def compare(left, right):
    """Three-way comparison; numeric when both sides are numbers."""
    left_num, right_num = to_number(left), to_number(right)
    if left_num is not None and right_num is not None:
        return (left_num > right_num) - (left_num < right_num)
    left_s, right_s = str(left), str(right)
    return (left_s > right_s) - (left_s < right_s)


def same_value(left, right):
    left_num, right_num = to_number(left), to_number(right)
    if left_num is not None and right_num is not None:
        return left_num == right_num
    return str(left) == str(right)


def satisfies(rule, candidate):
    """True if candidate meets the rule's condition, False if it violates it."""
    op = str(rule.get("op", "")).upper()
    value = rule.get("value")

    # Literal "null" fails every condition.
    if candidate == NULL_TOKEN:
        return False

    # An empty string only trips non-null checks.
    if candidate == "":
        return op != "NOTNULL"

    if op == "NOTNULL":
        return True

    if op == "SUM":
        total, target = to_number(candidate), to_number(value)
        return (total is not None and target is not None
                and abs(total - target) <= TOLERANCE)

    if op == "EQ":
        return same_value(candidate, value)
    if op == "IN":
        return any(same_value(candidate, option) for option in value)
    if op == "GT":
        return compare(candidate, value) > 0
    if op == "LT":
        return compare(candidate, value) < 0
    if op == "RANGE":
        low, high = value[0], value[1]
        return compare(candidate, low) >= 0 and compare(candidate, high) <= 0

    # Unknown operator: cannot assert a violation.
    return True


def is_total_rule(rule):
    return (str(rule.get("rid", "")).upper().startswith("SUM")
            or str(rule.get("op", "")).upper() == "SUM")


def priority(rule):
    """Numeric priority; larger wins.  Malformed values rank lowest."""
    value = to_number(rule.get("priority"))
    return value if value is not None else Decimal("-Infinity")


def cell_at(row, index):
    if index is None or index >= len(row):
        return MISSING
    return row[index]


def column_total(rows, index):
    """Sum numeric cells; absent/empty/literal-null cells contribute nothing."""
    total = Decimal("0")
    for row in rows:
        cell = cell_at(row, index)
        if cell is MISSING or cell == NULL_TOKEN:
            continue
        number = to_number(cell)
        if number is not None:
            total += number
    return total


def render(value):
    if isinstance(value, Decimal):
        text = format(value, "f")
        if "." in text:
            text = text.rstrip("0").rstrip(".")
        return "0" if text in ("", "-0") else text
    return "" if value is MISSING else str(value)


def main(argv):
    rules_path = argv[1] if len(argv) > 1 else "rules.json"
    data_path = argv[2] if len(argv) > 2 else "orders.csv"
    out_path = argv[3] if len(argv) > 3 else "violations.csv"
    stats_path = argv[4] if len(argv) > 4 else "rule_stats.txt"

    with open(rules_path, "r", encoding="utf-8") as fh:
        rules = [rule for rule in json.load(fh) if isinstance(rule, dict)]
    with open(data_path, "r", encoding="utf-8", newline="") as fh:
        reader = csv.reader(fh)
        header = next(reader)
        rows = list(reader)

    columns = {name: i for i, name in enumerate(header)}
    row_rules = [rule for rule in rules if not is_total_rule(rule)]
    total_rules = [rule for rule in rules if is_total_rule(rule)]

    # Rules on the same field compete on priority; other fields are independent.
    field_groups = {}
    for rule in row_rules:
        field_groups.setdefault(rule.get("field"), []).append(rule)

    total_groups = {}
    for rule in total_rules:
        total_groups.setdefault(rule.get("field"), []).append(rule)

    order = {id(rule): position for position, rule in enumerate(rules)}
    hits = {rule.get("rid", ""): 0 for rule in rules}
    findings = []

    for row_number, row in enumerate(rows, start=1):
        row_findings = []
        for field, group in field_groups.items():
            index = columns.get(field)
            if index is None:
                continue                      # field absent: nothing to judge
            if index >= len(row):
                continue                      # short row: skip the field
            cell = cell_at(row, index)
            found = [(rule, cell) for rule in group if not satisfies(rule, cell)]
            if not found:
                continue
            top = max(priority(rule) for rule in group)
            if any(priority(rule) == top for rule, _ in found):
                row_findings.extend(found)
        # Report in rules-file order for a stable file layout.
        row_findings.sort(key=lambda item: order[id(item[0])])
        for rule, cell in row_findings:
            hits[rule.get("rid", "")] += 1
            findings.append((row_number, rule, cell))

    for field, group in total_groups.items():
        index = columns.get(field)
        if index is None:
            continue                          # nothing to total
        total = column_total(rows, index)
        found = [rule for rule in group if not satisfies(rule, total)]
        if not found:
            continue
        top = max(priority(rule) for rule in group)
        if any(priority(rule) == top for rule in found):
            for rule in found:
                hits[rule.get("rid", "")] += 1
                findings.append((TOTAL_ROW, rule, total))

    with open(out_path, "w", encoding="utf-8", newline="") as fh:
        writer = csv.writer(fh, lineterminator="\n")
        writer.writerow(["row", "rid", "severity", "actual"])
        for row_number, rule, actual in findings:
            writer.writerow([row_number, rule.get("rid", ""),
                             rule.get("severity", ""), render(actual)])

    denominator = len(rows)
    with open(stats_path, "w", encoding="utf-8") as fh:
        fh.write("rid\thits\tshare\n")
        for rule in rules:
            rid = rule.get("rid", "")
            count = hits.get(rid, 0)
            share = (Decimal(count) / Decimal(denominator)) if denominator else Decimal(0)
            fh.write("%s\t%d\t%.2f%%\n" % (rid, count, share * 100))


if __name__ == "__main__":
    main(sys.argv)
