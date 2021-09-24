from __future__ import annotations

import hashlib
import json
from functools import reduce
from typing import Optional, Generator, Any

from networkx import DiGraph, MultiDiGraph, all_shortest_paths

from core import feature
from core.model.resolve_in_graph import GraphResolver, NodePath, ResolveProp
from core.model.model import Model
from core.model.typed_model import to_js
from core.types import Json
from core.util import utc, utc_str, value_in_path


class Section:

    # The reported section contains the data gathered by the collector.
    # This data is usually not changed by the user directly, but implicitly via changes on the
    # infrastructure, so the next collect run will change this state.
    reported = "reported"

    # This section holds changes that should be reflected by the given node.
    # The desired section can be queried the same way as the reported section
    # and allows to query parts of the graph with a common desired state.
    # For example the clean flag is manifested in the desired section.
    # The separate clean step would query all nodes that should be cleaned
    # and can compute the correct order of action by walking the graph structure.
    desired = "desired"

    # This section holds information about this node that are gathered during the import process.
    # Example: This section resolves common graph attributes like cloud, account, region, zone to make
    # querying the graph easy.
    metadata = "metadata"

    # The set of all allowed sections
    all_ordered = [reported, desired, metadata]
    all = set(all_ordered)


class EdgeType:
    # This edge type defines logical dependencies between resources.
    # It is the main edge type and is assumed, if no edge type is given.
    dependency = "dependency"

    # This edge type defines the order of delete operations.
    # A resource can be deleted, if all outgoing resources are deleted.
    delete = "delete"

    # The default edge type, that is used as fallback if no edge type is given.
    # The related graph is also used as source of truth for graph updates.
    default = dependency

    # The list of all allowed edge types.
    # Note: the database schema has to be adapted to support additional edge types.
    all = {dependency, delete}


class GraphBuilder:
    def __init__(self, model: Model, with_flatten: bool = feature.DB_SEARCH):
        self.model = model
        self.graph = MultiDiGraph()
        self.with_flatten = with_flatten
        self.nodes = 0
        self.edges = 0

    def add_from_json(self, js: Json) -> None:
        if "id" in js and Section.reported in js:
            self.add_node(
                js["id"],
                js[Section.reported],
                js.get(Section.desired, None),
                js.get(Section.metadata, None),
                js.get("merge", None) is True,
            )
        elif "from" in js and "to" in js:
            self.add_edge(js["from"], js["to"], js.get("edge_type", EdgeType.default))
        else:
            raise AttributeError(f"Format not understood! Got {json.dumps(js)} which is neither vertex nor edge.")

    def add_node(
        self,
        node_id: str,
        reported: Json,
        desired: Optional[Json] = None,
        metadata: Optional[Json] = None,
        merge: bool = False,
    ) -> None:
        self.nodes += 1
        # validate kind of this reported json
        coerced = self.model.check_valid(reported)
        item = reported if coerced is None else coerced
        kind = self.model[item]
        # create content hash
        sha = GraphBuilder.content_hash(item, desired, metadata)
        # flat all properties into a single string for search
        flat = GraphBuilder.flatten(item) if self.with_flatten else None
        self.graph.add_node(
            node_id,
            id=node_id,
            reported=item,
            desired=desired,
            metadata=metadata,
            hash=sha,
            kind=kind,
            kinds=list(kind.kind_hierarchy()),
            flat=flat,
            merge=merge,
        )

    def add_edge(self, from_node: str, to_node: str, edge_type: str) -> None:
        self.edges += 1
        key = GraphAccess.edge_key(from_node, to_node, edge_type)
        self.graph.add_edge(from_node, to_node, key, edge_type=edge_type)

    @staticmethod
    def content_hash(js: Json, desired: Optional[Json] = None, metadata: Optional[Json] = None) -> str:
        sha256 = hashlib.sha256()
        sha256.update(json.dumps(js, sort_keys=True).encode("utf-8"))
        if desired:
            sha256.update(json.dumps(desired, sort_keys=True).encode("utf-8"))
        if metadata:
            sha256.update(json.dumps(metadata, sort_keys=True).encode("utf-8"))
        return sha256.hexdigest()

    @staticmethod
    def flatten(js: Json) -> str:
        result = ""

        def dispatch(value: object) -> None:
            nonlocal result
            if isinstance(value, dict):
                flatten_object(value)
            elif isinstance(value, list):
                flatten_array(value)
            elif isinstance(value, bool):
                pass
            else:
                result += f" {value}"

        def flatten_object(js_doc: Json) -> None:
            for value in js_doc.values():
                dispatch(value)

        def flatten_array(arr: list[Any]) -> None:
            for value in arr:
                dispatch(value)

        dispatch(js)
        return result[1::]

    def check_complete(self) -> None:
        # check that all vertices are given, that were defined in any edge definition
        # note: DiGraph will create an empty vertex node automatically
        for node_id, node in self.graph.nodes(data=True):
            assert node.get(Section.reported), f"{node_id} was used in an edge definition but not provided as vertex!"

        edge_types = {edge[2] for edge in self.graph.edges(data="edge_type")}
        al = EdgeType.all
        assert not edge_types.difference(al), f"Graph contains unknown edge types! Given: {edge_types}. Known: {al}"
        # make sure there is only one root node
        rid = GraphAccess.root_id(self.graph)
        root_node = self.graph.nodes[rid]

        # make sure the root
        if value_in_path(root_node, NodePath.reported_kind) == "graph_root" and rid != "root":
            # remove node with wrong id +
            root_node = self.graph.nodes[rid]
            root_node["id"] = "root"
            self.graph.add_node("root", **root_node)

            for succ in list(self.graph.successors(rid)):
                for edge_type in EdgeType.all:
                    key = GraphAccess.edge_key(rid, succ, edge_type)
                    if self.graph.has_edge(rid, succ, key):
                        self.graph.remove_edge(rid, succ, key)
                        self.graph.add_edge("root", succ, GraphAccess.edge_key("root", succ, edge_type))
            self.graph.remove_node(rid)


NodeData = tuple[str, Json, Optional[Json], Optional[Json], Optional[Json], str, list[str], str]


class GraphAccess:
    def __init__(
        self,
        sub: MultiDiGraph,
        maybe_root_id: Optional[str] = None,
        visited_nodes: Optional[set[Any]] = None,
        visited_edges: Optional[set[tuple[Any, Any, str]]] = None,
    ):
        super().__init__()
        self.g = sub
        self.nodes = sub.nodes()
        self.visited_nodes: set[object] = visited_nodes if visited_nodes else set()
        self.visited_edges: set[tuple[object, object, str]] = visited_edges if visited_edges else set()
        self.at = utc()
        self.at_json = utc_str(self.at)
        self.maybe_root_id = maybe_root_id

    def root(self) -> str:
        return self.maybe_root_id if self.maybe_root_id else GraphAccess.root_id(self.g)

    def node(self, node_id: str) -> Optional[Json]:
        self.visited_nodes.add(node_id)
        if self.g.has_node(node_id):
            n = self.nodes[node_id]
            return self.dump(node_id, n)
        else:
            return None

    def has_edge(self, from_id: object, to_id: object, edge_type: str) -> bool:
        result: bool = self.g.has_edge(from_id, to_id, self.edge_key(from_id, to_id, edge_type))
        if result:
            self.visited_edges.add((from_id, to_id, edge_type))
        return result

    def resolve(self, node_id: str, node: Json) -> Json:
        def with_ancestor(ancestor: Json, prop: ResolveProp) -> None:
            extracted = value_in_path(ancestor, prop.extract_path)
            if extracted:
                if prop.section not in node:
                    node[prop.section] = {}
                node[prop.section][prop.name] = extracted

        for resolver in GraphResolver.to_resolve:
            # search for ancestor that matches filter criteria
            anc = self.ancestor_of(node_id, EdgeType.dependency, resolver.kind)
            if anc:
                for res in resolver.resolve:
                    with_ancestor(anc, res)
        return node

    def dump(self, node_id: str, node: Json) -> Json:
        return self.dump_direct(node_id, self.resolve(node_id, node))

    def predecessors(self, node_id: str, edge_type: str) -> Generator[str, Any, None]:
        for pred_id in self.g.predecessors(node_id):
            # direction from parent node to provided node
            if self.g.has_edge(pred_id, node_id, self.edge_key(pred_id, node_id, edge_type)):
                yield pred_id

    def successors(self, node_id: str, edge_type: str) -> Generator[str, Any, None]:
        for succ_id in self.g.successors(node_id):
            # direction from provided node to successor node
            if self.g.has_edge(node_id, succ_id, self.edge_key(node_id, succ_id, edge_type)):
                yield succ_id

    def ancestor_of(self, node_id: str, edge_type: str, kind: str) -> Optional[Json]:
        for p_id in self.predecessors(node_id, edge_type):
            p: Json = self.nodes[p_id]
            kinds: Optional[list[str]] = value_in_path(p, NodePath.kinds)
            if kinds and kind in kinds:
                return p
            else:
                # return if the parent has this value, otherwise walk all branches
                parent = self.ancestor_of(p_id, edge_type, kind)
                if parent:
                    return parent
        return None

    @staticmethod
    def dump_direct(node_id: str, node: Json) -> Json:
        reported: Json = to_js(node[Section.reported])
        desired: Optional[Json] = node.get(Section.desired, None)
        metadata: Optional[Json] = node.get(Section.metadata, None)
        if "id" not in node:
            node["id"] = node_id
        if "hash" not in node:
            node["hash"] = GraphBuilder.content_hash(reported, desired, metadata)
        if "flat" not in node:
            node["flat"] = GraphBuilder.flatten(reported)
        if "kinds" not in node:
            node["kinds"] = [reported["kind"]]
        return node

    def not_visited_nodes(self) -> Generator[Json, None, None]:
        return (self.dump(nid, self.nodes[nid]) for nid in self.g.nodes if nid not in self.visited_nodes)

    def not_visited_edges(self, edge_type: str) -> Generator[tuple[str, str], None, None]:
        # edge collection with (from, to, type): filter and drop type -> (from, to)
        edges = self.g.edges(data="edge_type")
        return (edge[:2] for edge in edges if edge[2] == edge_type and edge not in self.visited_edges)

    @staticmethod
    def edge_key(from_node: object, to_node: object, edge_type: str) -> str:
        return f"{from_node}_{to_node}_{edge_type}"

    @staticmethod
    def root_id(graph: DiGraph) -> str:
        # noinspection PyTypeChecker
        roots: list[str] = [n for n, d in graph.in_degree if d == 0]
        assert len(roots) == 1, f"Given subgraph has more than one root: {roots}"
        return roots[0]

    @staticmethod
    def merge_graphs(
        graph: DiGraph,
    ) -> tuple[list[str], GraphAccess, Generator[tuple[str, GraphAccess], None, None]]:
        """
        Find all merge graphs in the provided graph.
        A merge graph is a self contained graph under a node which is marked with merge=true.
        Such nodes are merged with the merge node in the database.
        Example:
        A -> B -> C(merge=true) -> E -> E1 -> E2
                                -> F -> F1
               -> D(merge=true) -> G -> G1 -> G2 -> G3 -> G4

        This will result in 3 merge roots:
            E: [A, B, C]
            F: [A, B, C]
            G: [A, B, D]

        Note that all successors of a merge node that is also a predecessors of the merge node is sorted out.
        Example: A -> B -> C(merge=true) -> A  ==> A is not considered merge root.

        :param graph: the incoming multi graph update.
        :return: the list of all merge roots, the expected parent graph and all merge root graphs.
        """

        # Find merge nodes: all nodes that are marked as merge node -> all children (merge roots) should be merged.
        # This method returns all merge roots as key, with the respective predecessors nodes as value.
        def merge_roots() -> dict[str, set[str]]:
            graph_root = GraphAccess.root_id(graph)
            merge_nodes = [node_id for node_id, data in graph.nodes(data=True) if data.get("merge", False)]
            assert len(merge_nodes) > 0, "No merge nodes provided in the graph. Mark at least one node with merge=true!"
            result: dict[str, set[str]] = {}
            for node in merge_nodes:
                # compute the shortest path from root to here and sort out all successors that are also predecessors
                pres: set[str] = reduce(lambda res, p: res | set(p), all_shortest_paths(graph, graph_root, node), set())
                for a in graph.successors(node):
                    if a not in pres:
                        result[a] = pres
            return result

        # Walk the graph from given starting node and return all successors.
        # A successor which is also a predecessors is not followed.
        def sub_graph_nodes(from_node: str, parent_ids: set[str]) -> set[str]:
            to_visit = [from_node]
            visited: set[str] = {from_node}

            def successors(node: str) -> list[str]:
                return [a for a in graph.successors(node) if a not in visited and a not in parent_ids]

            while to_visit:
                to_visit = reduce(lambda li, node: li + successors(node), to_visit, [])
                visited.update(to_visit)
            return visited

        # Create a generator for all given merge roots by:
        #   - creating the set of all successors
        #   - creating a subgraph which contains all predecessors and all succors
        #   - all predecessors are marked as visited
        #   - all predecessors edges are marked as visited
        # This way it is possible to have nodes in the graph that will not be touched by the update
        # while edges will be created from successors of the merge node to predecessors of the merge node.
        def merge_sub_graphs(
            root_nodes: dict[str, set[str]], parent_nodes: set[str], parent_edges: set[tuple[str, str, str]]
        ) -> Generator[tuple[str, GraphAccess], None, None]:
            all_successors: set[str] = set()
            for root, predecessors in root_nodes.items():
                successors: set[str] = sub_graph_nodes(root, predecessors)
                # make sure nodes are not "mixed" between different merge nodes
                overlap = successors & all_successors
                if overlap:
                    raise AttributeError(f"Nodes are referenced in more than one merge node: {overlap}")
                all_successors |= successors
                # create subgraph with all successors and all parents, where all parents are already marked as visited
                sub = GraphAccess(graph.subgraph(successors | parent_nodes), root, parent_nodes, parent_edges)
                yield root, sub

        roots = merge_roots()
        parents: set[str] = reduce(lambda res, ps: res | ps, roots.values(), set())
        parent_graph = graph.subgraph(parents)
        graphs = merge_sub_graphs(roots, parents, set(parent_graph.edges(data="edge_type")))
        return list(roots.keys()), GraphAccess(parent_graph, GraphAccess.root_id(graph)), graphs