"""ADR-001 topology: 19 business nodes, 41 normal edges (excluding START)."""
from langgraph.graph import END, START, StateGraph

from agent.graph_nodes import GraphNodes
from agent.state import AgentState, RuntimeContext


ROUTES = {
    "understand": ("decide", "prepare_clarify", "halt"),
    "decide": ("gate", "halt"),
    "gate": ("dispatch", "prepare_ticket", "prepare_clarify", "answer", "halt", "verify"),
    "dispatch": ("rag", "sql", "read_tool", "write_ticket", "halt"),
    "rag": ("verify",), "sql": ("verify",), "read_tool": ("verify",), "write_ticket": ("verify",),
    "prepare_ticket": ("confirm",), "prepare_clarify": ("clarify",),
    "confirm": ("resume_gate",), "clarify": ("resume_gate",),
    "resume_gate": ("gate", "understand", "prepare_clarify", "route_goals", "halt"),
    "verify": ("route_goals", "prepare_clarify", "degrade", "seal_unknown", "halt"),
    "degrade": ("route_goals",), "seal_unknown": ("halt",),
    "route_goals": ("prepare_clarify", "decide", "answer"),
    "halt": ("answer",), "answer": (END,),
}
NORMAL_EDGES = tuple((source, target) for source, targets in ROUTES.items() for target in targets)


def build_graph(checkpointer):
    builder = StateGraph(AgentState, context_schema=RuntimeContext)
    nodes = GraphNodes()
    for name, targets in ROUTES.items():
        builder.add_node(name, nodes.bound(name))
        if len(targets) == 1:
            builder.add_edge(name, targets[0])
        else:
            builder.add_conditional_edges(name, lambda state: state["route"], {target: target for target in targets})
    builder.add_edge(START, "understand")
    return builder.compile(checkpointer=checkpointer)
