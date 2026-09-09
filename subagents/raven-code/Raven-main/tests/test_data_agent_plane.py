"""DuckDB evidence plane and the four L1 tools, against a real fixture.

Builds a two-store fixture (a DuckDB file + a SQLite file with a prefix-mangled
join key) so the tools run over the same shape DataAgentBench uses, then checks
the read-only gate, profiling (sentinel + type-mix), distinct values, and the
join probe (match rate + fan-out).
"""

from __future__ import annotations

import duckdb
import pytest

from raven.plugin.context import PluginContext, ServiceLocator
from raven.plugin.data_agent import tools as dat
from raven.plugin.data_agent.session import DataAgentSession, SourceSpec
from raven.plugin.data_agent.sqlgate import SqlRejectedError, ensure_read_only, wrap_with_cap


@pytest.fixture
def stores(tmp_path):
    sales = tmp_path / "sales.duckdb"
    con = duckdb.connect(str(sales))
    con.execute("CREATE TABLE orders (order_id INTEGER, book_ref VARCHAR, amount DOUBLE)")
    con.execute(
        "INSERT INTO orders VALUES (1,'bref_1',10.0),(2,'bref_1',5.0),(3,'bref_2',7.0),(4,'bref_9',1.0)"
    )
    con.close()
    meta = tmp_path / "meta.db"
    con = duckdb.connect()
    con.execute("INSTALL sqlite_scanner; LOAD sqlite_scanner;")
    con.execute(f"ATTACH '{meta}' AS m (TYPE sqlite)")
    con.execute("CREATE TABLE m.books (book_id VARCHAR, rating VARCHAR)")
    # rating mixes numbers and a sentinel; book_id is prefixed differently from book_ref
    con.execute("INSERT INTO m.books VALUES ('bid_1','5'),('bid_2','NA'),('bid_3','4')")
    con.close()
    return sales, meta


@pytest.fixture
def session(stores, tmp_path):
    sales, meta = stores
    srcs = (
        SourceSpec(alias="sales", kind="duckdb", ref=str(sales)),
        SourceSpec(alias="meta", kind="sqlite", ref=str(meta)),
    )
    return DataAgentSession(workspace=tmp_path, sources=srcs)


def _ctx(session, tmp_path):
    # A PluginContext whose factory would resolve to this exact session.
    return PluginContext(
        config={},
        services=ServiceLocator(workspace=tmp_path),
    )


# ---- read-only gate ----

@pytest.mark.parametrize(
    "bad",
    [
        "INSERT INTO orders VALUES (9,'x',1)",
        "UPDATE orders SET amount=0",
        "DROP TABLE orders",
        "ATTACH 'x.db' AS y",
        "SELECT 1; DROP TABLE orders",
        "COPY orders TO 'out.csv'",
    ],
)
def test_gate_rejects_writes_and_stacking(bad):
    with pytest.raises(SqlRejectedError):
        ensure_read_only(bad)


@pytest.mark.parametrize(
    "ok",
    [
        "SELECT * FROM orders",
        "WITH t AS (SELECT 1 x) SELECT * FROM t",
        "SELECT count(*) FROM orders WHERE amount > 1  -- trailing comment",
    ],
)
def test_gate_allows_reads(ok):
    ensure_read_only(ok)  # must not raise


def test_cap_is_cte_aware():
    wrapped = wrap_with_cap("WITH t AS (SELECT 1 x) SELECT * FROM t", 10)
    assert duckdb.connect().sql(wrapped).fetchall() == [(1,)]


# ---- tools ----

async def test_sql_tool_preview_and_reject(session, tmp_path):
    tool = dat.SqlTool(session)
    out = await tool.execute("SELECT * FROM sales.orders ORDER BY order_id")
    assert "rows=4" in out and "bref_1" in out
    rejected = await tool.execute("DELETE FROM sales.orders")
    assert rejected.startswith("[SQL REJECTED]")


async def test_sql_empty_result_note(session):
    tool = dat.SqlTool(session)
    out = await tool.execute("SELECT * FROM sales.orders WHERE amount > 999")
    assert "rows=0" in out and "Empty is a valid answer" in out


async def test_profile_flags_sentinel_and_type_mix(session):
    tool = dat.ProfileTool(session)
    out = await tool.execute("meta.books", column="rating")
    assert "SENTINEL?" in out  # 'NA' surfaces
    assert "MIXED-TYPE?" in out  # '5','4' numeric but 'NA' not


async def test_distinct_values(session):
    tool = dat.DistinctValuesTool(session)
    out = await tool.execute("sales.orders", "book_ref")
    assert "bref_1" in out and "n_distinct=3" in out


async def test_python_tool_keeps_state_and_shares_con(session):
    tool = dat.PythonTool(session)
    first = await tool.execute("x = 41")
    assert "PYTHON ERROR" not in first
    second = await tool.execute("x + 1")
    assert "42" in second
    via_con = await tool.execute("con.sql('SELECT COUNT(*) FROM sales.orders').fetchone()[0]")
    assert "4" in via_con


async def test_python_tool_auto_inspects_dataframe(session):
    tool = dat.PythonTool(session)
    out = await tool.execute("df = con.sql('SELECT * FROM sales.orders').df()\ndf")
    assert "[AUTO-INSPECT]" in out and "shape=(4," in out


async def test_python_tool_verify_flags_empty_filter(session):
    tool = dat.PythonTool(session)
    await tool.execute("import pandas as pd\nbase = pd.DataFrame({'a': [1, 2, 3]})")
    out = await tool.execute("filtered = base[base['a'] > 99]")
    assert "[VERIFY: WARN]" in out and "filtered" in out


async def test_python_tool_verify_checks_merge_fanout(session):
    tool = dat.PythonTool(session)
    await tool.execute(
        "import pandas as pd\n"
        "left = pd.DataFrame({'k': [1, 1, 1]})\n"
        "right = pd.DataFrame({'k': [1, 1, 1]})"
    )
    out = await tool.execute("merged = left.merge(right, on='k')")
    assert "[VERIFY: FAIL] merge" in out


async def test_python_tool_reports_restart_after_kernel_death(session):
    tool = dat.PythonTool(session)
    await tool.execute("y = 7")
    crashed = await tool.execute("import os; os._exit(1)")
    assert "variable" in crashed.lower() or "kernel" in crashed.lower()
    after = await tool.execute("'y' in dir()")
    assert "KERNEL RESTARTED" in after
    assert "False" in after


async def test_sql_save_as_writes_parquet_handle(session):
    tool = dat.SqlTool(session)
    out = await tool.execute("SELECT * FROM sales.orders", save_as="orders_all")
    assert "orders_all.parquet" in out
    again = await tool.execute(
        f"SELECT COUNT(*) AS n FROM read_parquet('{session.artifact_dir / 'orders_all.parquet'}')"
    )
    assert "4" in again


def test_parse_sources_reads_sources_file(tmp_path):
    import json

    from raven.plugin.data_agent.session import parse_sources

    manifest = tmp_path / "dab_sources.json"
    manifest.write_text(json.dumps([{"alias": "meta", "kind": "sqlite", "path": "/dab/data/m.db"}]))
    specs = parse_sources({"sources": [{"alias": "a", "kind": "duckdb", "path": "/x.duckdb"}], "sources_file": str(manifest)})
    assert [s.alias for s in specs] == ["a", "meta"]
    assert specs[1].ref == "/dab/data/m.db"


async def test_semantic_map_materializes_joinable_table(session):
    from raven.plugin.data_agent.semantic_map import SemanticMapTool

    class FakeProvider:
        async def chat(self, messages, model=None, max_tokens=0, temperature=0.0):
            import json as _json
            from types import SimpleNamespace

            items = _json.loads(messages[0]["content"].split("ITEMS:\n", 1)[1])
            payload = [{"id": it["id"], "value": "big" if "9" in it["text"] else "small"} for it in items]
            return SimpleNamespace(content=_json.dumps(payload))

    tool = SemanticMapTool(session, FakeProvider())
    out = await tool.execute(
        table="sales.orders",
        id_column="order_id",
        text_column="book_ref",
        task="size of the book id",
        output_table="ref_size",
    )
    assert "materialized ref_size" in out and "4 rows, 4 with a value" in out
    joined = await dat.SqlTool(session).execute(
        "SELECT value, COUNT(*) c FROM ref_size GROUP BY 1 ORDER BY 1"
    )
    assert "big" in joined and "small" in joined


async def test_semantic_map_labels_vote_and_tie_break(session):
    from raven.plugin.data_agent.semantic_map import SemanticMapTool

    class SplitProvider:
        def __init__(self):
            self.calls = 0

        async def chat(self, messages, model=None, max_tokens=0, temperature=0.0):
            import json as _json
            from types import SimpleNamespace

            self.calls += 1
            prompt = messages[0]["content"]
            items = _json.loads(prompt.split("ITEMS:\n", 1)[1])
            second_pass = "second, independent" in prompt
            payload = []
            for it in items:
                # The second annotator flips exactly one row; the tie-break
                # pass sides with the first, so majority restores "keep".
                if second_pass and it["id"] == "1":
                    payload.append({"id": it["id"], "value": "flip"})
                else:
                    payload.append({"id": it["id"], "value": "keep"})
            return SimpleNamespace(content=_json.dumps(payload))

    provider = SplitProvider()
    tool = SemanticMapTool(session, provider)
    out = await tool.execute(
        table="sales.orders",
        id_column="order_id",
        text_column="book_ref",
        task="keep or flip",
        labels=["keep", "flip"],
        output_table="voted",
    )
    assert "4 rows, 4 with a value" in out
    assert "1 row(s) disagreed" in out and "1 resolved by majority" in out
    # One batch: first pass + sampled second pass + tie-break for the flipped row.
    assert provider.calls == 3
    values = await dat.SqlTool(session).execute("SELECT DISTINCT value FROM voted")
    assert "keep" in values and "flip" not in values


async def test_semantic_map_consistent_labels_skip_full_second_pass(session):
    from raven.plugin.data_agent.semantic_map import SemanticMapTool

    class SteadyProvider:
        def __init__(self):
            self.calls = 0

        async def chat(self, messages, model=None, max_tokens=0, temperature=0.0):
            import json as _json
            from types import SimpleNamespace

            self.calls += 1
            items = _json.loads(messages[0]["content"].split("ITEMS:\n", 1)[1])
            return SimpleNamespace(content=_json.dumps([{"id": it["id"], "value": "same"} for it in items]))

    ids = [[str(i), f"text {i}"] for i in range(450)]
    session.put_rows("many_rows", ["rid", "body"], ids)
    provider = SteadyProvider()
    tool = SemanticMapTool(session, provider)
    out = await tool.execute(
        table="many_rows",
        id_column="rid",
        text_column="body",
        task="same or not",
        labels=["same", "other"],
        output_table="steady",
    )
    assert "450 rows, 450 with a value" in out
    assert "sampled check only" in out and "0 row(s) disagreed" in out
    # 5 first-pass batches (100 rows each) + 3 sampled second passes,
    # no escalation, no tie-breaks.
    assert provider.calls == 8


async def test_semantic_map_open_extraction_stays_single_pass(session):
    from raven.plugin.data_agent.semantic_map import SemanticMapTool

    class CountingProvider:
        def __init__(self):
            self.calls = 0

        async def chat(self, messages, model=None, max_tokens=0, temperature=0.0):
            import json as _json
            from types import SimpleNamespace

            self.calls += 1
            items = _json.loads(messages[0]["content"].split("ITEMS:\n", 1)[1])
            return SimpleNamespace(content=_json.dumps([{"id": it["id"], "value": "v"} for it in items]))

    provider = CountingProvider()
    tool = SemanticMapTool(session, provider)
    out = await tool.execute(
        table="sales.orders",
        id_column="order_id",
        text_column="book_ref",
        task="anything",
        output_table="single_pass",
    )
    assert "4 rows, 4 with a value" in out
    assert provider.calls == 1


async def test_resolve_entities_clusters_variants(session):
    from raven.plugin.data_agent.entity_resolution import ResolveEntitiesTool

    rows = []
    for v, n in [
        ("Zo gaat het leven aan je voor", 3),
        ("zo gaat het leven aan je vóór", 2),
        ("Zo gaat het leven aan je vor", 1),
        ("Completely Different Song", 4),
    ]:
        rows.extend([[f"{v}-{i}", v] for i in range(n)])
    session.put_rows("plays", ["play_id", "song"], rows)

    tool = ResolveEntitiesTool(session)
    out = await tool.execute(table="plays", column="song", output_table="song_map")
    assert "4 distinct values -> 2 entities" in out
    assert "1 clusters merged 2+ variants" in out
    joined = await dat.SqlTool(session).execute(
        "SELECT m.canonical, COUNT(*) c FROM plays p JOIN song_map m ON p.song = m.value "
        "GROUP BY 1 ORDER BY c DESC"
    )
    # The most frequent original spelling wins the canonical slot, and the
    # merged entity aggregates all 6 plays vs 4 for the unrelated song.
    assert "Zo gaat het leven aan je voor" in joined and "6" in joined


async def test_resolve_entities_threshold_and_rejection(session):
    from raven.plugin.data_agent.entity_resolution import ResolveEntitiesTool

    tool = ResolveEntitiesTool(session)
    out = await tool.execute(
        table="sales.orders",
        column="book_ref",
        output_table="x",
        where="1=1; DROP TABLE sales.orders",
    )
    assert out.startswith("[RESOLVE_ENTITIES REJECTED]")
    strict = await tool.execute(
        table="sales.orders", column="book_ref", output_table="refs_strict", threshold=1.0
    )
    # bref_1 / bref_2 / bref_9 stay distinct at threshold 1.0.
    assert "3 distinct values -> 3 entities" in strict


async def test_resolve_entities_keeps_unnormalizable_values_distinct(session):
    from raven.plugin.data_agent.entity_resolution import ResolveEntitiesTool

    rows = []
    for i, v in enumerate(["***", "...", "- . -", "#9", "9"]):
        rows.extend([[f"{v}-{j}-{i}", v] for j in range(2)])
    session.put_rows("titles", ["row_id", "title"], rows)

    tool = ResolveEntitiesTool(session)
    out = await tool.execute(table="titles", column="title", output_table="title_map")
    # Punctuation-only titles normalize to nothing and digits-only labels are
    # placeholders: nothing here may merge, five spellings stay five entities.
    assert "5 distinct values -> 5 entities" in out


async def test_resolve_entities_merges_decorated_variants(session):
    from raven.plugin.data_agent.entity_resolution import ResolveEntitiesTool

    rows = []
    for i, v in enumerate(
        [
            "Zo gaat het leven aan je voor",
            "Zo gaat het leven aan je voor (Hillich fjoer | Heilig vuur)",
            "006-Zo gaat het leven aan je voor",
            "Syb van der Ploeg - Zo gaat het leven aan je voor",
            "Completely Different Song",
            "Love",
            "Love Story",
        ]
    ):
        rows.append([f"r{i}", v])
    session.put_rows("catalog", ["row_id", "title"], rows)

    tool = ResolveEntitiesTool(session)
    out = await tool.execute(table="catalog", column="title", output_table="catalog_map")
    # The four decorated spellings collapse into one entity; short names must not
    # merge by containment ('Love' stays apart from 'Love Story').
    assert "7 distinct values -> 4 entities" in out


async def test_resolve_entities_placeholders_stay_apart(session):
    from raven.plugin.data_agent.entity_resolution import ResolveEntitiesTool

    rows = []
    for i, v in enumerate(["unknown", "Unknown", "n/a", "[untitled]", "007", "Real Song Name Here"]):
        rows.extend([[f"p{i}-{j}", v] for j in range(2)])
    session.put_rows("labels", ["row_id", "name"], rows)

    tool = ResolveEntitiesTool(session)
    out = await tool.execute(table="labels", column="name", output_table="label_map")
    # 'unknown' and 'Unknown' share a normalized key but are placeholders, so no
    # merging happens anywhere: six distinct spellings stay six entities.
    assert "6 distinct values -> 6 entities" in out
    assert "look like null placeholders" in out


async def test_bootstrap_classifier_scales_sample_to_corpus(session):
    from raven.plugin.data_agent.bootstrap_classifier import BootstrapClassifierTool

    sports = ["match win goal team score league cup final", "player coach season tournament victory"]
    finance = ["stock market shares profit revenue earnings", "bank interest rate bond investor fund"]
    corpus, labeled = [], []
    for i in range(40):
        # First 20 rows sports-flavored, last 20 finance-flavored.
        text = f"{sports[i % 2]} filler{i}" if i < 20 else f"{finance[i % 2]} filler{i}"
        corpus.append([f"r{i}", text])
        if i % 2 == 0:
            labeled.append([f"r{i}", "Sports" if i < 20 else "Finance"])
    # Pad the labeled sample past the minimum by repeating with distinct ids.
    for j in range(20):
        base = corpus[j if j < 10 else 20 + j - 10]
        corpus.append([f"x{j}", base[1]])
        labeled.append([f"x{j}", "Sports" if j < 10 else "Finance"])
    session.put_rows("articles", ["article_id", "body"], corpus)
    session.put_rows("sample_labels", ["id", "value"], labeled)

    tool = BootstrapClassifierTool(session)
    out = await tool.execute(
        table="articles",
        id_column="article_id",
        text_column="body",
        labeled_table="sample_labels",
        output_table="body_labels",
    )
    assert "rows classified" in out
    joined = await dat.SqlTool(session).execute(
        "SELECT label, COUNT(*) FROM body_labels GROUP BY 1 ORDER BY 1"
    )
    assert "Finance" in joined and "Sports" in joined


async def test_bootstrap_classifier_refuses_thin_or_single_label(session):
    from raven.plugin.data_agent.bootstrap_classifier import BootstrapClassifierTool

    session.put_rows("tiny", ["rid", "txt"], [[f"t{i}", f"word{i}"] for i in range(40)])
    session.put_rows("tiny_labels", ["id", "value"], [["t0", "A"], ["t1", "B"]])
    tool = BootstrapClassifierTool(session)
    out = await tool.execute(
        table="tiny",
        id_column="rid",
        text_column="txt",
        labeled_table="tiny_labels",
        output_table="tiny_out",
    )
    assert out.startswith("[BOOTSTRAP_CLASSIFIER REFUSED]")
    session.put_rows(
        "tiny_labels2", ["id", "value"], [[f"t{i}", "OnlyLabel"] for i in range(35)]
    )
    out = await tool.execute(
        table="tiny",
        id_column="rid",
        text_column="txt",
        labeled_table="tiny_labels2",
        output_table="tiny_out2",
    )
    assert "single label" in out


async def test_semantic_map_rejects_write_predicate(session):
    from raven.plugin.data_agent.semantic_map import SemanticMapTool

    tool = SemanticMapTool(session, provider=object())
    out = await tool.execute(
        table="sales.orders",
        id_column="order_id",
        text_column="book_ref",
        task="x",
        output_table="t",
        where="1=1; DROP TABLE sales.orders",
    )
    assert out.startswith("[SEMANTIC_MAP REJECTED]")


def test_semantic_map_parse_tolerates_fences_and_garbage():
    from raven.plugin.data_agent.semantic_map import _parse_batch_response

    assert _parse_batch_response('```json\n[{"id": "1", "value": "a"}]\n```') == {"1": "a"}
    assert _parse_batch_response('noise [{"id": "1", "value": null}] noise') == {"1": None}
    assert _parse_batch_response("no json here") is None
    assert _parse_batch_response("[]") is None


def test_semantic_map_factory_declines_without_provider(tmp_path):
    from raven.plugin.data_agent.semantic_map import make_semantic_map_tool

    ctx = PluginContext(
        config={},
        services=ServiceLocator(workspace=tmp_path),
    )
    assert make_semantic_map_tool(ctx) is None


async def test_verify_join_detects_prefix_mismatch(session):
    tool = dat.VerifyJoinTool(session)
    # raw keys never match: bref_* vs bid_*
    raw = await tool.execute("sales.orders", "book_ref", "meta.books", "book_id")
    assert "NO KEYS MATCH" in raw
    # transform both to the bare id -> they match, and fan-out is reported
    fixed = await tool.execute(
        "sales.orders",
        "book_ref",
        "meta.books",
        "book_id",
        left_transform="replace(book_ref,'bref_','')",
        right_transform="replace(book_id,'bid_','')",
    )
    assert "match_rate=0" not in fixed.split("VERDICT")[0].split("match_rate=")[1][:5]


async def test_grain_check_detects_hierarchy(session):
    from raven.plugin.data_agent.grain_check import GrainCheckTool

    rows = [
        ["BRCA", "BRCA-LumA", "s1"],
        ["BRCA", "BRCA-LumB", "s2"],
        ["BRCA", "BRCA-LumA", "s3"],
        ["LUAD", "LUAD-prox", "s4"],
        ["LUAD", "LUAD-prox", "s5"],
        ["GBM", "GBM-mes", "s6"],
    ]
    session.put_rows("tumors", ["cancer_type", "cancer_subtype", "sample_id"], rows)
    tool = GrainCheckTool(session)
    out = await tool.execute(table="tumors", columns=["cancer_type", "cancer_subtype"])
    assert '"cancer_type" is a PARENT level of "cancer_subtype"' in out
    assert "(3 -> 4)" in out
    assert "DIFFERENT grains" in out


async def test_grain_check_aliases_and_independent(session):
    from raven.plugin.data_agent.grain_check import GrainCheckTool

    session.put_rows(
        "shops",
        ["shop_code", "shop_name", "city"],
        [["s1", "Alpha", "NY"], ["s2", "Beta", "NY"], ["s3", "Gamma", "LA"], ["s4", "Delta", "SF"]],
    )
    tool = GrainCheckTool(session)
    aliased = await tool.execute(table="shops", columns=["shop_code", "shop_name"])
    assert "1:1 (aliases of the same grain)" in aliased
    # city does not functionally determine shop_code and vice versa is a parent:
    # every shop maps to one city -> city is the coarser level.
    nested = await tool.execute(table="shops", columns=["shop_code", "city"])
    assert '"city" is a PARENT level of "shop_code"' in nested


async def test_grain_check_key_uniqueness(session):
    from raven.plugin.data_agent.grain_check import GrainCheckTool

    session.put_rows(
        "export_dup", ["grp", "val"], [["a", "1"], ["a", "2"], ["b", "3"]]
    )
    tool = GrainCheckTool(session)
    dup = await tool.execute(table="export_dup", columns=["grp"], key_column="grp")
    assert "NOT unique" in dup and "1 surplus" in dup
    session.put_rows("export_ok", ["grp", "val"], [["a", "1"], ["b", "3"]])
    ok = await tool.execute(table="export_ok", columns=["grp"], key_column="grp")
    assert "unique across the export" in ok


async def test_grain_check_error_paths(session):
    from raven.plugin.data_agent.grain_check import GrainCheckTool

    tool = GrainCheckTool(session)
    assert "REJECTED" in await tool.execute(table="sales.orders", columns=[])
    assert "REJECTED" in await tool.execute(
        table="sales.orders", columns=["a", "b", "c", "d", "e"]
    )
    missing = await tool.execute(table="sales.orders", columns=["no_such_column"])
    assert "GRAIN_CHECK ERROR" in missing and "profile the table" in missing


async def test_submit_answer_writes_rows_and_adjudication(tmp_path):
    import json as _json

    from raven.plugin.data_agent.deliver import SubmitAnswerTool

    tool = SubmitAnswerTool(answer_path=str(tmp_path / "answer.txt"))
    out = await tool.execute(
        rows=["Alpha 10", "Beta 8", "Gamma 5"],
        selected_reading="rank stores by units sold, absolute count",
        rejected_readings=["rank by revenue"],
        reason="the question says 'most units'; units_sold has 812 rows vs 0 rows for any revenue column",
    )
    assert "wrote 3 rows" in out
    assert (tmp_path / "answer.txt").read_text() == "Alpha 10\nBeta 8\nGamma 5\n"
    record = _json.loads((tmp_path / "adjudication.json").read_text())
    assert record["rows_delivered"] == 3 and record["rejected_readings"] == ["rank by revenue"]


async def test_submit_answer_refuses_hollow_adjudication(tmp_path):
    from raven.plugin.data_agent.deliver import SubmitAnswerTool

    tool = SubmitAnswerTool(answer_path=str(tmp_path / "answer.txt"))
    assert "REFUSED" in await tool.execute(rows=[], selected_reading="x", reason="y" * 50)
    assert "REFUSED" in await tool.execute(rows=["a"], selected_reading="x", reason="thin")
    assert not (tmp_path / "answer.txt").exists()


async def test_submit_answer_requires_numbers_when_adjudicating(tmp_path):
    from raven.plugin.data_agent.deliver import SubmitAnswerTool

    tool = SubmitAnswerTool(answer_path=str(tmp_path / "answer.txt"))
    out = await tool.execute(
        rows=["ka0X"],
        selected_reading="quantity-tier reading of the discount policy",
        rejected_readings=["mandatory bundle reading"],
        reason="the bundle reading seemed less plausible given the quote contents overall here",
    )
    assert "REFUSED" in out and "measured quantities" in out
    out = await tool.execute(
        rows=["ka0X"],
        selected_reading="quantity-tier reading of the discount policy",
        rejected_readings=["mandatory bundle reading"],
        reason="tier reading: 2659/2966 rows exactly on tier, 0 below; bundle reading fires on 209/209 quotes (background)",
    )
    assert "wrote 1 rows" in out
    out = await tool.execute(
        rows=["42"],
        selected_reading="single literal reading",
        reason="the question admits one reading; direct aggregate over the full table computed",
    )
    assert "wrote 1 rows" in out


async def test_submit_answer_refuses_for_each_shortfall_once(tmp_path):
    from raven.plugin.data_agent.deliver import SubmitAnswerTool

    q = tmp_path / "dab_question.txt"
    q.write_text("What is the best year for each CPC group at level 4?")
    tool = SubmitAnswerTool(
        answer_path=str(tmp_path / "answer.txt"), question_path=str(q)
    )
    kwargs = dict(
        rows=["A61 2016", "H04 2015"],
        selected_reading="best filing year per level-4 CPC class",
        reason="EMA per class over 23 level-4 classes computed from 46 primary code pairs",
    )
    out = await tool.execute(**kwargs)
    assert "REFUSED" in out and "for each" in out
    assert not (tmp_path / "answer.txt").exists()
    out = await tool.execute(**kwargs)
    assert "wrote 2 rows" in out

    scalar = SubmitAnswerTool(
        answer_path=str(tmp_path / "a2.txt"), question_path=str(q)
    )
    out = await scalar.execute(
        rows=["r1", "r2", "r3"],
        selected_reading="per-group best year, all groups",
        reason="three groups exist in the filtered data; one row each delivered",
    )
    assert "wrote 3 rows" in out

    noq = SubmitAnswerTool(
        answer_path=str(tmp_path / "a3.txt"),
        question_path=str(tmp_path / "missing.txt"),
    )
    out = await noq.execute(
        rows=["42"],
        selected_reading="single literal reading",
        reason="direct aggregate over the full table computed, single reading",
    )
    assert "wrote 1 rows" in out


async def test_survey_sources_surfaces_synonym_columns_and_shapes(session, tmp_path, monkeypatch):
    from raven.plugin.data_agent import survey as survey_mod
    from raven.plugin.data_agent.survey import SurveySourcesTool

    monkeypatch.setattr(survey_mod, "_NOTES_PATH", str(tmp_path / "survey.md"))
    session.put_rows(
        "clinical",
        ["histological_type", "icd_histology_code", "sample_id", "purity"],
        [
            ["Astrocytoma", "9382/3", "s1", "0.71"],
            ["Astrocytoma", "9400/3", "s2", "0.55"],
            ["Oligodendroglioma", "9450/3", "s3", "0.62"],
            ["Oligodendroglioma", "[Not Available]", "s4", "0.44"],
        ],
    )
    session.put_rows(
        "quotes",
        ["quantity", "line_amount"],
        [["5", "2279.95"], ["8", "2379.93"], ["10", "4499.91"], ["7", "2260.93"]],
    )
    out = await SurveySourcesTool(session).execute()
    notes = (tmp_path / "survey.md").read_text()
    assert "SYNONYM GROUP" in out and "histolog" in out.lower()
    assert "1 bracketed" in notes
    assert "count-like" in notes and "money/measure-like" in notes
    assert "ratio-like" in notes
    assert "survey.md" in out


async def test_survey_sources_flags_list_valued_columns(session, tmp_path, monkeypatch):
    from raven.plugin.data_agent import survey as survey_mod
    from raven.plugin.data_agent.survey import SurveySourcesTool

    monkeypatch.setattr(survey_mod, "_NOTES_PATH", str(tmp_path / "survey.md"))
    session.put_rows(
        "business",
        ["name", "categories"],
        [
            ["Cafe One", "Restaurants, Breakfast & Brunch, Cafes"],
            ["Cafe Two", "Restaurants, Coffee & Tea"],
            ["Bar Three", "Nightlife, Bars"],
            ["Solo Deli", "Delis"],
        ],
    )
    out = await SurveySourcesTool(session).execute()
    notes = (tmp_path / "survey.md").read_text()
    assert "LIST-VALUED" in out and "categories" in out
    assert "deliver the stored string whole" in notes
    flagged = out.split("LIST-VALUED columns in ")[1].split("--")[0].split(":")[1]
    assert "categories" in flagged and "name" not in flagged


async def test_survey_sources_detects_hierarchy_and_join_candidates(session, tmp_path, monkeypatch):
    from raven.plugin.data_agent import survey as survey_mod
    from raven.plugin.data_agent.survey import SurveySourcesTool

    monkeypatch.setattr(survey_mod, "_NOTES_PATH", str(tmp_path / "survey.md"))
    session.put_rows(
        "repos_catalog",
        ["org_name", "repo_name"],
        [["acme", "acme/a"], ["acme", "acme/b"], ["zeta", "zeta/x"], ["zeta", "zeta/y"], ["zeta", "zeta/z"]],
    )
    session.put_rows("stars", ["repo_name", "star_total"], [["acme/a", "10"], ["zeta/x", "7"]])
    out = await SurveySourcesTool(session).execute()
    notes = (tmp_path / "survey.md").read_text()
    assert "HIERARCHY" in notes and "org_name" in notes
    assert "JOIN CANDIDATE" in notes and "repo_name" in notes
    assert "surveyed" in out
