from collections import Counter
from csv import Error

from app.business import DomainIssue
from app.intake.reader import records
from app.intake.resolve import partition
from app.intake.schema import HEADER, record


def inspect(data):
    counted = repeated = blanks = 0
    candidates, rejected = [], []
    try:
        for line, values in records(data):
            counted += 1
            if not any(v.strip() for v in values):
                blanks += 1
                continue
            if tuple(values) == HEADER:
                repeated += 1
                continue
            try:
                candidates.append((line, record(values)))
            except DomainIssue as error:
                key = values[0][:36] if values else ""
                case = values[1][:36] if len(values) > 1 else ""
                rejected.append(
                    dict(line=line, record=key, case=case, reason=error.code)
                )
    except Error as error:
        raise DomainIssue("CSV_MALFORMED") from error
    accepted, conflicts = partition(candidates)
    rejects = sorted(rejected + conflicts, key=lambda row: row["line"])
    return dict(
        input=counted,
        accepted=len(accepted),
        repeated=repeated,
        blank=blanks,
        rejected=len(rejects),
        reasons=dict(Counter(r["reason"] for r in rejects)),
        isolation=rejects,
        rows=accepted,
    )


def accepted_rows_with_lines(data):
    """与 inspect 相同的采信结果，额外给出每条采信记录的源行号。

    行号放在独立映射中，不写进业务 row，避免破坏重复回放的等值判断。
    """
    line_of = {}
    candidates = []
    for line, values in records(data):
        if not any(v.strip() for v in values) or tuple(values) == HEADER:
            continue
        try:
            parsed = record(values)
        except DomainIssue:
            continue
        line_of[parsed["record"]] = line
        candidates.append((line, parsed))
    accepted, _ = partition(candidates)
    return accepted, line_of
