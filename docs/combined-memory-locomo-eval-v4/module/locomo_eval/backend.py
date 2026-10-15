"""Use our production MemoryService read path and waitable engine write paths.

Run in a separate process. No collection clearing, no QA ingestion, no auth bypass
in the server: this is an in-process algorithm benchmark, not an HTTP/auth test.
"""

from __future__ import annotations

import json
import sys
from datetime import timedelta
from pathlib import Path

from .runner import JUDGE_SYSTEM


class CombinedMemoryBackend:
    def __init__(
        self,
        *,
        run_id: str,
        ingestion: str,
        session_scope: str,
        last_n: int,
        answer_model: str | None,
        judge_model: str | None,
    ):
        self.project_id = f"locomo-{run_id}"
        self.ingestion = ingestion
        self.session_scope = session_scope
        self.last_n = last_n
        self.answer_model = answer_model
        self.judge_model = judge_model
        self.sessions = {}
        self.graph = self.vector = self.answer_client = self.judge_client = None
        self.profile = "current"

    async def initialize(self):
        # Offline inspect/score/tests never execute these application imports.
        root = Path(__file__).resolve().parents[2]
        sys.path.insert(0, str(root / "src"))
        from config.env_config import ENV
        from config.settings import get_settings

        settings = get_settings()
        if ENV != "dev":
            raise ValueError(
                "LoCoMo live runs require ENV=dev and dedicated test datastore endpoints"
            )
        self.ingest_assistant_episodes = settings.ingest_assistant_episodes
        from api.memory.service import MemoryService
        from clients.llm.bedrock_short_term import LLMClient
        from module.dev_mem.session_messages.store_singleton import initialize_session_message_store
        from module.dev_mem.sessions.store_singleton import initialize_session_store
        from services.long_term import get_memory_manager
        from services.short_term import get_simplemem_manager

        self.graph = await get_memory_manager()
        self.vector = await get_simplemem_manager()
        self.service = MemoryService()
        self.session_store = await initialize_session_store()
        self.message_store = await initialize_session_message_store()
        self.base_vector = self.vector._cfg.model_copy(deep=True)
        self.base_graph = self.graph.graphiti._config.model_copy(deep=True)
        cfg = settings.simplemem
        self.answer_model = self.answer_model or cfg.bedrock_retrieval_model
        creds = dict(
            region_name=cfg.bedrock_region,
            aws_access_key_id=cfg.aws_access_key_id,
            aws_secret_access_key=cfg.aws_secret_access_key,
            aws_session_token=cfg.aws_session_token,
        )
        self.answer_client = LLMClient(model=self.answer_model, **creds)
        if self.judge_model:
            self.judge_client = LLMClient(model=self.judge_model, **creds)

    def set_profile(self, profile):
        self.profile = profile
        self.vector._cfg = self.base_vector.model_copy(deep=True)
        graph_cfg = self.base_graph.model_copy(deep=True)
        if profile == "enhanced":
            # Experimental retrieval changes only; both profiles use identical stored data.
            self.vector._cfg.semantic_top_k = max(10, self.vector._cfg.semantic_top_k)
            self.vector._cfg.keyword_top_k = max(10, self.vector._cfg.keyword_top_k)
            self.vector._cfg.enable_planning = True
            self.vector._cfg.enable_reflection = True
            self.vector._cfg.max_reflection_rounds = 1
            graph_cfg.enable_node_search = True
            graph_cfg.edge_search_limit = max(16, graph_cfg.edge_search_limit)
            graph_cfg.episode_search_limit = max(16, graph_cfg.episode_search_limit)
        elif profile != "current":
            raise ValueError(f"Unknown profile: {profile}")
        self.graph.graphiti._config = graph_cfg

    def describe(self):
        # Explicit whitelist: never serialize Settings or connection credentials.
        return {
            "ingestion": self.ingestion,
            "session_scope": self.session_scope,
            "last_n": self.last_n,
            "ingest_assistant_episodes": self.ingest_assistant_episodes,
            "answer_model": self.answer_model,
            "judge_model": self.judge_model,
            "graph": {
                name: getattr(self.graph.graphiti._config, name)
                for name in (
                    "bedrock_model",
                    "summary_model",
                    "edge_search_limit",
                    "episode_search_limit",
                    "enable_node_search",
                    "edge_reranker",
                    "episode_reranker",
                    "max_search_depth",
                    "enable_community_detection",
                    "user_summary_min_episodes",
                    "session_summary_max_words",
                )
            },
            "vector": {
                name: getattr(self.vector._cfg, name)
                for name in (
                    "bedrock_storage_model",
                    "bedrock_retrieval_model",
                    "window_size",
                    "semantic_top_k",
                    "keyword_top_k",
                    "enable_planning",
                    "enable_reflection",
                    "max_reflection_rounds",
                    "enable_structured_search",
                    "min_similarity_score",
                    "min_keyword_score",
                )
            },
        }

    async def ingest(self, sample_id, turns):
        self.set_profile("current")
        user = f"sample-{sample_id}"
        sessions = sorted({turn.session for turn in turns})
        self.sessions[sample_id] = [f"{user}-session-{sid}" for sid in sessions]
        counts = {
            "turns": len(turns),
            "graph_episodes": 0,
            "vector_entries": 0,
            "summary_errors": [],
        }
        for sid in sessions:
            session = f"{user}-session-{sid}"
            await self.session_store.create(self.project_id, user, session_id=session)
            session_turns = [turn for turn in turns if turn.session == sid]
            # Await every graph write, preserving history, timestamps, and per-thread order.
            for order, turn in enumerate(session_turns):
                request = f"session-{sid}-exchange-{turn.exchange}"
                timestamp = turn.timestamp + timedelta(microseconds=order)
                content = f"{turn.speaker}: {turn.content}"
                message = await self.message_store.insert_one(
                    project_id=self.project_id,
                    user_id=user,
                    session_id=session,
                    request_id=request,
                    role=turn.role,
                    content=content,
                    created_at=timestamp,
                    message_id=turn.dia_id,
                )
                if (
                    self.ingestion == "corpus"
                    or turn.role == "user"
                    or self.ingest_assistant_episodes
                ):
                    # Corpus mode treats both human speakers as source facts (not AI answers).
                    result = await self.graph.store_message(
                        user_name=f"{self.project_id}_{user}",
                        session_id=f"{self.project_id}_{session}",
                        content=content,
                        role="user" if self.ingestion == "corpus" else turn.role,
                        raw_project_id=self.project_id,
                        raw_user_id=user,
                        raw_session_id=session,
                        request_id=request,
                        message_id=message.message_id,
                        reference_time=timestamp,
                        is_migration=True,
                        wait_timeout=300,
                        hard_timeout=600,
                    )
                    if not isinstance(result, dict) or not result.get("messages"):
                        raise RuntimeError("Graph ingestion returned no completed episode")
                    counts["graph_episodes"] += len(result["messages"])
                    counts["summary_errors"].extend(result.get("summary_errors", []))
            vector_turns = [
                turn
                for turn in session_turns
                if self.ingestion == "corpus" or turn.role == "assistant"
            ]
            # Match the live per-assistant-message builder lifetime in production policy mode.
            batches = (
                [vector_turns] if self.ingestion == "corpus" else [[turn] for turn in vector_turns]
            )
            for batch in batches:
                if not batch:
                    continue
                written = await self.vector.add_dialogues_batch(
                    session,
                    user,
                    [(turn.speaker, f"{turn.speaker}: {turn.content}") for turn in batch],
                    project_id=self.project_id,
                    request_id=f"session-{sid}-exchange-{batch[0].exchange}",
                    timestamp=batch[0].timestamp.isoformat().replace("+00:00", "Z"),
                    is_migration=True,
                )
                counts["vector_entries"] += written
        if counts["summary_errors"]:
            raise RuntimeError("Graph summaries failed during ingestion; see application logs")
        return counts

    async def retrieve(self, sample_id, question, memory_type):
        user = f"sample-{sample_id}"
        sessions = self.sessions[sample_id]
        if self.session_scope == "latest":
            sessions = sessions[-1:]
        # Production service reforms once and retrieves graph/vector concurrently per session.
        # Cross-session scope is an explicit benchmark extension; production accepts one session.
        blocks = {"long_term_context": [], "short_term_context": [], "last_n_messages_context": []}
        for session in sessions:
            result = await self.service.get_memory_context(
                project_id=self.project_id,
                user_id=user,
                session_id=session,
                user_query=question,
                memory_type=memory_type,
                last_n_messages=bool(self.last_n),
                n=self.last_n or 5,
            )
            for key in blocks:
                value = result.get(key, "").strip()
                if value and value not in blocks[key]:
                    blocks[key].append(value)
        return {key: "\n\n".join(values) for key, values in blocks.items()}

    async def answer(self, messages):
        if not any(json.loads(messages[-1]["content"])["memories"].values()):
            return "Not mentioned in the conversation"
        raw = await self.answer_client.chat_completion(messages, temperature=0)
        result = self.answer_client.extract_json(raw)
        if not isinstance(result.get("answer"), str) or not result["answer"].strip():
            raise ValueError("Answer model returned invalid answer JSON")
        return result["answer"].strip()

    async def judge(self, question, prediction, reference):
        raw = await self.judge_client.chat_completion(
            [
                {"role": "system", "content": JUDGE_SYSTEM},
                {
                    "role": "user",
                    "content": json.dumps(
                        dict(question=question, prediction=prediction, reference=reference)
                    ),
                },
            ],
            temperature=0,
        )
        result = self.judge_client.extract_json(raw)
        if type(result.get("score")) is not int or result["score"] not in (0, 1):
            raise ValueError("Judge must return integer 0 or 1")
        return {"judge_score": result["score"], "judge_reason": str(result.get("reason", ""))}

    async def close(self):
        # Handles partial initialization as well as a completed run.
        for resource in (self.judge_client, self.answer_client, self.graph, self.vector):
            if resource is not None:
                try:
                    await resource.close()
                except Exception:
                    pass
        if self.graph is not None or self.vector is not None:
            from module.dev_mem.weaviate_shared import close_weaviate_client

            close_weaviate_client()
