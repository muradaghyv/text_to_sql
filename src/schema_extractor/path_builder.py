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
from fk_extractor import FKRelationship

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
    