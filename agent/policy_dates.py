"""Bounded, source-bound policy as-of dates. These never change the server clock.

Only explicit calendar dates are accepted; relative/incomplete dates and multiple
dates require clarification. A model paraphrase is not a source of a new date.
"""
from datetime import date
import re


_CALENDAR = r"(?:[0-9]{4}-[0-9]{1,2}(?:-[0-9]{1,2})?|[0-9]{4}年(?:[0-9]{1,2}月(?:[0-9]{1,2}[日号])?)?|[0-9]{1,2}月[0-9]{1,2}[日号])"
_RELATIVE = r"(?:去年|前年|上个月|以前|过去|旧版生效时)(?:[0-9年月日号]*)"
_TOKEN = rf"(?:{_CALENDAR}|{_RELATIVE})"
_DATE = re.compile(rf"(?<![A-Za-z0-9_-]){_CALENDAR}(?![A-Za-z0-9_-])")
_WHEN = re.compile(rf"(?:请)?(?:截至|截止到?|回到|按照|按|以|在)\s*(?P<date>{_TOKEN})(?:当天|当日|当时|的时候|时)?(?:的)?(?:(?:有效|生效|适用)的?)?(?:政策|规则|规定)?")
_TEMPORAL_ONLY = re.compile(r"(?:(?:请|截至|截止到|截止|回到|按照|按|以|在|当天|当日|当时|的时候|时|的|有效|生效|适用|政策|规则|规定|还是|或者|或|到|至|不要|不能|不是|不|之前|以前|之后|以后)|\s)*")
CLARIFICATION = "请明确一个政策查询日期（如 2026-09-30）。需要完整、有效的年月日；订单日期或文档发布日期不能直接当作政策查询日期。"


def _calendar(text):
    match = re.fullmatch(r"([0-9]{4})-([0-9]{1,2})-([0-9]{1,2})|([0-9]{4})年([0-9]{1,2})月([0-9]{1,2})[日号]", text)
    if match is None:
        return None
    try:
        return date(*map(int, match.groups()[:3] if match[1] else match.groups()[3:])).isoformat()
    except ValueError:
        return None


def parse_policy_date(text):
    """Keep all business qualifiers; remove only explicit temporal syntax."""
    mentions, dates = list(_WHEN.finditer(text)), list(_DATE.finditer(text))
    body = []
    for clause in re.split(r"[，,；;。？?！!\n]", text):
        cleaned = _WHEN.sub("", clause).strip()
        # A date-only choice/range can be clarified without losing its question.
        if _TEMPORAL_ONLY.fullmatch(_DATE.sub("", cleaned)):
            continue
        if cleaned:
            body.append(cleaned)
    result = {"as_of": None, "business_question": "；".join(body), "error": None, "source_text": None}
    if not mentions and not dates:
        result["business_question"] = text
        return result
    if len(mentions) != 1 or len(dates) != 1:
        result["error"] = "AMBIGUOUS_POLICY_DATE"
        return result
    match = mentions[0]
    value = _calendar(match["date"])
    # Do not interpret an asserted order event as an as-of request.
    event = re.search(r"订单\s*[A-Za-z0-9_-]*\s*$", text[:match.start()]) or re.match(r"(?:下单|购买|支付|签收|发货|创建|退款)", text[match.end():])
    unresolved = (re.search(r"(?:不|不要|不能|不是|别|而非)\s*$", text[:match.start()])
                  or re.match(r"(?:之前|以前|之后|以后|前|后)", text[match.end("date"):]))
    if value is None or event or unresolved:
        result["error"] = "INVALID_POLICY_DATE" if value is None else "AMBIGUOUS_POLICY_DATE"
    else:
        result.update(as_of=value, source_text=match.group())
    return result


def bind_policy_date(description, original, message_id, *, single_goal):
    request = parse_policy_date(description)
    source = parse_policy_date(original)
    if single_goal:
        request = source
    elif source["error"] or request["as_of"] != source["as_of"]:
        # A mixed request with uncertain date scope must not inherit another
        # goal's date, or silently fall back to today after model omission.
        request["error"] = "AMBIGUOUS_POLICY_DATE"
    elif request["as_of"] and not re.search(r"政策|规则|规定", source["source_text"] or ""):
        # In a mixed request, a bare date can belong to the SQL goal. Require
        # explicit policy-date wording in the user's source, not just the draft.
        request["error"] = "AMBIGUOUS_POLICY_DATE"
    if request["as_of"]:
        request["source_text"] = source["source_text"]
    request["source_message_id"] = message_id
    return request


def date_reply(text):
    """Accept only a short date answer; never discard an additional request."""
    text = text.strip().rstrip("。！!？?")
    text = re.sub(r"^(?:改为|改成|换成)\s*", "", text)
    value = _calendar(text)
    if value:
        return value
    match = _WHEN.fullmatch(text)
    return _calendar(match["date"]) if match else None
