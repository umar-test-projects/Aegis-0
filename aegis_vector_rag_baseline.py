"""Local TF-IDF/cosine simulation of a text-only Vector RAG retriever.

The fixed corpus contains the raw decision summaries from the seeded Jira and
Slack records. No timestamps, active flags, or SUPERSEDES edges enter scoring.
This small lexical baseline demonstrates missing lineage, not all embedding RAG.
"""

from collections import Counter
import math
import re
from time import perf_counter


DEFAULT_QUERY = "EU traffic spikes failover us-east-1"
RAW_DECISION_CHUNKS = (
    {
        "decision_id": "DEC-2026-101", "source": "Jira RFC",
        "source_url": "https://jira.internal/browse/ARCH-101",
        "text": "Allow cross-Atlantic failover to us-east-1 during EU traffic spikes.",
    },
    {
        "decision_id": "DEC-2026-409", "source": "Slack thread",
        "source_url": "https://slack.internal/archives/C02compliance/p1760480000",
        "text": "STRICT FREEZE: EU payment streams must remain strictly within GDPR-compliant EU regions due to BaFin Audit #88.",
    },
)


def _tokens(text):
    return re.findall(r"\b[\w-]+\b", text.lower())


class VectorRAGBaseline:
    """Rank raw chunks by cosine similarity between TF-IDF vectors."""

    def __init__(self, chunks=RAW_DECISION_CHUNKS):
        self.chunks = [dict(chunk) for chunk in chunks]
        counts = [Counter(_tokens(chunk["text"])) for chunk in self.chunks]
        frequencies = Counter(token for count in counts for token in count)
        self.idf = {
            token: math.log((1 + len(counts)) / (1 + frequency)) + 1
            for token, frequency in frequencies.items()
        }
        self.vectors = [self._vector(count) for count in counts]

    def _vector(self, counts):
        weighted = {token: count * self.idf[token] for token, count in counts.items()
                    if token in self.idf}
        norm = math.sqrt(sum(value * value for value in weighted.values()))
        return {token: value / norm for token, value in weighted.items()} if norm else {}

    def retrieve(self, query, top_k=2):
        if top_k < 1:
            return []
        vector = self._vector(Counter(_tokens(query)))
        matches = [dict(chunk, cosine_similarity=sum(
            value * document.get(token, 0.0) for token, value in vector.items()
        )) for chunk, document in zip(self.chunks, self.vectors)]
        return sorted(matches, key=lambda row: (
            -row["cosine_similarity"], row["decision_id"]
        ))[:top_k]


def run_side_by_side_benchmark(engine, query=DEFAULT_QUERY):
    """Measure retrieval separately from the US counterfactual safety check.

    Index construction is excluded; the graph timing includes a database round
    trip. No production route mutation, handoff or learning update is performed.
    This illustration is not an incident observation. The engine deletes the
    ephemeral shadow graph after simulation as in normal triage.
    """
    baseline = VectorRAGBaseline()
    started = perf_counter()
    vector_results = baseline.retrieve(query)
    vector_ms = (perf_counter() - started) * 1000
    started = perf_counter()
    graph_results = engine.recall_playbooks_and_company_brain("pipeline_eu_payments")
    graph_ms = (perf_counter() - started) * 1000
    us_candidate = next((candidate for candidate in graph_results
                         if candidate["candidate_service_id"] == "svc_us_failover_fast"
                         and candidate.get("active_decision_id")), None)
    simulation = None
    simulation_ms = None
    if us_candidate:
        started = perf_counter()
        simulation = engine.run_shadow_graph_simulation(
            "pipeline_eu_payments", "svc_us_failover_fast", record_rejection=False,
        )
        simulation_ms = (perf_counter() - started) * 1000
    return {
        "query": query, "vector_results": vector_results, "vector_ms": vector_ms,
        "graph_results": graph_results, "graph_ms": graph_ms,
        "simulation": simulation, "simulation_ms": simulation_ms,
    }
