"""Read LambdaCore flat and big-list mail without executing MOO code."""

from dataclasses import asdict, dataclass
import re

from .inspection import LookupFailed, property_value


@dataclass(frozen=True)
class MailMessage:
    number: int
    date: int
    sender: str
    recipients: str
    subject: str
    headers: tuple[str, ...]
    body: tuple[str, ...]

    def as_dict(self):
        return asdict(self)


def _integer(value):
    return type(value) is int


def _message(number, text):
    if not _integer(number) or number < 1:
        raise LookupFailed("Invalid stored mail message number")
    if (not isinstance(text, list) or len(text) < 4 or not _integer(text[0])
            or not all(isinstance(s, str) for s in text[1:])):
        raise LookupFailed(f"Malformed mail message {number}")
    # Additional headers precede the first empty string, never part of the body.
    split = text.index("", 4) if "" in text[4:] else len(text)
    return MailMessage(number, *text[:4], tuple(text[4:split]), tuple(text[split + 1:]))


def read_mailbox(db, obj):
    """Return active messages in stored order; deleted mail is not resurrected.

    Numbers are stored mailbox numbers, not list positions. They are meaningful
    within a snapshot; a live mailbox can explicitly renumber its messages.
    """
    raw = property_value(db, obj, "messages")
    if not isinstance(raw, list):
        raise LookupFailed(f"#{obj.id}.messages is not a supported mail store")
    rows = []
    if raw and isinstance(raw[0], str):
        # $big_mail_recipient uses {property, count, ordering} descriptors.
        seen = set()

        def visit(desc, expected_level=None):
            if (not isinstance(desc, list) or len(desc) != 3 or not isinstance(desc[0], str)
                    or not _integer(desc[1]) or desc[1] < 0 or not isinstance(desc[2], list)):
                raise LookupFailed("Malformed big-mail descriptor")
            name = desc[0]
            if name in seen:
                raise LookupFailed("Repeated or cyclic big-mail node")
            seen.add(name)
            node = property_value(db, obj, name)
            if (not isinstance(node, list) or len(node) != 2 or not _integer(node[0])
                    or node[0] < 0 or not isinstance(node[1], list)
                    or (expected_level is not None and node[0] != expected_level)):
                raise LookupFailed("Malformed big-mail node")
            before = len(rows)
            if node[0]:
                for child in node[1]:
                    visit(child, node[0] - 1)
            else:
                for leaf in node[1]:
                    if (not isinstance(leaf, list) or len(leaf) < 6
                            or not (leaf[0] == 0 or isinstance(leaf[0], str))):
                        raise LookupFailed("Malformed big-mail message")
                    body = property_value(db, obj, leaf[0]) if isinstance(leaf[0], str) else None
                    if body is not None and (not isinstance(body, list) or not all(isinstance(s, str) for s in body)):
                        raise LookupFailed("Malformed big-mail body")
                    rows.append([leaf[1], leaf[2:] + ([""] + body if body is not None else [])])
            if len(rows) - before != desc[1]:
                raise LookupFailed("Big-mail message count disagrees with stored tree")

        visit(raw)
    else:
        rows = raw
    result = []
    seen_numbers = set()
    for row in rows:
        if not isinstance(row, list) or len(row) != 2:
            raise LookupFailed("Malformed flat-mail record")
        message = _message(*row)
        if message.number in seen_numbers:
            raise LookupFailed(f"Duplicate mail message number {message.number}")
        seen_numbers.add(message.number)
        result.append(message)
    return result


def select_messages(messages, sequence="all", *, search=None, tail=None):
    """Select stored numbers/ranges, then case-insensitive literal search, then tail."""
    if tail is not None and tail < 1:
        raise LookupFailed("tail must be positive")
    if sequence != "all":
        ranges = []
        for part in sequence.split(","):
            if not re.fullmatch(r"[1-9][0-9]*(?:-[1-9][0-9]*)?", part):
                raise LookupFailed("Use all, a message number, an inclusive range, or comma-separated numbers/ranges")
            bounds = [int(n) for n in part.split("-")]
            start, end = bounds[0], bounds[-1]
            if end < start:
                raise LookupFailed("Mail range end precedes its start")
            ranges.append((start, end))
        messages = [m for m in messages if any(start <= m.number <= end for start, end in ranges)]
        if not messages:
            raise LookupFailed("No stored messages in that number selection")
    if search is not None:
        needle = search.casefold()
        messages = [m for m in messages if needle in "\n".join(
            (m.sender, m.recipients, m.subject, *m.headers, *m.body)).casefold()]
    return messages[-tail:] if tail else messages
