"""Knowledge graph derived from PostgreSQL foreign keys.

No graph database. Nodes are table rows identified as `kind:pk`, edges are the
foreign keys that already exist, and traversal is a breadth-first expansion.
PostgreSQL stays the only source of truth, so there is no projection job and no
dual-write consistency problem.

Everything here is a pure function over row-like objects, so it runs against
ORM rows or plain test objects identically.
"""

from collections import defaultdict, deque
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any, NamedTuple

NODE_KINDS = (
    "customer",
    "commodity",
    "facility",
    "bin",
    "contract",
    "delivery",
    "invoice",
)
MAX_DEPTH = 3


class Edge(NamedTuple):
    source: str
    target: str
    label: str


@dataclass(frozen=True)
class Graph:
    nodes: dict[str, dict[str, Any]]
    edges: list[Edge]


def node_id(kind: str, pk: int) -> str:
    return f"{kind}:{pk}"


def parse_node_id(raw: str) -> tuple[str, int]:
    """Split `customer:3` into ("customer", 3).

    Raises ValueError on anything malformed — this parses a URL path segment,
    so it must reject junk rather than trust it.
    """
    kind, _, pk = raw.partition(":")
    if kind not in NODE_KINDS or not pk.isdigit():
        raise ValueError(f"bad node id {raw!r}; expected one of {NODE_KINDS} as kind:pk")
    return kind, int(pk)


def build(
    customers: Sequence[Any],
    commodities: Sequence[Any],
    facilities: Sequence[Any],
    bins: Sequence[Any],
    contracts: Sequence[Any],
    deliveries: Sequence[Any],
    invoices: Sequence[Any],
) -> Graph:
    """Assemble the whole graph from table rows.

    ponytail: builds the full node and edge set per request. Fine at demo scale
    (hundreds of rows); swap the expansion for a recursive CTE if the tables
    outgrow one fetch.
    """
    nodes: dict[str, dict[str, Any]] = {}

    def add(kind: str, pk: int, label: str, **extra: Any) -> str:
        nid = node_id(kind, pk)
        nodes[nid] = {"id": nid, "kind": kind, "label": label, **extra}
        return nid

    for c in customers:
        add("customer", c.id, c.name, subtype=c.kind)
    for c in commodities:
        add("commodity", c.id, c.name)
    for f in facilities:
        add("facility", f.id, f.name, location=f.location)
    for b in bins:
        add("bin", b.id, b.name)
    for c in contracts:
        add("contract", c.id, c.number, status=c.status)
    for d in deliveries:
        add("delivery", d.id, d.ticket_number, truck_id=d.truck_id)
    for i in invoices:
        add("invoice", i.id, i.number, status=i.status)

    edges: list[Edge] = []

    def link(src: str, src_pk: int, label: str, dst: str, dst_pk: int | None) -> None:
        # Skip dangling or nullable FKs rather than emitting edges to nowhere.
        if dst_pk is None:
            return
        a, b = node_id(src, src_pk), node_id(dst, dst_pk)
        if a in nodes and b in nodes:
            edges.append(Edge(a, b, label))

    for b in bins:
        link("facility", b.facility_id, "HAS_BIN", "bin", b.id)
        link("bin", b.id, "STORES", "commodity", b.commodity_id)
    for c in contracts:
        link("customer", c.customer_id, "SIGNED", "contract", c.id)
        link("contract", c.id, "FOR_COMMODITY", "commodity", c.commodity_id)
    for d in deliveries:
        link("delivery", d.id, "FULFILLS", "contract", d.contract_id)
        link("delivery", d.id, "DELIVERED_BY", "customer", d.customer_id)
        link("delivery", d.id, "RECEIVED_AT", "facility", d.facility_id)
    for i in invoices:
        link("invoice", i.id, "BILLS", "contract", i.contract_id)
        link("invoice", i.id, "BILLED_TO", "customer", i.customer_id)

    return Graph(nodes=nodes, edges=edges)


def summary(graph: Graph) -> dict[str, Any]:
    """Node counts per kind and edge counts per label, for the graph overview."""
    by_kind: dict[str, int] = defaultdict(int)
    for node in graph.nodes.values():
        by_kind[node["kind"]] += 1
    by_label: dict[str, int] = defaultdict(int)
    for edge in graph.edges:
        by_label[edge.label] += 1
    return {
        "node_count": len(graph.nodes),
        "edge_count": len(graph.edges),
        "nodes_by_kind": dict(sorted(by_kind.items())),
        "edges_by_label": dict(sorted(by_label.items())),
    }


def expand(graph: Graph, start: str, depth: int = 1) -> dict[str, Any]:
    """Breadth-first expansion `depth` hops out from `start`.

    Edges are followed in both directions — an invoice should reach its customer
    whether the FK points that way or not. Raises KeyError if `start` is absent.
    """
    if start not in graph.nodes:
        raise KeyError(start)
    depth = max(0, min(depth, MAX_DEPTH))

    adjacency: dict[str, list[Edge]] = defaultdict(list)
    for edge in graph.edges:
        adjacency[edge.source].append(edge)
        adjacency[edge.target].append(edge)

    hops = {start: 0}
    queue = deque([start])
    while queue:
        current = queue.popleft()
        if hops[current] >= depth:
            continue
        for edge in adjacency[current]:
            neighbour = edge.target if edge.source == current else edge.source
            if neighbour not in hops:
                hops[neighbour] = hops[current] + 1
                queue.append(neighbour)

    # Induced subgraph: keep every edge whose endpoints both survived.
    kept = [e for e in graph.edges if e.source in hops and e.target in hops]
    return {
        "root": start,
        "depth": depth,
        "nodes": [
            {**graph.nodes[nid], "hops": hop}
            for nid, hop in sorted(hops.items(), key=lambda kv: (kv[1], kv[0]))
        ],
        "edges": [e._asdict() for e in kept],
    }
