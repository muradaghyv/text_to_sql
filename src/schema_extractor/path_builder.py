"""
What is path builder?

Building a graph of all FK relationships and finding two-hop paths.

Implementation
We are creating an undirected graph in which Nodes are table names whereas
edges are foreign key relationships.

For example: A - B, means A and B has a direct relationships. 
Example of finding a two-hop path: If there is no any direct FK relationship between 
A and B, we are checking if there is such a table C which has direct FK relationship like that:
B - C and A - C. So, we can create a two-hop path: A - C - B.
"""
from collections import defaultdict
from dataclasses import dataclass
from schema_extractor.fk_extractor import FKRelationship

@dataclass
class TwoHopPath:
    """
    Representing a two-hop connection between 2 tables via a bridge table. 

    Example: 
        table_a = "customers"
        bridge_table = "orders"
        table_b = "products"

        customers and products table can be connected via orders table.
    """
    table_a: str
    bridge_table: str
    table_b: str

def build_adjacency(fks: list[FKRelationship]) -> dict[str, set[str]]:
    """
    Converts a list of FK relationships into an undirected adjacency list.

    For each FK: source_table -> target_table
    We are adding both directions for making graph undirected:
        adjacency[source_table].add(target_table)
        adjacency[target_table].add(source_table)
    
    Example:
        {
            "orders": {"customers", "products", "employees"},
            "customers: {"orders"},
            "employees": {"orders"}
        }
    """
    adjacency: dict[str, set[str]] = defaultdict(set)

    for fk in fks:
        adjacency[fk.source_table].add(fk.target_table)
        adjacency[fk.target_table].add(fk.source_table)
    
    return adjacency

def find_two_hop_paths(adjacency: dict[str, set[str]]) -> list[TwoHopPath]:
    """
    Finds all pairs of tables (A, B) that share a common FK neighbor (bridge table)
    but are NOT directly connected to each other.

    Algorithm:
        For each bridge table C, look at every pair of C's neighbors (A, B).
        If A and B are not directly connected, record A - C - B.
        We sort (A, B) so that A < B alphabetically to avoid recording
        the same path twice (A-C-B and B-C-A are the same path).

    Example:
        customers -- orders -- products
        customers and products are not directly connected,
        but both connect to orders → two-hop path: customers - orders - products
    """
    paths = []
    seen: set[tuple[str, str, str]] = set()

    for bridge_table, neighbors in adjacency.items():
        neighbor_list = sorted(neighbors)
        for i in range(len(neighbor_list)):
            for j in range(i + 1, len(neighbor_list)):
                table_a = neighbor_list[i]
                table_b = neighbor_list[j]

                # Skip if they already have a direct FK relationship
                if table_b in adjacency.get(table_a, set()):
                    continue

                key = (table_a, bridge_table, table_b)
                if key not in seen:
                    seen.add(key)
                    paths.append(TwoHopPath(
                        table_a=table_a,
                        bridge_table=bridge_table,
                        table_b=table_b
                    ))

    return paths
