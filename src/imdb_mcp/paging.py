"""Keep tool results small enough for a chat client to accept.

A result over BUDGET is kept here and answered with its first page, the total
and the field names. get_result_page reads the rest, filtered or narrowed to
chosen fields, so nothing is lost: it only arrives in parts.
"""

import json
import uuid
from collections import OrderedDict

BUDGET = 80_000
"""Characters per response, about 20 000 tokens."""

_INLINE = 2_000
"""Lists smaller than this stay in the first page; larger ones are paged."""

# ponytail: count-capped LRU in process memory; results are lost on restart.
_KEPT = 8
_results: OrderedDict = OrderedDict()

_HINT = (
    "Large result, sent in pages. Call get_result_page with this result_id and "
    "next_offset for more, or path to page another of the lists. Pass fields "
    'to fetch only some keys, or match to filter, e.g. {"title": "alien"}.'
)


def _dump(value) -> str:
    return json.dumps(value, separators=(",", ":"), ensure_ascii=False, default=str)


def _at(value, path):
    for key in path:
        value = value[key]
    return value


def _lists(value, path=(), depth=0, found=None) -> dict:
    """Every list worth paging, by path, largest first."""
    found = {} if found is None else found
    if isinstance(value, list):
        size = len(_dump(value))
        if size >= _INLINE or not path:
            found[path] = size
    elif isinstance(value, dict) and depth <= 4:
        for key, child in value.items():
            _lists(child, (*path, key), depth + 1, found)
    return dict(sorted(found.items(), key=lambda kv: -kv[1]))


def _get(item, field: str):
    for key in field.split("."):
        if not isinstance(item, dict):
            return None
        item = item.get(key)
    return item


def _matches(item, match: dict) -> bool:
    return all(
        str(want).lower() in _dump(_get(item, field)).lower()
        for field, want in match.items()
    )


def _replace(payload, replacements: dict):
    """A copy of payload with the list at each path swapped for a new value."""
    if () in replacements:
        return replacements[()]
    copy = dict(payload)
    for path, value in replacements.items():
        node = copy
        for key in path[:-1]:
            node[key] = dict(node[key])
            node = node[key]
        node[path[-1]] = value
    return copy


def _name(path) -> str | None:
    return ".".join(map(str, path)) or None


def fit(payload) -> str:
    """Serialise a tool's answer, paging it when it is over BUDGET."""
    text = _dump(payload)
    if len(text) <= BUDGET:
        return text

    result_id = uuid.uuid4().hex[:12]
    lists = _lists(payload)
    if lists and len(_dump(_replace(payload, {p: [] for p in lists}))) > BUDGET // 2:
        lists = {}
    _results[result_id] = (payload if lists else text, lists)
    while len(_results) > _KEPT:
        _results.popitem(last=False)
    return _page(result_id)


def shape(result, extra: dict | None = None) -> str:
    """fit() for a raw API result, wrapped the way the generated tools answer."""
    if isinstance(result, str):
        # A body with one byte of bad UTF-8 fails response.json() but is still JSON.
        try:
            result = json.loads(result)
        except ValueError:
            pass
    return fit({"status": "success", **(extra or {}), "result": result})


def _text_page(result_id, text, offset) -> str:
    # Escaping inside a JSON string grows the text, so shrink until it fits.
    end = offset + BUDGET - 1_000
    while len(_dump(text[offset:end])) > BUDGET - 1_000:
        end = offset + (end - offset) * 9 // 10
    return _dump({
        "status": "success",
        "partial_json": text[offset:end],
        "paging": {
            "result_id": result_id,
            "total_chars": len(text),
            "offset": offset,
            "next_offset": end if end < len(text) else None,
            "hint": "Too large to split into items. Concatenate partial_json "
            "from every page to rebuild it.",
        },
    })


def _page(result_id, offset=0, limit=None, fields=None, match=None, path=None) -> str:
    payload, lists = _results[result_id]
    _results.move_to_end(result_id)
    if not lists:
        return _text_page(result_id, payload, offset)

    chosen = next(iter(lists)) if path is None else tuple(path.split(".")) if path else ()
    if chosen not in lists:
        return _dump({
            "status": "error",
            "message": f"No list at {path!r}. Choose one of: {', '.join(map(str, map(_name, lists)))}.",
        })

    items = _at(payload, chosen)
    if match:
        items = [i for i in items if _matches(i, match)]
    total = len(items)
    window = items[offset:]
    if limit is not None:
        window = window[:limit]
    if fields:
        window = [{f: _get(i, f) for f in fields} for i in window]

    sample = next((i for i in items if isinstance(i, dict)), None)
    paging = {
        "result_id": result_id,
        "path": _name(chosen),
        "lists": {_name(p): len(_at(payload, p)) for p in lists} if len(lists) > 1 else None,
        "total": total,
        "offset": offset,
        "fields": sorted(sample) if sample else None,
        "hint": _HINT,
    }
    empty = {p: [] for p in lists}
    used = len(_dump({"page": _replace(payload, empty), "paging": paging})) + 200

    taken = []
    for item in window:
        size = len(_dump(item)) + 1
        if taken and used + size > BUDGET:
            break
        taken.append(item)
        used += size

    following = offset + len(taken)
    paging["returned"] = len(taken)
    paging["next_offset"] = following if following < total else None

    page = _replace(payload, {**empty, chosen: taken})
    if isinstance(page, dict):
        return _dump({**page, "paging": paging})
    return _dump({"status": "success", "result": page, "paging": paging})


def get_result_page(
    result_id: str,
    offset: int = 0,
    limit: int | None = None,
    fields: list[str] | None = None,
    match: dict[str, str] | None = None,
    path: str | None = None,
) -> str:
    """Read more of a result that was too large to return in one piece.

    Any tool whose answer is too large returns its first page with a
    `paging` block. Pass that block's `result_id` here. Nothing is fetched
    again: the stored result is paged, filtered and narrowed.

    Args:
        result_id: From the `paging` block of the earlier answer.
        offset: First item to return, usually the previous `next_offset`.
        limit: At most this many items. Fewer come back if they would not fit.
        fields: Only these keys of each item. Dots reach nested keys, such as
            "statistics.sizeOnDisk". The `paging.fields` list shows what exists.
        match: Keep only items whose field contains this text, ignoring case,
            for example {"title": "alien", "year": "1979"}.
        path: Which list to page when the result holds several, as named in
            `paging.lists`. Defaults to the largest.
    """
    if result_id not in _results:
        return _dump({
            "status": "error",
            "message": "That result has expired. Call the original tool again.",
        })
    return _page(result_id, offset, limit, fields, match, path)


def register(mcp) -> None:
    """Add get_result_page to a server."""
    mcp.tool(annotations={
        "readOnlyHint": True,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": False,
    })(get_result_page)
