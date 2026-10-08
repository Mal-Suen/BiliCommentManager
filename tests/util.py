def make_comment(rpid, deleted=False, keep=False, t=1700000000, type=1, msg="x"):
    """构造一条最小可用的评论记录（与 fetch 落盘结构一致）。"""
    return {"rpid": rpid, "oid": 1, "type": type, "message": msg, "time": t,
            "is_reply": False, "keep": keep, "deleted": deleted, "error": None}
