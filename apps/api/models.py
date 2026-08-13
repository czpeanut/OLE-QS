"""資料模型。

設計原則：
1. **來源標註是必填，不是選填。** 依律師意見，公開試題可用但須保留來源標註 ——
   所以 Document 的來源欄位不可為空，且每一次匯出都會強制印出處（見 export.py）。
   把它做成資料庫層的約束，而不是靠使用者自律。
2. 題號的唯一鍵是「文件 + 大題 + 題號」。實測考卷有兩種編號慣例
   （分大題重編 / 全卷連續），單靠題號無法定位。
3. 資產分 figure / table / chart，且分「題目專屬」與「多題共用」兩種歸屬。
   共用資產若遺失，引用它的題目會表面正常但無法作答。
4. 欄位型別刻意選 Postgres 也支援的形狀，之後遷移只需換 JSON 欄位型別與全文索引。
"""

from __future__ import annotations

import enum
from datetime import datetime, timezone

from sqlalchemy import (JSON, Boolean, CheckConstraint, DateTime, Enum, Float,
                        ForeignKey, Integer, String, Text, UniqueConstraint)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


CITIES = ("台北", "新北", "桃園", "台中", "台南", "高雄", "基隆", "新竹", "嘉義",
          "苗栗", "彰化", "南投", "雲林", "屏東", "宜蘭", "花蓮", "台東",
          "澎湖", "金門", "連江")


def split_school(full: str) -> tuple[str, str]:
    """把學校全名拆成 (縣市, 校名簡稱)。

    「臺中市立向上國民中學」→ ("台中", "向上")
    「臺中市大業國中」      → ("台中", "大業")

    出處要印在每一題底下，必須夠短才不會蓋過題目本身，
    所以顯示用簡稱；完整校名仍存在 school 欄位，法律上的可追溯性不受影響。
    """
    name = (full or "").replace("臺", "台").strip()
    city = next((c for c in CITIES if name.startswith(c)), "")
    if city:
        name = name[len(city):].lstrip("市縣")
    name = name.removeprefix("立")
    # 長的排前面：高中附設的國中部會寫成「林園高級中學國中部」，
    # 先比對到短的「國中」只會剩下「林園高級中學」這種不像簡稱的簡稱。
    for suffix in ("高級中學附設國中部", "高級中學國中部", "高級中學國中",
                   "國民中學", "完全中學", "高級中學",
                   "國民小學", "國中", "高中", "中學", "國小"):
        if name.endswith(suffix):
            name = name[: -len(suffix)]
            break
    return city, name.strip()


class Base(DeclarativeBase):
    pass


class QuestionType(str, enum.Enum):
    single = "single"        # 單選
    multiple = "multiple"    # 多選
    tf = "tf"                # 是非
    fill = "fill"            # 填充
    matching = "matching"    # 配合
    calc = "calc"            # 計算／非選
    essay = "essay"          # 問答／寫作
    group = "group"          # 題組（有子題）


class ReviewStatus(str, enum.Enum):
    draft = "draft"
    needs_review = "needs_review"
    reviewed = "reviewed"
    rejected = "rejected"


class AnswerStatus(str, enum.Enum):
    missing = "missing"              # 尚無答案
    ai_generated = "ai_generated"    # AI 產生，未經人工確認
    disputed = "disputed"            # 多模型不一致，待人工判定
    verified = "verified"            # 已由答案卷或人工確認


class AssetKind(str, enum.Enum):
    figure = "figure"
    table = "table"
    chart = "chart"


# ─────────────────────────── 來源 ───────────────────────────

class Document(Base):
    """一份考卷。來源標註的權威來源。"""

    __tablename__ = "document"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)

    # ── 來源標註（依授權要求必填）──────────────────────────
    title: Mapped[str] = mapped_column(String(300), nullable=False)
    school: Mapped[str] = mapped_column(String(120), nullable=False)
    exam_name: Mapped[str] = mapped_column(String(200), nullable=False)
    academic_year_roc: Mapped[int] = mapped_column(Integer, nullable=False)

    semester: Mapped[int | None] = mapped_column(Integer)
    exam_seq: Mapped[int | None] = mapped_column(Integer)
    grade: Mapped[int] = mapped_column(Integer, nullable=False)
    subject: Mapped[str] = mapped_column(String(40), nullable=False, index=True)
    sub_subject: Mapped[str | None] = mapped_column(String(40))

    scope_note: Mapped[str | None] = mapped_column(String(300))
    publisher: Mapped[str | None] = mapped_column(String(40))

    source_file: Mapped[str | None] = mapped_column(String(500))
    page_count: Mapped[int | None] = mapped_column(Integer)
    total_score: Mapped[int | None] = mapped_column(Integer)
    numbering: Mapped[str | None] = mapped_column(String(20))   # per_section | continuous

    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)

    sections: Mapped[list["Section"]] = relationship(back_populates="document",
                                                     cascade="all, delete-orphan")
    # 題目**不**隨文件級聯刪除。題目自帶出處快照後就是獨立實體，
    # 清掉一次匯入紀錄不該毀掉已被考卷引用的題目。
    questions: Mapped[list["Question"]] = relationship(back_populates="document",
                                                       passive_deletes=True)
    assets: Mapped[list["Asset"]] = relationship(back_populates="document",
                                                 cascade="all, delete-orphan")

    __table_args__ = (
        CheckConstraint("length(trim(school)) > 0", name="ck_document_school_required"),
        CheckConstraint("length(trim(exam_name)) > 0", name="ck_document_exam_required"),
    )

    @property
    def citation(self) -> str:
        """這份文件的描述字串。

        ⚠️ 這**不是**題目出處的權威來源 —— 題目的出處存在 QuestionSource，
        於匯入當下快照。原因見 QuestionSource 的說明。
        """
        bits = [self.school, f"{self.academic_year_roc}學年度"]
        if self.semester:
            bits.append(f"第{self.semester}學期")
        if self.exam_seq:
            bits.append(f"第{self.exam_seq}次定期評量")
        bits.append(f"{self.grade}年級{self.subject}")
        return " ".join(bits)


class Section(Base):
    """大題（一、選擇題 / 二、填充題 …）。"""

    __tablename__ = "section"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    document_id: Mapped[str] = mapped_column(ForeignKey("document.id", ondelete="CASCADE"),
                                             index=True)
    ord: Mapped[int] = mapped_column(Integer, nullable=False)
    name: Mapped[str] = mapped_column(String(80), nullable=False)
    type: Mapped[str | None] = mapped_column(String(20))
    score_rule: Mapped[str | None] = mapped_column(String(200))
    per_item_score: Mapped[float | None] = mapped_column(Float)
    declared_count: Mapped[int | None] = mapped_column(Integer)

    document: Mapped[Document] = relationship(back_populates="sections")

    __table_args__ = (UniqueConstraint("document_id", "ord", name="uq_section_doc_ord"),)


# ─────────────────────────── 題目 ───────────────────────────

class Question(Base):
    __tablename__ = "question"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    # 只代表「從哪次匯入進來」，不是出處的權威（出處在 QuestionSource）
    document_id: Mapped[str | None] = mapped_column(
        ForeignKey("document.id", ondelete="SET NULL"), index=True)
    section_ord: Mapped[int] = mapped_column(Integer, nullable=False)
    number: Mapped[int] = mapped_column(Integer, nullable=False)

    type: Mapped[QuestionType] = mapped_column(Enum(QuestionType), nullable=False, index=True)
    stem_md: Mapped[str] = mapped_column(Text, nullable=False)
    stem_raw: Mapped[str | None] = mapped_column(Text)      # 正規化前原文，供對照原圖
    group_stem: Mapped[str | None] = mapped_column(Text)    # 題組共用說明

    answer: Mapped[list | None] = mapped_column(JSON)
    answer_status: Mapped[AnswerStatus] = mapped_column(
        Enum(AnswerStatus), default=AnswerStatus.missing, index=True)
    answer_source: Mapped[str | None] = mapped_column(String(80))   # answer_key / gemini+claude / human
    explanation_md: Mapped[str | None] = mapped_column(Text)

    difficulty: Mapped[int | None] = mapped_column(Integer, index=True)
    score: Mapped[float | None] = mapped_column(Float)
    page: Mapped[int | None] = mapped_column(Integer)
    answer_count: Mapped[int | None] = mapped_column(Integer)   # 應有幾個答案

    parent_id: Mapped[str | None] = mapped_column(ForeignKey("question.id"))
    shared_asset_key: Mapped[str | None] = mapped_column(String(40))

    status: Mapped[ReviewStatus] = mapped_column(
        Enum(ReviewStatus), default=ReviewStatus.needs_review, index=True)
    review_note: Mapped[str | None] = mapped_column(Text)
    uncertain_spans: Mapped[list | None] = mapped_column(JSON)

    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)

    document: Mapped[Document | None] = relationship(back_populates="questions")
    options: Mapped[list["Option"]] = relationship(
        back_populates="question", cascade="all, delete-orphan",
        order_by="Option.ord")
    assets: Mapped[list["Asset"]] = relationship(
        back_populates="question", cascade="all, delete-orphan")
    tags: Mapped[list["Tag"]] = relationship(
        back_populates="question", cascade="all, delete-orphan")
    sources: Mapped[list["QuestionSource"]] = relationship(
        back_populates="question", cascade="all, delete-orphan",
        order_by="QuestionSource.ord")

    @property
    def citations(self) -> list[str]:
        """這一題的所有出處。組卷匯出時逐題列出，與它來自哪份文件無關。"""
        return [s.citation for s in self.sources]

    @property
    def citation(self) -> str:
        # 方括號本身即為分界，多個出處用空白串接即可
        return " ".join(self.citations) or "（來源未標註）"

    __table_args__ = (
        # 題號在不同大題會重複，唯一鍵必須帶上大題
        UniqueConstraint("document_id", "section_ord", "number", name="uq_question_position"),
        CheckConstraint("difficulty IS NULL OR (difficulty BETWEEN 1 AND 5)",
                        name="ck_question_difficulty_range"),
    )


class Option(Base):
    __tablename__ = "option"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    question_id: Mapped[str] = mapped_column(ForeignKey("question.id", ondelete="CASCADE"),
                                             index=True)
    label: Mapped[str] = mapped_column(String(4), nullable=False)
    content_md: Mapped[str] = mapped_column(Text, default="")
    # 選項本身可以是圖（實測自然科卷有整題四個選項都是圖）
    asset_file: Mapped[str | None] = mapped_column(String(300))
    ord: Mapped[int] = mapped_column(Integer, nullable=False)

    question: Mapped[Question] = relationship(back_populates="options")

    __table_args__ = (
        UniqueConstraint("question_id", "label", name="uq_option_label"),
        CheckConstraint("length(trim(content_md)) > 0 OR asset_file IS NOT NULL",
                        name="ck_option_has_content"),
    )


class Asset(Base):
    """圖、表、圖表。

    scope='question' 為單題專屬；scope='shared' 為多題共用（共用圖、閱讀短文）。
    共用資產掛在 document 上，題目以 shared_asset_key 引用。
    """

    __tablename__ = "asset"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    document_id: Mapped[str] = mapped_column(ForeignKey("document.id", ondelete="CASCADE"),
                                             index=True)
    question_id: Mapped[str | None] = mapped_column(
        ForeignKey("question.id", ondelete="CASCADE"), index=True)

    key: Mapped[str] = mapped_column(String(40), nullable=False)
    label: Mapped[str | None] = mapped_column(String(40))      # 圖(一)、表(二)
    kind: Mapped[AssetKind] = mapped_column(Enum(AssetKind), nullable=False)
    scope: Mapped[str] = mapped_column(String(10), default="question")   # question | shared

    file: Mapped[str | None] = mapped_column(String(300))      # figure/chart 用
    markdown: Mapped[str | None] = mapped_column(Text)         # table 結構化內容
    text: Mapped[str | None] = mapped_column(Text)             # 共用閱讀短文
    alt: Mapped[str | None] = mapped_column(Text)
    bbox: Mapped[list | None] = mapped_column(JSON)
    used_by: Mapped[list | None] = mapped_column(JSON)         # 共用資產的使用題號
    # 資產已辨識、檔案尚未產生（例：已知該處有圖但還沒裁切）。
    # 這是管線的合法中間狀態，但必須顯式標記 —— 否則「沒有內容的資產」
    # 和「忘記帶檔案的資產」會混在一起，而後者會讓題目無法作答。
    pending: Mapped[bool] = mapped_column(Boolean, default=False)

    document: Mapped[Document] = relationship(back_populates="assets")
    question: Mapped[Question | None] = relationship(back_populates="assets")

    __table_args__ = (
        UniqueConstraint("document_id", "key", name="uq_asset_key"),
        CheckConstraint(
            "file IS NOT NULL OR markdown IS NOT NULL OR text IS NOT NULL "
            "OR pending = 1",
            name="ck_asset_has_payload"),
    )


class QuestionSource(Base):
    """題目的出處。**一題可以有多個**。

    為什麼不直接用 Question.document_id 推導出處：

    1. **去重合併**：同一題常同時出現在多份考卷。合併成一題後，外鍵只能指向
       其中一份，其餘來源就消失了 —— 但它們同樣是這題的合法出處。
    2. **改編題**：老師改動數字或敘述後，這題不再屬於原卷，出處應記為
       「改編自 X」。掛外鍵等於謊稱它就是原卷的題目。
    3. **出處必須不可變**：若渲染時才從 Document 算出處，那份文件被修改或刪除，
       已經發出去的考卷的出處就跟著變。因此在匯入當下把來源欄位**快照**進來。

    document_id 保留，但只代表「這題是從哪次匯入進來的」，不是出處的權威。
    """

    __tablename__ = "question_source"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    question_id: Mapped[str] = mapped_column(ForeignKey("question.id", ondelete="CASCADE"),
                                             index=True)
    ord: Mapped[int] = mapped_column(Integer, default=0)

    # ── 快照欄位：匯入當下複製，之後不隨 Document 變動 ──────────
    school: Mapped[str] = mapped_column(String(120), nullable=False)
    # 顯示用簡稱，匯入時由 school 推導，可在來源資料中明確覆寫
    city: Mapped[str | None] = mapped_column(String(20))
    school_short: Mapped[str | None] = mapped_column(String(40))
    exam_name: Mapped[str] = mapped_column(String(200), nullable=False)
    academic_year_roc: Mapped[int] = mapped_column(Integer, nullable=False)
    semester: Mapped[int | None] = mapped_column(Integer)
    exam_seq: Mapped[int | None] = mapped_column(Integer)
    grade: Mapped[int] = mapped_column(Integer, nullable=False)
    subject: Mapped[str] = mapped_column(String(40), nullable=False)

    page: Mapped[int | None] = mapped_column(Integer)
    number_in_paper: Mapped[int | None] = mapped_column(Integer)

    # original 原題 / adapted 改編 / duplicate 同題另一出處
    relation: Mapped[str] = mapped_column(String(20), default="original")
    note: Mapped[str | None] = mapped_column(String(300))

    document_id: Mapped[str | None] = mapped_column(
        ForeignKey("document.id", ondelete="SET NULL"))

    question: Mapped["Question"] = relationship(back_populates="sources")

    __table_args__ = (
        CheckConstraint("length(trim(school)) > 0", name="ck_qsource_school"),
        CheckConstraint("length(trim(exam_name)) > 0", name="ck_qsource_exam"),
    )

    @property
    def citation(self) -> str:
        """印在題目底下的出處，格式 [台中 大業 113]。

        刻意極簡：出處要跟著每一題走，冗長就會蓋過題目本身。
        學期、次數、年級、科目、題號仍完整存在資料庫，需要時可查。
        """
        city, short = self.city, self.school_short
        if not short:
            city, short = split_school(self.school)
        bits = [b for b in (city, short, str(self.academic_year_roc)) if b]
        mark = {"adapted": "改編 ", "duplicate": "另見 "}.get(self.relation, "")
        return f"[{mark}{' '.join(bits)}]"


class Tag(Base):
    """分類標籤。

    axis:
      textbook   教科書章節（老師實際搜尋用的語言）
      curriculum 108 課綱學習內容代碼（跨版本通用）
      concept    知識點（細粒度，可多個）
    """

    __tablename__ = "tag"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    question_id: Mapped[str] = mapped_column(ForeignKey("question.id", ondelete="CASCADE"),
                                             index=True)
    axis: Mapped[str] = mapped_column(String(20), nullable=False, index=True)
    value: Mapped[str] = mapped_column(String(200), nullable=False, index=True)
    is_primary: Mapped[bool] = mapped_column(Boolean, default=False)
    confidence: Mapped[float | None] = mapped_column(Float)
    labeled_by: Mapped[str | None] = mapped_column(String(20))   # ai | human | source

    question: Mapped[Question] = relationship(back_populates="tags")

    __table_args__ = (
        UniqueConstraint("question_id", "axis", "value", name="uq_tag_unique"),
    )


# ─────────────────────────── 組卷 ───────────────────────────

class Paper(Base):
    __tablename__ = "paper"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    title: Mapped[str] = mapped_column(String(200), nullable=False)
    subject: Mapped[str | None] = mapped_column(String(40))
    owner: Mapped[str | None] = mapped_column(String(80))
    notes: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, onupdate=utcnow)

    items: Mapped[list["PaperItem"]] = relationship(
        back_populates="paper", cascade="all, delete-orphan", order_by="PaperItem.ord")

    @property
    def total_score(self) -> float:
        return sum(i.score or 0 for i in self.items)


class PaperItem(Base):
    __tablename__ = "paper_item"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    paper_id: Mapped[str] = mapped_column(ForeignKey("paper.id", ondelete="CASCADE"),
                                          index=True)
    question_id: Mapped[str] = mapped_column(ForeignKey("question.id", ondelete="CASCADE"))
    ord: Mapped[int] = mapped_column(Integer, nullable=False)
    score: Mapped[float | None] = mapped_column(Float)
    section_name: Mapped[str | None] = mapped_column(String(80))

    paper: Mapped[Paper] = relationship(back_populates="items")
    question: Mapped[Question] = relationship()

    __table_args__ = (
        UniqueConstraint("paper_id", "question_id", name="uq_paper_question"),
    )
