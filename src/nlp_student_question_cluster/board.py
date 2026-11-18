"""学生问题归纳台：按轮次追溯问题原文、主题归属、教师处置与汇总口径。

设计要点：
- 一切变更都发生在某个开放的轮次内，并写入事件日志，可按轮次回放。
- 问题有稳定编号与文本修订历史；归属是“问题 × 主题”的多对多记录，
  区分手工指定与自动聚类，手工优先，自动聚类不得覆盖手工归属。
- 主题可合并、可拆分，血缘关系（predecessors/successors）永久保留；
  合并时对同一问题的重复归属去重，避免统计重复计数。
- 教师跟进记录持有主题快照与变更轨迹；已完成的跟进绝不随主题
  合并/拆分或文本修订而静默迁移。
- 汇总按当前有效归属统计：主题内按问题去重，总数跨主题去重，
  每轮快照连同口径说明一并落库。
"""

from datetime import datetime, timezone

from .text import normalize_text

# 汇总口径说明，随快照保存，便于日后核对统计规则。
AGGREGATION_BASIS = "current-assignments-distinct-questions"


class BoardError(Exception):
    """归纳台操作被拒绝。"""


def _utc_now():
    return datetime.now(timezone.utc)


def default_state():
    return {
        "schema": 1,
        "current_round": None,
        "rounds": [],
        "questions": {},
        "themes": {},
        "assignments": [],
        "followups": [],
        "aggregations": [],
        "events": [],
        "counters": {"assignment": 0, "followup": 0, "event": 0},
    }


def _clean_id(value, what):
    text = str(value).strip()
    if not text:
        raise BoardError(f"{what}不能为空")
    return text


class ReviewBoard:
    """归纳台领域对象：在一份可 JSON 序列化的状态字典上工作。"""

    def __init__(self, state=None, clock=None):
        self.state = state if state is not None else default_state()
        for key, value in default_state().items():
            self.state.setdefault(key, value)
        self.clock = clock or _utc_now

    # ---- 基础工具 ----

    def _now(self):
        moment = self.clock()
        if isinstance(moment, datetime):
            return moment.isoformat()
        return str(moment)

    def _round(self):
        if self.state["current_round"] is None:
            raise BoardError("当前没有开放的轮次，请先开启轮次")
        return self.state["current_round"]

    def _event(self, kind, **details):
        self.state["counters"]["event"] += 1
        event = {
            "seq": self.state["counters"]["event"],
            "round": self.state["current_round"],
            "at": self._now(),
            "kind": kind,
            "details": details,
        }
        self.state["events"].append(event)
        return event

    def _next_id(self, kind, prefix):
        self.state["counters"][kind] += 1
        return f"{prefix}{self.state['counters'][kind]}"

    def _question(self, question_id):
        question = self.state["questions"].get(question_id)
        if question is None:
            raise BoardError(f"未找到问题 {question_id}")
        return question

    def _theme(self, theme_id):
        theme = self.state["themes"].get(theme_id)
        if theme is None:
            raise BoardError(f"未找到主题 {theme_id}")
        return theme

    def _assignment(self, assignment_id):
        for assignment in self.state["assignments"]:
            if assignment["assignment_id"] == assignment_id:
                return assignment
        raise BoardError(f"未找到归属记录 {assignment_id}")

    def _followup(self, followup_id):
        for followup in self.state["followups"]:
            if followup["followup_id"] == followup_id:
                return followup
        raise BoardError(f"未找到跟进记录 {followup_id}")

    def _live_assignments(self, question_id=None, theme_id=None):
        """当前有效的归属（active 或 needs_review；superseded 已成为历史）。"""
        result = []
        for assignment in self.state["assignments"]:
            if assignment["status"] == "superseded":
                continue
            if question_id is not None and assignment["question_id"] != question_id:
                continue
            if theme_id is not None and assignment["theme_id"] != theme_id:
                continue
            result.append(assignment)
        return result

    def _create_assignment(self, question_id, theme_id, origin, basis="", status="active"):
        question = self._question(question_id)
        assignment = {
            "assignment_id": self._next_id("assignment", "a"),
            "question_id": question_id,
            "theme_id": theme_id,
            "origin": origin,
            "status": status,
            "basis": basis,
            "question_revision": question["revision"],
            "created_round": self._round(),
            "created_at": self._now(),
            "superseded_round": None,
            "superseded_by": None,
        }
        self.state["assignments"].append(assignment)
        self._event(
            "assignment_created",
            assignment_id=assignment["assignment_id"],
            question_id=question_id,
            theme_id=theme_id,
            origin=origin,
            status=status,
            basis=basis,
        )
        return assignment

    def _supersede(self, assignment, replaced_by=None, reason=""):
        assignment["status"] = "superseded"
        assignment["superseded_round"] = self._round()
        assignment["superseded_by"] = replaced_by
        self._event(
            "assignment_superseded",
            assignment_id=assignment["assignment_id"],
            question_id=assignment["question_id"],
            theme_id=assignment["theme_id"],
            origin=assignment["origin"],
            replaced_by=replaced_by,
            reason=reason,
        )

    def _confirm(self, assignment, via):
        assignment["status"] = "active"
        assignment["question_revision"] = self._question(assignment["question_id"])["revision"]
        self._event(
            "assignment_confirmed",
            assignment_id=assignment["assignment_id"],
            question_id=assignment["question_id"],
            theme_id=assignment["theme_id"],
            origin=assignment["origin"],
            via=via,
        )

    # ---- 轮次 ----

    def open_round(self, note=""):
        if self.state["current_round"] is not None:
            raise BoardError("已有开放的轮次，请先关闭当前轮次")
        number = len(self.state["rounds"]) + 1
        record = {
            "round": number,
            "opened_at": self._now(),
            "closed_at": None,
            "note": str(note),
        }
        self.state["rounds"].append(record)
        self.state["current_round"] = number
        self._event("round_opened", note=str(note))
        return record

    def close_round(self):
        number = self._round()
        record = self.state["rounds"][number - 1]
        record["closed_at"] = self._now()
        self._event("round_closed")
        self.state["current_round"] = None
        return record

    # ---- 问题 ----

    def upsert_question(self, question_id, text):
        """登记或补充问题文本。文本变化会提升修订号，并把相关归属标记为待复核。"""
        self._round()
        question_id = _clean_id(question_id, "问题编号")
        text = normalize_text(str(text))
        if not text:
            raise BoardError("问题文本不能为空")
        round_no = self.state["current_round"]
        now = self._now()
        existing = self.state["questions"].get(question_id)
        if existing is None:
            question = {
                "question_id": question_id,
                "text": text,
                "revision": 1,
                "created_round": round_no,
                "updated_round": round_no,
                "history": [{"revision": 1, "text": text, "round": round_no, "at": now}],
            }
            self.state["questions"][question_id] = question
            self._event("question_created", question_id=question_id, revision=1)
            return {"question": question, "changed": True, "flagged": []}
        if existing["text"] == text:
            return {"question": existing, "changed": False, "flagged": []}
        existing["revision"] += 1
        existing["text"] = text
        existing["updated_round"] = round_no
        existing["history"].append(
            {"revision": existing["revision"], "text": text, "round": round_no, "at": now}
        )
        self._event(
            "question_text_updated", question_id=question_id, revision=existing["revision"]
        )
        flagged = []
        for assignment in self._live_assignments(question_id=question_id):
            if assignment["status"] == "needs_review":
                continue
            assignment["status"] = "needs_review"
            flagged.append(assignment)
            self._event(
                "assignment_flagged_review",
                assignment_id=assignment["assignment_id"],
                question_id=question_id,
                theme_id=assignment["theme_id"],
                origin=assignment["origin"],
                reason="问题文本已补充，归属需重新复核",
            )
        return {"question": existing, "changed": True, "flagged": flagged}

    # ---- 主题 ----

    def add_theme(self, theme_id, label, keywords=()):
        self._round()
        theme_id = _clean_id(theme_id, "主题编号")
        label = normalize_text(str(label))
        if not label:
            raise BoardError("主题名称不能为空")
        if theme_id in self.state["themes"]:
            raise BoardError(f"主题 {theme_id} 已存在")
        theme = {
            "theme_id": theme_id,
            "label": label,
            "keywords": [normalize_text(str(k)) for k in keywords if str(k).strip()],
            "status": "active",
            "created_round": self.state["current_round"],
            "closed_round": None,
            "predecessors": [],
            "successors": [],
        }
        self.state["themes"][theme_id] = theme
        self._event("theme_created", theme_id=theme_id, label=label)
        return theme

    def _theme_terms(self, theme):
        terms = [theme["label"], *theme["keywords"]]
        return [normalize_text(term).casefold() for term in terms if term.strip()]

    def _matching_themes(self, text, theme_ids=None):
        haystack = normalize_text(text).casefold()
        matched = []
        for theme in self.state["themes"].values():
            if theme["status"] != "active":
                continue
            if theme_ids is not None and theme["theme_id"] not in theme_ids:
                continue
            if any(term and term in haystack for term in self._theme_terms(theme)):
                matched.append(theme["theme_id"])
        return sorted(matched)

    # ---- 归属 ----

    def assign_manual(self, question_id, theme_id, basis=""):
        """教师手工指定归属，优先于自动聚类。"""
        self._round()
        self._question(question_id)
        theme = self._theme(theme_id)
        if theme["status"] != "active":
            raise BoardError(f"主题 {theme_id} 已关闭，不能指派")
        existing = self._live_assignments(question_id=question_id, theme_id=theme_id)
        if existing:
            current = existing[0]
            if current["origin"] == "manual":
                if current["status"] == "needs_review":
                    self._confirm(current, via="manual_reassign")
                return current
            replacement = self._create_assignment(
                question_id, theme_id, "manual", basis or "教师指定"
            )
            self._supersede(current, replaced_by=replacement["assignment_id"], reason="手工指定优先")
            return replacement
        return self._create_assignment(question_id, theme_id, "manual", basis or "教师指定")

    def autocluster(self):
        """按主题名称与关键词自动聚类。

        - 不覆盖、不删除手工归属；同一问题同一主题不重复建档。
        - 文本修订后待复核的自动归属：仍匹配则确认，不再匹配则作废。
        - 待复核的手工归属保持待复核，等待教师确认。
        """
        self._round()
        created, confirmed, dropped, manual_pending = [], [], [], []
        questions = sorted(self.state["questions"].values(), key=lambda q: q["question_id"])
        for question in questions:
            question_id = question["question_id"]
            live = {a["theme_id"]: a for a in self._live_assignments(question_id=question_id)}
            candidates = self._matching_themes(question["text"])
            for theme_id, assignment in live.items():
                if assignment["status"] != "needs_review":
                    continue
                if assignment["origin"] == "manual":
                    manual_pending.append(assignment["assignment_id"])
                    continue
                if theme_id in candidates:
                    self._confirm(assignment, via="autocluster")
                    confirmed.append(assignment["assignment_id"])
                else:
                    self._supersede(assignment, reason="文本修订后不再匹配该主题")
                    dropped.append(assignment["assignment_id"])
            for theme_id in candidates:
                if theme_id in live:
                    continue
                assignment = self._create_assignment(question_id, theme_id, "auto", "自动聚类")
                created.append(assignment["assignment_id"])
        summary = {
            "created": created,
            "confirmed": confirmed,
            "dropped": dropped,
            "manual_pending": manual_pending,
        }
        self._event(
            "autocluster_run",
            created=len(created),
            confirmed=len(confirmed),
            dropped=len(dropped),
            manual_pending=len(manual_pending),
        )
        return summary

    def confirm_assignment(self, assignment_id):
        """教师复核后确认归属。"""
        self._round()
        assignment = self._assignment(assignment_id)
        if assignment["status"] != "needs_review":
            raise BoardError("该归属不处于待复核状态")
        self._confirm(assignment, via="manual_confirm")
        return assignment

    # ---- 主题合并与拆分 ----

    def merge_themes(self, target_id, source_ids, label=None, keywords=None):
        """把若干主题合并进目标主题。

        同一问题在多个来源主题都有归属时只保留一条（手工优先），
        避免合并后统计重复计数；待复核状态随归属一起携带。
        进行中的跟进改指目标主题并记录轨迹；已完成的跟进保持原样。
        """
        self._round()
        target_id = _clean_id(target_id, "合并目标编号")
        source_ids = [_clean_id(s, "来源主题编号") for s in source_ids]
        if not source_ids:
            raise BoardError("合并至少需要一个来源主题")
        if len(set(source_ids)) != len(source_ids):
            raise BoardError("来源主题编号重复")
        if target_id in source_ids:
            raise BoardError("合并目标不能同时是来源")
        sources = []
        for source_id in source_ids:
            theme = self._theme(source_id)
            if theme["status"] != "active":
                raise BoardError(f"主题 {source_id} 已关闭，不能合并")
            sources.append(theme)
        if target_id in self.state["themes"]:
            target = self._theme(target_id)
            if target["status"] != "active":
                raise BoardError(f"合并目标 {target_id} 已关闭")
        else:
            if not label:
                raise BoardError("新建合并目标需要提供名称")
            target = self.add_theme(target_id, label, keywords or ())

        moved, deduped, repointed = [], [], []
        for source in sources:
            source["status"] = "merged"
            source["closed_round"] = self.state["current_round"]
            if target_id not in source["successors"]:
                source["successors"].append(target_id)
            if source["theme_id"] not in target["predecessors"]:
                target["predecessors"].append(source["theme_id"])
            for assignment in list(self._live_assignments(theme_id=source["theme_id"])):
                existing = self._live_assignments(
                    question_id=assignment["question_id"], theme_id=target_id
                )
                if existing:
                    keep = existing[0]
                    if assignment["origin"] == "manual" and keep["origin"] == "auto":
                        carried = self._carry_assignment(assignment, target_id)
                        self._supersede(
                            keep, replaced_by=carried["assignment_id"], reason="合并时手工优先"
                        )
                        self._supersede(
                            assignment,
                            replaced_by=carried["assignment_id"],
                            reason="主题合并",
                        )
                        moved.append(carried["assignment_id"])
                    else:
                        self._supersede(
                            assignment,
                            replaced_by=keep["assignment_id"],
                            reason="主题合并去重",
                        )
                        deduped.append(assignment["assignment_id"])
                else:
                    carried = self._carry_assignment(assignment, target_id)
                    self._supersede(
                        assignment, replaced_by=carried["assignment_id"], reason="主题合并"
                    )
                    moved.append(carried["assignment_id"])
            for followup in self.state["followups"]:
                if followup["theme_id"] != source["theme_id"] or followup["status"] != "pending":
                    continue
                followup["theme_id"] = target_id
                followup["theme_label"] = target["label"]
                followup["trail"].append(
                    {
                        "round": self.state["current_round"],
                        "theme_id": target_id,
                        "theme_label": target["label"],
                        "reason": f"主题合并，自 {source['theme_id']} 改指",
                    }
                )
                repointed.append(followup["followup_id"])
                self._event(
                    "followup_repointed",
                    followup_id=followup["followup_id"],
                    question_id=followup["question_id"],
                    from_theme=source["theme_id"],
                    to_theme=target_id,
                )
        self._event(
            "theme_merged",
            target=target_id,
            sources=source_ids,
            moved=len(moved),
            deduped=len(deduped),
            repointed_followups=len(repointed),
        )
        return {"target": target, "moved": moved, "deduped": deduped, "repointed_followups": repointed}

    def _carry_assignment(self, assignment, theme_id):
        """合并/拆分时把归属搬到新主题，保留来源、依据与待复核状态。"""
        carried = self._create_assignment(
            assignment["question_id"],
            theme_id,
            assignment["origin"],
            basis=assignment["basis"],
            status=assignment["status"],
        )
        carried["question_revision"] = assignment["question_revision"]
        return carried

    def split_theme(self, source_id, new_themes, mapping=None):
        """把主题拆分为若干新主题。

        归属按 mapping（手工指定，优先）或自动聚类落到新主题；
        无法匹配的问题列入 unassigned，等待人工处理。
        已有跟进（无论是否完成）保持指向源主题快照，不静默迁移，
        仍可通过问题编号追溯。
        """
        self._round()
        source = self._theme(source_id)
        if source["status"] != "active":
            raise BoardError(f"主题 {source_id} 已关闭，不能拆分")
        specs = []
        for spec in new_themes:
            specs.append(
                {
                    "theme_id": _clean_id(spec.get("theme_id", ""), "新主题编号"),
                    "label": normalize_text(str(spec.get("label", ""))),
                    "keywords": list(spec.get("keywords", ())),
                }
            )
        if len(specs) < 2:
            raise BoardError("拆分至少需要两个新主题")
        new_ids = [spec["theme_id"] for spec in specs]
        if len(set(new_ids)) != len(new_ids):
            raise BoardError("拆分目标编号重复")
        for new_id in new_ids:
            if new_id in self.state["themes"]:
                raise BoardError(f"主题 {new_id} 已存在")
        mapping = {str(k).strip(): str(v).strip() for k, v in dict(mapping or {}).items()}
        affected = {
            a["question_id"]: a for a in self._live_assignments(theme_id=source_id)
        }
        for question_id, theme_id in mapping.items():
            if theme_id not in new_ids:
                raise BoardError(f"映射目标 {theme_id} 不是本次拆分的新主题")
            if question_id not in affected:
                raise BoardError(f"映射中的问题 {question_id} 当前未归属源主题")

        created_themes = [
            self.add_theme(spec["theme_id"], spec["label"], spec["keywords"]) for spec in specs
        ]
        source["status"] = "split"
        source["closed_round"] = self.state["current_round"]
        for new_id in new_ids:
            if new_id not in source["successors"]:
                source["successors"].append(new_id)
        for theme in created_themes:
            theme["predecessors"].append(source_id)

        reassigned, unassigned = [], []
        for question_id in sorted(affected):
            assignment = affected[question_id]
            if question_id in mapping:
                targets = [mapping[question_id]]
                origin, basis = "manual", "拆分指定"
            else:
                targets = self._matching_themes(
                    self._question(question_id)["text"], theme_ids=set(new_ids)
                )
                origin, basis = "auto", "拆分自动聚类"
            carried_ids = []
            for theme_id in targets:
                carried = self._carry_assignment(assignment, theme_id)
                if carried["origin"] != origin:
                    carried["origin"] = origin
                carried["basis"] = basis
                carried_ids.append(carried["assignment_id"])
                reassigned.append(carried["assignment_id"])
            if carried_ids:
                self._supersede(
                    assignment, replaced_by=carried_ids[0], reason="主题拆分"
                )
            else:
                unassigned.append(question_id)
                self._supersede(assignment, reason="主题拆分后暂未归属")
        retained = [
            f["followup_id"]
            for f in self.state["followups"]
            if f["theme_id"] == source_id and f["status"] == "pending"
        ]
        self._event(
            "theme_split",
            source=source_id,
            successors=new_ids,
            reassigned=len(reassigned),
            unassigned=list(unassigned),
            retained_followups=list(retained),
        )
        return {
            "new_themes": created_themes,
            "reassigned": reassigned,
            "unassigned": unassigned,
            "retained_followups": retained,
        }

    # ---- 教师跟进 ----

    def open_followup(self, question_id, theme_id=None, note=""):
        self._round()
        question = self._question(question_id)
        live = self._live_assignments(question_id=question_id)
        if theme_id is None:
            theme_ids = sorted({a["theme_id"] for a in live})
            if not theme_ids:
                raise BoardError("问题尚未归属任何主题，无法建立跟进")
            if len(theme_ids) > 1:
                raise BoardError("问题归属多个主题，请指定跟进的主题")
            theme_id = theme_ids[0]
        theme = self._theme(theme_id)
        if theme["status"] != "active":
            raise BoardError(f"主题 {theme_id} 已关闭，不能建立跟进")
        if not any(a["theme_id"] == theme_id for a in live):
            raise BoardError("问题当前未归属该主题，不能建立跟进")
        followup = {
            "followup_id": self._next_id("followup", "f"),
            "question_id": question_id,
            "theme_id": theme_id,
            "theme_label": theme["label"],
            "status": "pending",
            "note": str(note),
            "opened_round": self.state["current_round"],
            "opened_at": self._now(),
            "closed_round": None,
            "closed_at": None,
            "completion_note": "",
            "question_revision": question["revision"],
            "closed_question_revision": None,
            "trail": [
                {
                    "round": self.state["current_round"],
                    "theme_id": theme_id,
                    "theme_label": theme["label"],
                    "reason": "opened",
                }
            ],
        }
        self.state["followups"].append(followup)
        self._event(
            "followup_opened",
            followup_id=followup["followup_id"],
            question_id=question_id,
            theme_id=theme_id,
        )
        return followup

    def complete_followup(self, followup_id, note=""):
        self._round()
        followup = self._followup(followup_id)
        if followup["status"] != "pending":
            raise BoardError("跟进已完成，不能重复操作")
        followup["status"] = "done"
        followup["closed_round"] = self.state["current_round"]
        followup["closed_at"] = self._now()
        followup["completion_note"] = str(note)
        followup["closed_question_revision"] = self._question(followup["question_id"])["revision"]
        self._event(
            "followup_completed",
            followup_id=followup_id,
            question_id=followup["question_id"],
            theme_id=followup["theme_id"],
        )
        return followup

    # ---- 汇总与追溯 ----

    def aggregate(self):
        """按当前口径汇总：主题内按问题去重，总数跨主题去重，并保存快照。"""
        self._round()
        per_theme = {}
        counted = set()
        for theme in self.state["themes"].values():
            if theme["status"] != "active":
                continue
            live = self._live_assignments(theme_id=theme["theme_id"])
            question_ids = sorted({a["question_id"] for a in live})
            per_theme[theme["theme_id"]] = {
                "label": theme["label"],
                "questions": question_ids,
                "count": len(question_ids),
                "by_origin": {
                    "manual": len({a["question_id"] for a in live if a["origin"] == "manual"}),
                    "auto": len({a["question_id"] for a in live if a["origin"] == "auto"}),
                },
                "needs_review": sorted(
                    {a["question_id"] for a in live if a["status"] == "needs_review"}
                ),
            }
            counted.update(question_ids)
        assigned = {a["question_id"] for a in self._live_assignments()}
        snapshot = {
            "round": self.state["current_round"],
            "computed_at": self._now(),
            "basis": AGGREGATION_BASIS,
            "themes": per_theme,
            "total_questions": len(counted),
            "unassigned": sorted(q for q in self.state["questions"] if q not in assigned),
        }
        self.state["aggregations"].append(snapshot)
        self._event(
            "aggregation_snapshot",
            total_questions=snapshot["total_questions"],
            themes=sorted(per_theme),
        )
        return snapshot

    def trace_question(self, question_id):
        """单条问题的完整履历：文本修订、归属变迁、跟进与历次汇总命中。"""
        question = self._question(question_id)
        assignments = [
            self._assignment_view(a)
            for a in self.state["assignments"]
            if a["question_id"] == question_id
        ]
        followups = [f for f in self.state["followups"] if f["question_id"] == question_id]
        appearances = []
        for snapshot in self.state["aggregations"]:
            themes = [
                theme_id
                for theme_id, data in snapshot["themes"].items()
                if question_id in data["questions"]
            ]
            appearances.append(
                {"round": snapshot["round"], "themes": themes, "counted": bool(themes)}
            )
        return {
            "question": question,
            "assignments": assignments,
            "followups": followups,
            "aggregation_appearances": appearances,
        }

    def trace_theme(self, theme_id):
        """主题生命周期：血缘、归属、跟进与逐轮统计。"""
        theme = self._theme(theme_id)
        assignments = [
            self._assignment_view(a)
            for a in self.state["assignments"]
            if a["theme_id"] == theme_id
        ]
        followups = [
            f
            for f in self.state["followups"]
            if f["theme_id"] == theme_id
            or any(step["theme_id"] == theme_id for step in f["trail"])
        ]
        counts = [
            {"round": s["round"], "count": s["themes"][theme_id]["count"]}
            for s in self.state["aggregations"]
            if theme_id in s["themes"]
        ]
        return {
            "theme": theme,
            "assignments": assignments,
            "followups": followups,
            "counts_by_round": counts,
        }

    def _assignment_view(self, assignment):
        theme = self.state["themes"].get(assignment["theme_id"])
        view = dict(assignment)
        view["theme_label"] = theme["label"] if theme else None
        return view

    def report(self):
        """当前待办总览：待复核归属、进行中跟进、完成后文本又更新的跟进。"""
        live = self._live_assignments()
        needs_review = [a for a in live if a["status"] == "needs_review"]
        pending = [f for f in self.state["followups"] if f["status"] == "pending"]
        stale_done = []
        for followup in self.state["followups"]:
            if followup["status"] != "done" or followup["closed_question_revision"] is None:
                continue
            question = self.state["questions"].get(followup["question_id"])
            if question and question["revision"] > followup["closed_question_revision"]:
                stale_done.append(followup["followup_id"])
        return {
            "current_round": self.state["current_round"],
            "questions": len(self.state["questions"]),
            "active_themes": sorted(
                t["theme_id"] for t in self.state["themes"].values() if t["status"] == "active"
            ),
            "live_assignments": len(live),
            "needs_review": [
                {
                    "assignment_id": a["assignment_id"],
                    "question_id": a["question_id"],
                    "theme_id": a["theme_id"],
                    "origin": a["origin"],
                }
                for a in needs_review
            ],
            "pending_followups": [
                {
                    "followup_id": f["followup_id"],
                    "question_id": f["question_id"],
                    "theme_id": f["theme_id"],
                    "theme_label": f["theme_label"],
                    "theme_status": self.state["themes"][f["theme_id"]]["status"],
                    "opened_round": f["opened_round"],
                }
                for f in pending
            ],
            "completed_followups_on_updated_questions": stale_done,
        }

    def round_events(self, round_no=None):
        if round_no is None:
            return list(self.state["events"])
        return [e for e in self.state["events"] if e["round"] == round_no]
