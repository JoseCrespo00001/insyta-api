"""initial schema + RLS

Revision ID: 0001_initial
Revises:
Create Date: 2026-04-27 23:30:00.000000

Creates the 8 core tenancy tables (organizations, projects, agents,
conversations, messages, evaluations, users, api_keys), enables RLS on the six
that hold tenant data, and installs `org_isolation` + `project_visibility`
policies that read `app.current_org` and `app.allowed_projects` GUCs.

UUIDv7 generation:
  Postgres 16 ships with `pgcrypto` (`gen_random_uuid()` -> v4). Native
  `uuidv7()` lands in PG18; until then we provide a SQL-only `uuid_generate_v7`
  helper so the column default can switch later without rewriting columns.
  TODO(EQUIP-58): swap to native once we migrate to PG18.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0001_initial"
down_revision: str | Sequence[str] | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


# ---------------------------------------------------------------------------
# UUIDv7 SQL helper (idempotent CREATE OR REPLACE).
# ---------------------------------------------------------------------------
UUIDV7_FN = """
CREATE OR REPLACE FUNCTION uuid_generate_v7()
RETURNS uuid AS $$
DECLARE
    unix_ts_ms bigint;
    uuid_bytes bytea;
BEGIN
    unix_ts_ms := (extract(epoch FROM clock_timestamp()) * 1000)::bigint;
    uuid_bytes := set_byte(gen_random_bytes(16), 6,
        ((get_byte(gen_random_bytes(1), 0) & 15) | 112)
    );
    uuid_bytes := set_byte(uuid_bytes, 0, ((unix_ts_ms >> 40) & 255)::int);
    uuid_bytes := set_byte(uuid_bytes, 1, ((unix_ts_ms >> 32) & 255)::int);
    uuid_bytes := set_byte(uuid_bytes, 2, ((unix_ts_ms >> 24) & 255)::int);
    uuid_bytes := set_byte(uuid_bytes, 3, ((unix_ts_ms >> 16) & 255)::int);
    uuid_bytes := set_byte(uuid_bytes, 4, ((unix_ts_ms >>  8) & 255)::int);
    uuid_bytes := set_byte(uuid_bytes, 5, ((unix_ts_ms      ) & 255)::int);
    uuid_bytes := set_byte(uuid_bytes, 8,
        ((get_byte(uuid_bytes, 8) & 63) | 128)
    );
    RETURN encode(uuid_bytes, 'hex')::uuid;
END;
$$ LANGUAGE plpgsql VOLATILE;
"""


# Tables that hold tenant-scoped data and must enforce RLS.
RLS_TABLES: tuple[str, ...] = (
    "organizations",
    "projects",
    "agents",
    "conversations",
    "messages",
    "evaluations",
)


def _enable_rls(table: str, *, has_org_id: bool = True) -> None:
    op.execute(f"ALTER TABLE {table} ENABLE ROW LEVEL SECURITY")
    op.execute(f"ALTER TABLE {table} FORCE ROW LEVEL SECURITY")
    if table == "organizations":
        op.execute(
            "CREATE POLICY org_isolation ON organizations "
            "USING (id = current_setting('app.current_org', true)::uuid)"
        )
    else:
        if has_org_id:
            op.execute(
                f"CREATE POLICY org_isolation ON {table} "
                "USING (org_id = current_setting('app.current_org', true)::uuid)"
            )
        if table in {"projects", "agents", "conversations", "messages", "evaluations"}:
            project_col = "id" if table == "projects" else "project_id"
            op.execute(
                f"CREATE POLICY project_visibility ON {table} "
                f"USING ({project_col} = ANY("
                "    string_to_array("
                "        current_setting('app.allowed_projects', true), ','"
                "    )::uuid[]"
                "))"
            )


def upgrade() -> None:
    # Required PG extensions.
    op.execute("CREATE EXTENSION IF NOT EXISTS pgcrypto")
    op.execute(UUIDV7_FN)

    # ---- organizations ----
    op.create_table(
        "organizations",
        sa.Column(
            "id",
            sa.dialects.postgresql.UUID(as_uuid=True),
            primary_key=True,
            server_default=sa.text("uuid_generate_v7()"),
        ),
        sa.Column("public_id", sa.String(64), nullable=False, unique=True),
        sa.Column("slug", sa.String(64), nullable=False, unique=True),
        sa.Column("name", sa.String(200), nullable=False),
        sa.Column("org_type", sa.String(16), nullable=False, server_default="customer"),
        sa.Column("plan", sa.String(32), nullable=False, server_default="free"),
        sa.Column("stripe_customer_id", sa.String(64)),
        sa.Column("stripe_subscription_id", sa.String(64)),
        sa.Column("white_label_config", sa.dialects.postgresql.JSONB),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.CheckConstraint(
            "org_type IN ('customer','agency')", name="ck_organizations_org_type_enum"
        ),
        sa.CheckConstraint(
            "plan IN ('free','starter','growth','business','agency','enterprise')",
            name="ck_organizations_plan_enum",
        ),
    )

    # ---- projects ----
    op.create_table(
        "projects",
        sa.Column(
            "id",
            sa.dialects.postgresql.UUID(as_uuid=True),
            primary_key=True,
            server_default=sa.text("uuid_generate_v7()"),
        ),
        sa.Column("public_id", sa.String(64), nullable=False, unique=True),
        sa.Column(
            "org_id",
            sa.dialects.postgresql.UUID(as_uuid=True),
            sa.ForeignKey("organizations.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("slug", sa.String(64), nullable=False),
        sa.Column("name", sa.String(200), nullable=False),
        sa.Column("description", sa.Text),
        sa.Column("environment", sa.String(8), nullable=False, server_default="live"),
        sa.Column("webhook_secret", sa.String(128), nullable=False),
        sa.Column("retention_days", sa.Integer, nullable=False, server_default="30"),
        sa.Column("alert_thresholds", sa.dialects.postgresql.JSONB),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.UniqueConstraint("org_id", "slug", name="uq_projects_org_id_slug"),
        sa.CheckConstraint(
            "environment IN ('live','test')", name="ck_projects_environment_enum"
        ),
    )
    op.create_index(
        "ix_projects_org_id_created_at",
        "projects",
        ["org_id", sa.text("created_at DESC")],
    )

    # ---- agents ----
    op.create_table(
        "agents",
        sa.Column(
            "id",
            sa.dialects.postgresql.UUID(as_uuid=True),
            primary_key=True,
            server_default=sa.text("uuid_generate_v7()"),
        ),
        sa.Column("public_id", sa.String(64), nullable=False, unique=True),
        sa.Column(
            "project_id",
            sa.dialects.postgresql.UUID(as_uuid=True),
            sa.ForeignKey("projects.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "org_id",
            sa.dialects.postgresql.UUID(as_uuid=True),
            sa.ForeignKey("organizations.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("slug", sa.String(64), nullable=False),
        sa.Column("name", sa.String(200), nullable=False),
        sa.Column("platform", sa.String(32), nullable=False),
        sa.Column("system_prompt", sa.Text),
        sa.Column("prompt_versions", sa.dialects.postgresql.JSONB),
        sa.Column("config", sa.dialects.postgresql.JSONB),
        sa.Column(
            "enabled", sa.Boolean, nullable=False, server_default=sa.text("true")
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.UniqueConstraint("project_id", "slug", name="uq_agents_project_id_slug"),
        sa.CheckConstraint(
            "platform IN ('wati','respondio','manychat','custom_sdk')",
            name="ck_agents_platform_enum",
        ),
    )
    op.create_index(
        "ix_agents_project_id_created_at",
        "agents",
        ["project_id", sa.text("created_at DESC")],
    )

    # ---- conversations ----
    op.create_table(
        "conversations",
        sa.Column(
            "id",
            sa.dialects.postgresql.UUID(as_uuid=True),
            primary_key=True,
            server_default=sa.text("uuid_generate_v7()"),
        ),
        sa.Column("public_id", sa.String(64), nullable=False, unique=True),
        sa.Column(
            "project_id",
            sa.dialects.postgresql.UUID(as_uuid=True),
            sa.ForeignKey("projects.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "org_id",
            sa.dialects.postgresql.UUID(as_uuid=True),
            sa.ForeignKey("organizations.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "agent_id",
            sa.dialects.postgresql.UUID(as_uuid=True),
            sa.ForeignKey("agents.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("external_id", sa.String(128), nullable=False),
        sa.Column("platform", sa.String(32), nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True)),
        sa.Column("ended_at", sa.DateTime(timezone=True)),
        sa.Column("message_count", sa.Integer, nullable=False, server_default="0"),
        sa.Column("status", sa.String(16), nullable=False, server_default="active"),
        sa.Column("phoenix_trace_id", sa.String(64)),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.UniqueConstraint(
            "agent_id", "external_id", name="uq_conversations_agent_id_external_id"
        ),
        sa.CheckConstraint(
            "status IN ('active','completed','abandoned','escalated')",
            name="ck_conversations_status_enum",
        ),
    )
    op.create_index(
        "ix_conversations_project_id_created_at",
        "conversations",
        ["project_id", sa.text("created_at DESC")],
    )
    op.create_index(
        "ix_conversations_project_id_agent_id_created_at",
        "conversations",
        ["project_id", "agent_id", sa.text("created_at DESC")],
    )

    # ---- messages ----
    op.create_table(
        "messages",
        sa.Column(
            "id",
            sa.dialects.postgresql.UUID(as_uuid=True),
            primary_key=True,
            server_default=sa.text("uuid_generate_v7()"),
        ),
        sa.Column("public_id", sa.String(64), nullable=False, unique=True),
        sa.Column(
            "conversation_id",
            sa.dialects.postgresql.UUID(as_uuid=True),
            sa.ForeignKey("conversations.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "project_id",
            sa.dialects.postgresql.UUID(as_uuid=True),
            sa.ForeignKey("projects.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "org_id",
            sa.dialects.postgresql.UUID(as_uuid=True),
            sa.ForeignKey("organizations.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("role", sa.String(16), nullable=False),
        sa.Column("content", sa.Text, nullable=False),
        sa.Column("content_anonymized", sa.Text),
        sa.Column("timestamp", sa.DateTime(timezone=True), nullable=False),
        sa.Column("metadata", sa.dialects.postgresql.JSONB),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.CheckConstraint(
            "role IN ('user','assistant','system')", name="ck_messages_role_enum"
        ),
    )
    op.create_index(
        "ix_messages_conversation_id_timestamp",
        "messages",
        ["conversation_id", "timestamp"],
    )

    # ---- evaluations ----
    op.create_table(
        "evaluations",
        sa.Column(
            "id",
            sa.dialects.postgresql.UUID(as_uuid=True),
            primary_key=True,
            server_default=sa.text("uuid_generate_v7()"),
        ),
        sa.Column("public_id", sa.String(64), nullable=False, unique=True),
        sa.Column(
            "conversation_id",
            sa.dialects.postgresql.UUID(as_uuid=True),
            sa.ForeignKey("conversations.id", ondelete="CASCADE"),
            nullable=False,
            unique=True,
        ),
        sa.Column(
            "project_id",
            sa.dialects.postgresql.UUID(as_uuid=True),
            sa.ForeignKey("projects.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "org_id",
            sa.dialects.postgresql.UUID(as_uuid=True),
            sa.ForeignKey("organizations.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "agent_id",
            sa.dialects.postgresql.UUID(as_uuid=True),
            sa.ForeignKey("agents.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("score", sa.Integer),
        sa.Column("resolution", sa.Boolean),
        sa.Column("satisfaction", sa.Integer),
        sa.Column("tone", sa.String(16)),
        sa.Column("frustration", sa.Boolean),
        sa.Column("escalated", sa.Boolean),
        sa.Column("efficiency", sa.Integer),
        sa.Column("scope_violation", sa.Boolean),
        sa.Column("topic", sa.String(128)),
        sa.Column("summary", sa.Text),
        sa.Column("model_used", sa.String(64)),
        sa.Column("tokens_used", sa.Integer),
        sa.Column("cost_usd", sa.Numeric(10, 6)),
        sa.Column("phoenix_span_id", sa.String(64)),
        sa.Column("evaluated_at", sa.DateTime(timezone=True)),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.CheckConstraint(
            "score IS NULL OR (score >= 0 AND score <= 100)",
            name="ck_evaluations_score_range",
        ),
        sa.CheckConstraint(
            "satisfaction IS NULL OR (satisfaction >= 1 AND satisfaction <= 5)",
            name="ck_evaluations_satisfaction_range",
        ),
        sa.CheckConstraint(
            "efficiency IS NULL OR (efficiency >= 1 AND efficiency <= 5)",
            name="ck_evaluations_efficiency_range",
        ),
        sa.CheckConstraint(
            "tone IS NULL OR tone IN ('positive','neutral','negative')",
            name="ck_evaluations_tone_enum",
        ),
    )
    op.create_index(
        "ix_evaluations_project_id_evaluated_at",
        "evaluations",
        ["project_id", sa.text("evaluated_at DESC")],
    )
    op.create_index(
        "ix_evaluations_agent_id_topic", "evaluations", ["agent_id", "topic"]
    )
    op.create_index(
        "ix_evaluations_project_id_score", "evaluations", ["project_id", "score"]
    )

    # ---- users ----
    # Not RLS-protected: identity table queried with service-role pre-tenant.
    op.create_table(
        "users",
        sa.Column(
            "id",
            sa.dialects.postgresql.UUID(as_uuid=True),
            primary_key=True,
            server_default=sa.text("uuid_generate_v7()"),
        ),
        sa.Column("public_id", sa.String(64), nullable=False, unique=True),
        sa.Column("supabase_user_id", sa.String(64), nullable=False, unique=True),
        sa.Column("email", sa.String(254), nullable=False, unique=True),
        sa.Column("full_name", sa.String(200)),
        sa.Column(
            "org_id",
            sa.dialects.postgresql.UUID(as_uuid=True),
            sa.ForeignKey("organizations.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("role", sa.String(16), nullable=False, server_default="member"),
        sa.Column("allowed_project_ids", sa.dialects.postgresql.JSONB),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.CheckConstraint(
            "role IN ('owner','admin','member','viewer')",
            name="ck_users_user_role_enum",
        ),
    )
    op.create_index("ix_users_org_id", "users", ["org_id"])

    # ---- api_keys ----
    op.create_table(
        "api_keys",
        sa.Column(
            "id",
            sa.dialects.postgresql.UUID(as_uuid=True),
            primary_key=True,
            server_default=sa.text("uuid_generate_v7()"),
        ),
        sa.Column("public_id", sa.String(64), nullable=False, unique=True),
        sa.Column(
            "org_id",
            sa.dialects.postgresql.UUID(as_uuid=True),
            sa.ForeignKey("organizations.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "project_id",
            sa.dialects.postgresql.UUID(as_uuid=True),
            sa.ForeignKey("projects.id", ondelete="CASCADE"),
        ),
        sa.Column("prefix", sa.String(32), nullable=False),
        sa.Column("key_hash", sa.String(128), nullable=False, unique=True),
        sa.Column("scope", sa.String(8), nullable=False),
        sa.Column("environment", sa.String(8), nullable=False),
        sa.Column("name", sa.String(200)),
        sa.Column("last_used_at", sa.DateTime(timezone=True)),
        sa.Column("revoked_at", sa.DateTime(timezone=True)),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.CheckConstraint(
            "scope IN ('sk','pk')", name="ck_api_keys_api_key_scope_enum"
        ),
        sa.CheckConstraint(
            "environment IN ('live','test')", name="ck_api_keys_api_key_env_enum"
        ),
    )
    op.create_index("ix_api_keys_org_id", "api_keys", ["org_id"])
    op.create_index("ix_api_keys_project_id", "api_keys", ["project_id"])

    # ---- RLS ----
    for table in RLS_TABLES:
        _enable_rls(table)


def downgrade() -> None:
    # Drop RLS policies first (PG will drop them with the table, but explicit
    # cleanup makes the downgrade idempotent across partial states).
    for table in RLS_TABLES:
        op.execute(f"DROP POLICY IF EXISTS org_isolation ON {table}")
        op.execute(f"DROP POLICY IF EXISTS project_visibility ON {table}")
        op.execute(f"ALTER TABLE {table} DISABLE ROW LEVEL SECURITY")

    # Drop tables in FK-reverse order.
    op.drop_index("ix_api_keys_project_id", table_name="api_keys")
    op.drop_index("ix_api_keys_org_id", table_name="api_keys")
    op.drop_table("api_keys")

    op.drop_index("ix_users_org_id", table_name="users")
    op.drop_table("users")

    op.drop_index("ix_evaluations_project_id_score", table_name="evaluations")
    op.drop_index("ix_evaluations_agent_id_topic", table_name="evaluations")
    op.drop_index("ix_evaluations_project_id_evaluated_at", table_name="evaluations")
    op.drop_table("evaluations")

    op.drop_index("ix_messages_conversation_id_timestamp", table_name="messages")
    op.drop_table("messages")

    op.drop_index(
        "ix_conversations_project_id_agent_id_created_at", table_name="conversations"
    )
    op.drop_index("ix_conversations_project_id_created_at", table_name="conversations")
    op.drop_table("conversations")

    op.drop_index("ix_agents_project_id_created_at", table_name="agents")
    op.drop_table("agents")

    op.drop_index("ix_projects_org_id_created_at", table_name="projects")
    op.drop_table("projects")

    op.drop_table("organizations")

    op.execute("DROP FUNCTION IF EXISTS uuid_generate_v7()")
