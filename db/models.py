"""
SQLAlchemy Core table definitions mirroring db/001_initial_schema.sql.

Server-side only. This package is deliberately outside ``services/``, which
IntelAvatar.spec bundles wholesale into the EXE — the client must never carry
SQLAlchemy or a Postgres driver.

Core tables rather than the ORM: ingestion is a set of bulk upserts with
dialect-specific ON CONFLICT clauses, and the ORM's unit-of-work adds identity
tracking that buys nothing here.
"""

from __future__ import annotations

from sqlalchemy import (
    BigInteger, Boolean, CheckConstraint, Column, Date, DateTime, ForeignKey,
    Identity, Integer, LargeBinary, MetaData, Numeric, SmallInteger, Table,
    Text, UniqueConstraint,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID

metadata = MetaData()

ENVIRONMENTS = ("production", "sim", "format_check", "dev")

# Kept in sync with avatar_silver_turn_status. Ranked because two unordered threads
# write a turn: the route reports the outcome it saw, the usage worker settles
# tokens milliseconds later with its own default of "completed".
TURN_STATUS_RANK = {"started": 0, "completed": 1, "failed": 2, "cancelled": 3}


# ------------------------------------------------------------------ bronze --
raw_event = Table(
    "avatar_bronze_raw_event", metadata,
    Column("event_id", UUID(as_uuid=True), primary_key=True),
    Column("event_type", Text, nullable=False),
    Column("schema_version", SmallInteger, nullable=False),
    Column("environment", Text, nullable=False),
    Column("occurred_at", DateTime(timezone=True), nullable=False),
    Column("received_at", DateTime(timezone=True), nullable=False),
    Column("user_name", Text, nullable=False),
    Column("app_version", Text, nullable=False, server_default=""),
    Column("payload", JSONB, nullable=False),
    Column("source_ref", Text, nullable=False, server_default=""),
)


# ------------------------------------------------------------- silver dims --
technology = Table(
    "avatar_silver_technology", metadata,
    Column("technology_id", SmallInteger, primary_key=True),
    Column("code", Text, nullable=False, unique=True),
    Column("label", Text, nullable=False),
)

agent = Table(
    "avatar_silver_agent", metadata,
    Column("agent_id", SmallInteger, primary_key=True),
    Column("code", Text, nullable=False, unique=True),
    Column("technology_id", SmallInteger,
           ForeignKey("avatar_silver_technology.technology_id"), nullable=False),
)

app_user = Table(
    "avatar_silver_app_user", metadata,
    Column("user_id", Integer, Identity(always=True), primary_key=True),
    Column("user_name", Text, nullable=False, unique=True),
    Column("first_seen", DateTime(timezone=True), nullable=False),
    Column("last_seen", DateTime(timezone=True), nullable=False),
)

support_case = Table(
    "avatar_silver_support_case", metadata,
    Column("case_id", Integer, Identity(always=True), primary_key=True),
    Column("case_nbr", Text, nullable=False, unique=True),
    Column("subject", Text, nullable=False, server_default=""),
    Column("issue_type", Text, nullable=False, server_default=""),
    Column("technology_id", SmallInteger,
           ForeignKey("avatar_silver_technology.technology_id"), nullable=False,
           server_default="0"),
    Column("first_seen", DateTime(timezone=True), nullable=False),
    Column("last_seen", DateTime(timezone=True), nullable=False),
)

llm_model = Table(
    "avatar_silver_llm_model", metadata,
    Column("model_id", Integer, Identity(always=True), primary_key=True),
    Column("model_name", Text, nullable=False, unique=True),
    Column("rate_input_per_mtok", Numeric(12, 6)),
    Column("rate_output_per_mtok", Numeric(12, 6)),
    Column("pricing_version", Text, nullable=False, server_default=""),
)

feature = Table(
    "avatar_silver_feature", metadata,
    Column("feature_id", SmallInteger, Identity(always=True), primary_key=True),
    Column("code", Text, nullable=False, unique=True),
    Column("label", Text, nullable=False, server_default=""),
)

turn_status = Table(
    "avatar_silver_turn_status", metadata,
    Column("status", Text, primary_key=True),
    Column("rank", SmallInteger, nullable=False, unique=True),
)

log_file = Table(
    "avatar_silver_log_file", metadata,
    Column("file_id", BigInteger, Identity(always=True), primary_key=True),
    Column("sha256", LargeBinary, nullable=False, unique=True),
    Column("byte_size", BigInteger),
    Column("file_name", Text, nullable=False, server_default=""),
    Column("storage_uri", Text, nullable=False, server_default=""),
    Column("first_seen", DateTime(timezone=True), nullable=False),
)


# ---------------------------------------------------------- silver entities --
workflow = Table(
    "avatar_silver_workflow", metadata,
    Column("workflow_id", UUID(as_uuid=True), primary_key=True),
    Column("user_id", Integer, ForeignKey("avatar_silver_app_user.user_id"),
           nullable=False),
    Column("case_id", Integer, ForeignKey("avatar_silver_support_case.case_id")),
    Column("technology_id", SmallInteger,
           ForeignKey("avatar_silver_technology.technology_id"), nullable=False,
           server_default="0"),
    Column("environment", Text, nullable=False),
    Column("app_version", Text, nullable=False, server_default=""),
    Column("started_at", DateTime(timezone=True), nullable=False),
    Column("updated_at", DateTime(timezone=True), nullable=False),
    # The attachment claim and its provenance. An empty declaration_source
    # means the attachment AI never ran for this workflow — distinct from it
    # running and being unable to decide, which leaves a source but a NULL
    # verdict. See db/003_attachment_declaration.sql.
    Column("declared_attached", Boolean),
    Column("declaration_source", Text, nullable=False, server_default=""),
    Column("declaration_confidence", Text, nullable=False, server_default=""),
    Column("declaration_conflict", Boolean, nullable=False, server_default="false"),
    CheckConstraint(f"environment IN {ENVIRONMENTS}", name="workflow_environment_ck"),
    CheckConstraint("declared_attached IS NULL OR declaration_source <> ''",
                    name="workflow_declaration_ck"),
)

conversation = Table(
    "avatar_silver_conversation", metadata,
    Column("conversation_id", UUID(as_uuid=True), primary_key=True),
    Column("workflow_id", UUID(as_uuid=True),
           ForeignKey("avatar_silver_workflow.workflow_id")),
    Column("http_session_id", Text, nullable=False, server_default=""),
    Column("user_id", Integer, ForeignKey("avatar_silver_app_user.user_id"),
           nullable=False),
    Column("case_id", Integer, ForeignKey("avatar_silver_support_case.case_id")),
    Column("case_ref_source", Text, nullable=False, server_default="absent"),
    Column("agent_id", SmallInteger, ForeignKey("avatar_silver_agent.agent_id"),
           nullable=False, server_default="0"),
    Column("technology_id", SmallInteger,
           ForeignKey("avatar_silver_technology.technology_id"), nullable=False,
           server_default="0"),
    Column("environment", Text, nullable=False),
    Column("app_version", Text, nullable=False, server_default=""),
    Column("started_at", DateTime(timezone=True), nullable=False),
    Column("updated_at", DateTime(timezone=True), nullable=False),
    Column("issue_time", DateTime(timezone=True)),
    Column("issue_window_minutes", SmallInteger),
    Column("primary_file_id", BigInteger, ForeignKey("avatar_silver_log_file.file_id")),
    CheckConstraint("case_ref_source IN ('explicit','derived_from_path','absent')",
                    name="conversation_case_ref_ck"),
    CheckConstraint("(case_ref_source = 'absent') = (case_id IS NULL)",
                    name="conversation_case_consistency_ck"),
)

turn = Table(
    "avatar_silver_turn", metadata,
    Column("turn_id", UUID(as_uuid=True), primary_key=True),
    Column("conversation_id", UUID(as_uuid=True),
           ForeignKey("avatar_silver_conversation.conversation_id", ondelete="CASCADE"),
           nullable=False),
    Column("seq", Integer),
    Column("status", Text, ForeignKey("avatar_silver_turn_status.status"), nullable=False),
    Column("error_code", Text, nullable=False, server_default=""),
    Column("model_id", Integer, ForeignKey("avatar_silver_llm_model.model_id")),
    Column("input_tokens", BigInteger, nullable=False, server_default="0"),
    Column("cache_read_tokens", BigInteger, nullable=False, server_default="0"),
    Column("cache_write_tokens", BigInteger, nullable=False, server_default="0"),
    Column("output_tokens", BigInteger, nullable=False, server_default="0"),
    # total_tokens is GENERATED ALWAYS in the DDL; never written from Python.
    Column("cost_usd", Numeric(12, 6)),
    Column("unpriced_model", Text, nullable=False, server_default=""),
    Column("rate_input_per_mtok", Numeric(12, 6)),
    Column("rate_output_per_mtok", Numeric(12, 6)),
    Column("pricing_version", Text, nullable=False, server_default=""),
    Column("latency_ms", Integer),
    Column("started_at", DateTime(timezone=True), nullable=False),
    Column("settled_at", DateTime(timezone=True)),
)

ai_invocation = Table(
    "avatar_silver_ai_invocation", metadata,
    Column("invocation_id", UUID(as_uuid=True), primary_key=True),
    Column("workflow_id", UUID(as_uuid=True),
           ForeignKey("avatar_silver_workflow.workflow_id", ondelete="CASCADE"),
           nullable=False),
    Column("conversation_id", UUID(as_uuid=True),
           ForeignKey("avatar_silver_conversation.conversation_id")),
    Column("feature_id", SmallInteger, ForeignKey("avatar_silver_feature.feature_id"),
           nullable=False),
    Column("agent_id", SmallInteger, ForeignKey("avatar_silver_agent.agent_id"),
           nullable=False, server_default="0"),
    Column("model_id", Integer, ForeignKey("avatar_silver_llm_model.model_id")),
    Column("input_tokens", BigInteger, nullable=False, server_default="0"),
    Column("cache_read_tokens", BigInteger, nullable=False, server_default="0"),
    Column("cache_write_tokens", BigInteger, nullable=False, server_default="0"),
    Column("output_tokens", BigInteger, nullable=False, server_default="0"),
    Column("cost_usd", Numeric(12, 6)),
    Column("unpriced_model", Text, nullable=False, server_default=""),
    Column("pricing_version", Text, nullable=False, server_default=""),
    Column("status", Text, nullable=False, server_default="success"),
    Column("error_code", Text, nullable=False, server_default=""),
    Column("latency_ms", Integer),
    Column("occurred_at", DateTime(timezone=True), nullable=False),
)

attachment_event = Table(
    "avatar_silver_attachment_event", metadata,
    Column("attachment_event_id", UUID(as_uuid=True), primary_key=True),
    Column("workflow_id", UUID(as_uuid=True),
           ForeignKey("avatar_silver_workflow.workflow_id", ondelete="CASCADE"),
           nullable=False),
    Column("file_id", BigInteger, ForeignKey("avatar_silver_log_file.file_id")),
    Column("declared_name", Text, nullable=False, server_default=""),
    Column("log_family", Text, nullable=False, server_default=""),
    Column("was_selected", Boolean, nullable=False, server_default="false"),
    Column("download_status", Text, nullable=False, server_default="not_attempted"),
    Column("byte_size", BigInteger),
    Column("latency_ms", Integer),
    Column("attempt_count", Integer),
    Column("error_code", Text, nullable=False, server_default=""),
    Column("occurred_at", DateTime(timezone=True), nullable=False),
)

feedback_event = Table(
    "avatar_silver_feedback_event", metadata,
    Column("feedback_event_id", UUID(as_uuid=True), primary_key=True),
    Column("conversation_id", UUID(as_uuid=True),
           ForeignKey("avatar_silver_conversation.conversation_id")),
    Column("turn_id", UUID(as_uuid=True), ForeignKey("avatar_silver_turn.turn_id")),
    Column("workflow_id", UUID(as_uuid=True),
           ForeignKey("avatar_silver_workflow.workflow_id")),
    Column("case_id", Integer, ForeignKey("avatar_silver_support_case.case_id")),
    Column("user_id", Integer, ForeignKey("avatar_silver_app_user.user_id"),
           nullable=False),
    Column("environment", Text, nullable=False),
    Column("submitted_at", DateTime(timezone=True), nullable=False),
)
