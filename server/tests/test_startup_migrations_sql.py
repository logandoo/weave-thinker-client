"""回归测试：STARTUP_MIGRATIONS 的 SQL 必须是合法 PostgreSQL。

根因（2026-09-14 Docker 全新库冒烟）：CREATE TABLE 字符串里混入 MySQL 风格
`#` 注释 → asyncpg.exceptions.PostgresSyntaxError: syntax error at or near "#"
（生产库因 migration_versions 台账已记录而跳过，缺陷只在全新部署暴露）。

Run from backend/:  ../.venv/bin/python -m pytest tests/test_startup_migrations_sql.py -v
"""
import re

from app.db.migrations import STARTUP_MIGRATIONS

# PostgreSQL 合法 `#` 用法：JSON 操作符 #> / #>>；迁移 SQL 中暂无使用，
# 保留白名单以防将来出现。
_ALLOWED_HASH = re.compile(r"#[<>]")
# 单引号字符串字面量（含 '' 转义）先剥离——DEFAULT '#3b82f6' 等合法值不参与扫描
_SQL_STRING = re.compile(r"'(?:[^']|'')*'")


class TestStartupMigrationsSQL:
    def test_no_hash_comments_in_migration_sql(self):
        offenders = []
        for version, statement in STARTUP_MIGRATIONS:
            cleaned = _SQL_STRING.sub("", statement)
            cleaned = _ALLOWED_HASH.sub("", cleaned)
            if "#" in cleaned:
                offenders.append(version)
        assert not offenders, (
            "STARTUP_MIGRATIONS 含非 PostgreSQL 注释 '#'（应改 '--'）: "
            + ", ".join(offenders)
        )

    def test_provider_migration_drops_constraint_before_index(self):
        """A4.9 Critical 回归（2026-09-15）：全新库 create_all 先生成 ORM
        UniqueConstraint（约束型索引）——DROP INDEX 会报 "cannot drop index
        ... because constraint ... requires it" 炸启动。DROP CONSTRAINT 必须
        排在 DROP INDEX 之前（独立索引场景 DROP CONSTRAINT 为 no-op）。"""
        names = [v for v, _ in STARTUP_MIGRATIONS]
        assert "user_model_providers_drop_kind_constraint" in names, (
            "缺少 DROP CONSTRAINT 条目——全新库升级路径会在启动迁移崩溃"
        )
        assert "user_model_providers_drop_kind_unique" in names
        assert names.index("user_model_providers_drop_kind_constraint") < names.index(
            "user_model_providers_drop_kind_unique"
        ), "DROP CONSTRAINT 必须在 DROP INDEX 之前执行"
        stmt = dict(STARTUP_MIGRATIONS)["user_model_providers_drop_kind_constraint"]
        assert "DROP CONSTRAINT IF EXISTS uq_user_model_providers_user_kind" in stmt

    def test_provider_column_not_null_enforced(self):
        """provider 列必须收敛为 NOT NULL DEFAULT ''（NULL 在唯一索引中互不
        相等 → 可绕过 (user_id,kind,provider) 唯一性）。"""
        names = [v for v, _ in STARTUP_MIGRATIONS]
        assert "user_model_providers_provider_not_null_enforce" in names
        stmt = dict(STARTUP_MIGRATIONS)["user_model_providers_provider_not_null_enforce"]
        assert "SET NOT NULL" in stmt

    def test_sync_tombstone_payload_migrations_present(self):
        """W9 项9（复审 B4）：两条迁移必须存在（删条目即红——placement 由后缀
        纯度测试与 w9 文件断言双重覆盖）。"""
        names = [v for v, _ in STARTUP_MIGRATIONS]
        for v in ("sync_tombstones_payload", "sync_tombstones_payload_backfill"):
            assert v in names, f"缺启动迁移 {v}"
        assert "ADD COLUMN IF NOT EXISTS payload JSONB" in dict(
            STARTUP_MIGRATIONS)["sync_tombstones_payload"]

    def test_pgvector_skip_window_suffix_purity(self):
        """§9.5 跳过窗守卫（W4 项 4，2026-10-06）：run_startup_migrations 在
        pgvector 不可用时跳过 `_MEMORY_MIGRATION_START`（pgvector_extension）起的
        **连续后缀**、不记版本号。因此后缀必须纯 memory-v2——与 memory v2 无关的
        迁移一律置于标记之前（migrations.py 区域注释约定），否则无 pgvector 库
        升级后缺列/缺表（delivery_status/message_attempts/message_payloads 等
        UndefinedColumn 族，W4 实锤 22 条被误置）。

        新增 memory-v2 迁移 → 扩充 _MEMORY_V2_SUFFIX 白名单（有意摩擦）；
        新增非 memory 迁移 → 放到 pgvector_extension 之前，勿改本守卫。"""
        from app.db.migrations import _MEMORY_MIGRATION_START

        names = [v for v, _ in STARTUP_MIGRATIONS]
        assert names.count(_MEMORY_MIGRATION_START) == 1, (
            "pgvector_extension 标记必须恰好出现一次")
        mem_start = names.index(_MEMORY_MIGRATION_START)
        suffix = names[mem_start + 1:]
        offenders = [v for v in suffix if v not in _MEMORY_V2_SUFFIX]
        assert not offenders, (
            "非 memory-v2 迁移落入 pgvector 跳过窗（无 pgvector 库升级会丢它们）——"
            "请移到 pgvector_extension 之前: " + ", ".join(offenders))
        missing = sorted(_MEMORY_V2_SUFFIX - set(suffix))
        assert not missing, (
            "白名单有但后缀缺失的 memory-v2 版本名（要么被挪出后缀需回位，要么"
            "白名单过期需同步）: " + ", ".join(missing))


    def test_migration_index_never_shadows_model_index_unreconciled(self):
        """双索引守卫（W4 项6a，2026-10-06）：model Column(index=True) 由 create_all
        建 ix_<table>_<col>；迁移若再建同 (table,col) 的**异名**索引，全新库会拿到
        双索引（写放大）。异名遮蔽条目必须被更靠后的迁移 DROP 掉，且模型名 ix_
        必须有 create 路径（存量库 create_all 不补索引）。

        命中面刻意收窄到「单列 CREATE INDEX IF NOT EXISTS <name> ON <table>(<col>)」
        简形——复合/表达式/USING 索引语义不同不在此列。"""
        import re

        from app.db.database import Base

        simple = re.compile(r"^CREATE INDEX IF NOT EXISTS (\w+) ON (\w+)\((\w+)\)$")
        model_ix = {}
        for table in Base.metadata.tables.values():
            for col in table.columns:
                if col.index:
                    model_ix[(table.name, col.name)] = f"ix_{table.name}_{col.name}"

        entries = [(i, v, s) for i, (v, s) in enumerate(STARTUP_MIGRATIONS)]
        offenders = []
        for i, version, stmt in entries:
            m = simple.match(stmt.strip())
            if not m:
                continue
            name, table, col = m.groups()
            expected = model_ix.get((table, col))
            if not expected or name == expected:
                continue
            later = [s for j, _, s in entries if j > i]
            # \b 词边界防前缀碰撞（DROP INDEX idx_foo 不得匹配 idx_foo_bar）；
            # create 也必须在遮蔽条目之后——存量库只重跑未记录的后段条目，
            # 早于遮蔽位的 create 不能作为 legacy 补建路径。
            dropped = any(
                re.search(rf"\bDROP INDEX {name}(?![\w])", s) for s in later)
            created = any(
                re.search(rf"\bCREATE INDEX (?:IF NOT EXISTS )?{expected}(?![\w])", s)
                for s in later)
            if not (dropped and created):
                offenders.append(
                    f"{version} 建 {name}（遮蔽模型 {expected}；"
                    f"drop={'有' if dropped else '无'}, create={'有' if created else '无'}）")
        assert not offenders, "模型索引被异名迁移索引遮蔽且未 reconcile: " + "; ".join(offenders)
    def test_no_redundant_index_pairs(self):
        """复合/前缀冗余守卫（W4 Minor-4 二波，2026-10-06）：同一表上——
        (a) 两索引同列组：存活者（无后续 DROP）不得超过 1 个，非唯一方必须被
            后续迁移 DROP（唯一侧永不为被 drop 方）；
        (b) 非唯一索引的列组是另一存活索引列组的严格前缀：短方必须被 DROP
            （长索引可服务前缀查询；短方是纯写放大）。
        同名条目（model index=True 与迁移同名 create）视为同一索引；
        部分索引（WHERE）/USING 表达式索引不参与；drop 证据须在定义位之后
        （早于定义的 drop 会被 create 复活）。"""
        import re

        from sqlalchemy import UniqueConstraint
        from app.db.database import Base

        by_name = {}  # name -> (table, cols, unique, defined_at)

        def put(name, table, cols, unique, at):
            if not name:
                name = f"unnamed:{table}:{','.join(cols)}:{at}"
            cur = by_name.get(name)
            by_name[name] = (table, cols, unique, max(at, cur[3]) if cur else at)

        for table in Base.metadata.tables.values():
            cols_seen = {}
            for col in table.columns:
                if col.unique or col.index:
                    cols_seen[(col.name,)] = bool(col.unique)
                    put(f"ix_{table.name}_{col.name}", table.name, (col.name,), bool(col.unique), -1)
            for cons in table.constraints:
                if isinstance(cons, UniqueConstraint):
                    cols = tuple(c.name for c in cons.columns)
                    if cols not in cols_seen:
                        cols_seen[cols] = True
                        put(cons.name or f"uq_{table.name}_{','.join(cols)}",
                            table.name, cols, True, -1)
            pk = tuple(c.name for c in table.primary_key.columns)
            if pk and pk not in cols_seen:
                cols_seen[pk] = True
                put(f"pk_{table.name}", table.name, pk, True, -1)
            for idx in table.indexes:
                partial = bool(getattr(idx, "dialect_options", {}).get("postgresql", {}).get("where")) \
                    or getattr(idx, "partial", False)
                if partial:
                    continue
                cols = tuple(c.name for c in idx.columns)
                if cols in cols_seen and cols_seen[cols] == bool(idx.unique):
                    continue
                put(idx.name or f"idx_{table.name}_{','.join(cols)}",
                    table.name, cols, bool(idx.unique), -1)

        idx_stmt = re.compile(
            r"^CREATE (UNIQUE )?INDEX (?:IF NOT EXISTS )?(\w+) ON (\w+)\(([^)]+)\)$")
        for i, (version, stmt) in enumerate(STARTUP_MIGRATIONS):
            m = idx_stmt.match(stmt.strip())
            if not m or " WHERE " in stmt or " USING " in stmt:
                continue
            unique, name, table, cols = m.groups()
            put(name, table, tuple(c.strip() for c in cols.split(",")), bool(unique), i)

        def dropped(name, after):
            pat = re.compile(rf"\bDROP INDEX {re.escape(name)}(?![\w])")
            return any(pat.search(s) for j, (_, s) in enumerate(STARTUP_MIGRATIONS) if j > after)

        survivors = [(n, t, c, u) for n, (t, c, u, at) in by_name.items() if not dropped(n, at)]
        offenders = []
        groups = {}
        for n, t, c, u in survivors:
            groups.setdefault((t, c), []).append((n, u))
        for (t, c), members in groups.items():
            if len(members) > 1:
                non_unique = [n for n, u in members if not u]
                for n in (non_unique or [m[0] for m in members]):
                    offenders.append(f"{t}{c} 同列组存活 {len(members)} 个索引，冗余方 {n} 未 DROP")
        for i, (na, ta, ca, ua) in enumerate(survivors):
            for nb, tb, cb, ub in survivors[i + 1:]:
                if ta != tb or ua and ub:
                    continue
                short, long = (ca, cb) if len(ca) < len(cb) else (cb, ca)
                n_short, u_short = (na, ua) if len(ca) < len(cb) else (nb, ub)
                if len(short) < len(long) and long[:len(short)] == short and not u_short:
                    offenders.append(
                        f"{ta}{short} 是 {long} 的严格前缀冗余 {n_short}（短方未 DROP）")
        assert not offenders, "索引冗余对（同列组/严格前缀）未 reconcile: " + "; ".join(offenders)


# §9.5 跳过窗后缀白名单（唯一事实源）：pgvector_extension 之后仅允许这些
# memory-v2 版本名。存量 48 条（2026-10-06 W4 归位后）；新增 memory-v2 迁移
# 时在此登记。
_MEMORY_V2_SUFFIX = frozenset({
    "create_memory_concepts",
    "idx_concepts_user_status",
    "idx_concepts_user_weight",
    "idx_concepts_user_recalled",
    "idx_concepts_needs_review",
    "idx_concepts_embedding",
    "create_memory_clusters",
    "idx_clusters_user",
    "create_concept_cluster_members",
    "idx_ccm_cluster",
    "create_concept_relations",
    "idx_rel_source",
    "idx_rel_target",
    "idx_rel_user",
    "create_memory_clarifications",
    "idx_clar_user",
    "idx_clar_applied",
    "idx_clar_user_redundant_drop",
    "create_subconscious_log",
    "idx_sub_user_created",
    "idx_sub_user_unpromoted",
    "idx_sub_embedding",
    "add_subconscious_recurrence_scan_count",
    "add_concept_importance",
    "add_concept_importance_evaluated",
    "create_memory_episodes",
    "idx_epi_user_created",
    "idx_epi_embedding",
    "mc_last_recurrence_at",
    "me_last_recalled_at",
    "me_source_type",
    "mc_weight_decayed_at",
    "idx_epi_source_type",
    "create_memory_llm_calls",
    "idx_mlc_user_ts",
    "mc_cluster_embedding_model",
    "mlc_billing_class",
    "mc_concept_embedding_model",
    "me_embedding_model",
    "sub_embedding_model",
    "sub_content_hash",
    "sub_needs_embedding",
    "sub_flag_missing_embeddings",
    "idx_sub_content_hash",
    "mrl_create",
    "mrl_idx_user_created",
    "me_participants",
    "me_locations",
    "cr_edge_source",
})
