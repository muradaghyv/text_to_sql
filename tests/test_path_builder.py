"""
Unit tests for path_builder.py

Tests build_adjacency() and find_two_hop_paths() using synthetic FK data —
no database connection required.
"""
import pytest
from schema_extractor.fk_extractor import FKRelationship
from schema_extractor.path_builder import build_adjacency, find_two_hop_paths, TwoHopPath


# ── Helpers ──────────────────────────────────────────────────────────────────

def make_fk(src_table: str, tgt_table: str) -> FKRelationship:
    return FKRelationship(
        source_schema="public",
        source_table=src_table,
        source_column="id",
        target_schema="public",
        target_table=tgt_table,
        target_column="id",
    )


# ── build_adjacency ───────────────────────────────────────────────────────────

class TestBuildAdjacency:
    def test_single_fk_creates_bidirectional_edge(self):
        fks = [make_fk("orders", "customers")]
        adj = build_adjacency(fks)
        assert "customers" in adj["orders"]
        assert "orders" in adj["customers"]

    def test_multiple_fks(self):
        fks = [
            make_fk("orders", "customers"),
            make_fk("orders", "products"),
        ]
        adj = build_adjacency(fks)
        assert adj["orders"] == {"customers", "products"}
        assert "orders" in adj["customers"]
        assert "orders" in adj["products"]

    def test_empty_fk_list_returns_empty_adjacency(self):
        assert build_adjacency([]) == {}

    def test_no_self_loops_introduced(self):
        fks = [make_fk("a", "b")]
        adj = build_adjacency(fks)
        assert "a" not in adj["a"]  # no self-loop on a
        assert "b" not in adj["b"]  # no self-loop on b


# ── find_two_hop_paths ────────────────────────────────────────────────────────

class TestFindTwoHopPaths:
    def test_classic_bridge_table(self):
        """
        customers -- orders -- products
        orders is the bridge; customers↔products have no direct FK.
        Expect one two-hop path: customers - orders - products.
        """
        fks = [
            make_fk("orders", "customers"),
            make_fk("orders", "products"),
        ]
        adj = build_adjacency(fks)
        paths = find_two_hop_paths(adj)

        assert len(paths) == 1
        p = paths[0]
        assert p.bridge_table == "orders"
        assert {p.table_a, p.table_b} == {"customers", "products"}

    def test_no_two_hop_when_direct_connection_exists(self):
        """
        If A and B are already directly connected, they should NOT appear
        as a two-hop path even if they share a bridge table.
        """
        fks = [
            make_fk("a", "b"),  # direct connection
            make_fk("a", "c"),
            make_fk("b", "c"),  # b and c also directly connected
        ]
        adj = build_adjacency(fks)
        paths = find_two_hop_paths(adj)

        # Every pair (a,b), (a,c), (b,c) is directly connected → no two-hop paths
        assert paths == []

    def test_no_duplicate_paths(self):
        """
        A-bridge-B and B-bridge-A must not both appear.
        """
        fks = [
            make_fk("orders", "customers"),
            make_fk("orders", "products"),
        ]
        adj = build_adjacency(fks)
        paths = find_two_hop_paths(adj)

        # Normalise each path as a frozenset of endpoints + bridge
        keys = [(p.bridge_table, frozenset([p.table_a, p.table_b])) for p in paths]
        assert len(keys) == len(set(keys)), "Duplicate two-hop path found"

    def test_empty_fks_return_no_paths(self):
        assert find_two_hop_paths({}) == []

    def test_single_fk_no_bridge(self):
        """Only one edge — not enough for a two-hop path."""
        fks = [make_fk("a", "b")]
        adj = build_adjacency(fks)
        assert find_two_hop_paths(adj) == []

    def test_multiple_bridges(self):
        """
        Schema: employees -- projects -- departments
                employees -- teams    -- departments
        departments and employees can be reached via either projects or teams.
        Both are valid two-hop paths and must both appear.
        """
        fks = [
            make_fk("projects", "employees"),
            make_fk("projects", "departments"),
            make_fk("teams", "employees"),
            make_fk("teams", "departments"),
        ]
        adj = build_adjacency(fks)
        paths = find_two_hop_paths(adj)

        endpoint_pairs = [frozenset([p.table_a, p.table_b]) for p in paths]
        target_pair = frozenset(["employees", "departments"])
        matching = [p for p in paths if frozenset([p.table_a, p.table_b]) == target_pair]

        assert len(matching) == 2
        bridges_used = {p.bridge_table for p in matching}
        assert bridges_used == {"projects", "teams"}
