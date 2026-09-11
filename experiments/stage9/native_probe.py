"""启动隔离 dev Agent Server，验证真实 Store、HITL、state 与原生回收 API。"""

import argparse
import importlib.metadata
import json
import subprocess
import sys
import time
from pathlib import Path

import httpx
from langgraph_sdk import get_sync_client

from experiments.stage8_hotfix.environment import ROOT, NativeServer, isolated_environment
from financeclaw.agent_server.memory.service import owner_namespace
from financeclaw.kernel.context import ExecutionContext
from financeclaw.shared.conversation.repository import SqlAlchemyConversationRepository
from financeclaw.shared.infrastructure.database import ApplicationDatabase


class ProbeServer(NativeServer):
    """沿用已有的独占端口和退出清理，只更换本次探针图与隔离配置。"""

    def __enter__(self):
        """启用 Store 索引；配置不读取本机 .env 或任何模型凭证。"""
        self.directory.mkdir(parents=True, exist_ok=True)
        config = self.directory / "langgraph.json"
        config.write_text(
            json.dumps(
                {
                    "dependencies": [str(ROOT)],
                    "env": {},
                    "graphs": {"stage9": f"{ROOT}/experiments/stage9/server.py:root"},
                    "store": {
                        "index": {
                            "dims": 3,
                            "fields": ["content"],
                            "embed": f"{ROOT}/experiments/stage9/server.py:embeddings",
                        }
                    },
                }
            )
        )
        environment = {
            **isolated_environment(self.event_log),
            "FINANCECLAW_ENVIRONMENT": "test",
            "FINANCECLAW_OFFLINE_MODEL": "true",
            "FINANCECLAW_DEBUG_FULL_IO": "false",
            "FINANCECLAW_DATABASE_URL": f"sqlite+pysqlite:///{self.directory / 'business.db'}",
            "FINANCECLAW_DATABASE_AUTO_CREATE_SCHEMA": "true",
            "FINANCECLAW_ARTIFACT_ROOT": str(self.directory / "artifacts"),
            "FINANCECLAW_ARTIFACT_INLINE_BYTES": "512",
            "FINANCECLAW_EMBEDDING_DIMENSIONS": "3",
            "FINANCECLAW_CONTEXT_RECENT_TURNS": "1",
            "FINANCECLAW_CONTEXT_SUMMARY_TRIGGER_TOKENS": "500",
            "FINANCECLAW_CONTEXT_SOFT_INPUT_TOKENS": "2000",
        }
        self.log = (self.directory / "agent-server.log").open("a")
        try:
            self.process = subprocess.Popen(
                [
                    sys.executable,
                    "-m",
                    "langgraph_cli",
                    "dev",
                    "--config",
                    str(config),
                    "--host",
                    "127.0.0.1",
                    "--port",
                    str(self.port),
                    "--no-browser",
                    "--no-reload",
                    "--allow-blocking",
                    "--server-log-level",
                    "WARNING",
                ],
                cwd=self.directory,
                env=environment,
                stdout=self.log,
                stderr=subprocess.STDOUT,
                start_new_session=True,
            )
            deadline = time.monotonic() + 40
            while time.monotonic() < deadline:
                if self.process.poll() is not None:
                    raise RuntimeError(f"native probe server exited; inspect {self.directory}")
                try:
                    if httpx.get(self.url + "/ok", timeout=1, trust_env=False).is_success:
                        return self
                except httpx.HTTPError:
                    pass
                time.sleep(0.1)
            raise TimeoutError("native server did not become ready")
        except BaseException:
            self.close()
            raise


def run(directory: Path) -> dict:
    """所有数据属于本次隔离目录；不修改原项目的本地数据库或 native 数据目录。"""
    evidence = {
        "mode": "native_agent_server_http_synthetic_models",
        "semantic_quality_verified": False,
        "versions": {
            name: importlib.metadata.version(name)
            for name in ("langchain", "langgraph", "langgraph-api", "langgraph-sdk")
        },
    }
    with ProbeServer(directory) as server:
        client = get_sync_client(url=server.url)
        database = ApplicationDatabase(f"sqlite+pysqlite:///{directory / 'business.db'}")
        repository = SqlAlchemyConversationRepository(database.session_factory)
        conversation = repository.create_conversation(
            tenant_id="probe",
            subject_id="owner",
            agent_id="finance_agent",
            agent_profile_version="1.6.0",
        )
        thread = client.threads.create(thread_id=conversation.agent_thread_id)
        states = []
        for index, text in enumerate(
            ("查明细", "上次的明细呢", "以后都用中文", "记住目标：三年后购房", "查明细跟进")
        ):
            turn, message, _ = repository.begin_turn(
                conversation_id=conversation.conversation_id,
                tenant_id="probe",
                subject_id="owner",
                idempotency_key=f"turn-{index}",
                request_hash=f"{index:064x}",
                message=text,
                target_type="agent",
                target_id="finance_agent",
                target_version="1.6.0",
            )
            context = ExecutionContext(
                tenant_id="probe",
                subject_id="owner",
                conversation_id=conversation.conversation_id,
                turn_id=turn.turn_id,
                run_id=turn.run_id,
                scopes={"memory:read", "memory:write", "memory:delete", "artifacts:read"},
            )
            result = client.runs.wait(
                thread["thread_id"],
                "stage9",
                input={"messages": [{"role": "user", "content": text, "id": message.message_id}]},
                context=context.model_dump(mode="json"),
            )
            state = client.threads.get_state(thread["thread_id"])
            if index == 3:
                assert state["next"], "high-impact goal must interrupt"
                namespace = (*owner_namespace(context), "events")
                assert not client.store.search_items(namespace)["items"]
                result = client.runs.wait(
                    thread["thread_id"],
                    "stage9",
                    command={"resume": {"decisions": [{"type": "approve"}]}},
                    context=context.model_dump(mode="json"),
                )
                assert not client.threads.get_state(thread["thread_id"])["next"]
                assert client.store.search_items(namespace)["items"], result
                evidence["one_native_approval"] = True
            else:
                assert not state["next"]
            assert "__error__" not in result, result
            repository.bind_server_run(turn.turn_id, f"probe-native-{index}", "success")
            repository.append_assistant_message(
                run_id=turn.run_id, content=str(result["messages"][-1]["content"])
            )
            states.append(result)
        assert json.loads(states[1]["messages"][-1]["content"])["previous_tools"] >= 1
        assert any(
            item.get("additional_kwargs", {}).get("lc_source") == "summarization"
            for item in states[-1]["messages"]
        )
        profile = client.store.get_item((*owner_namespace(context), "profile"), "language")
        assert profile["value"]["content"] == "zh-CN"
        evidence.update(
            recent_tool_result_retained=True,
            native_summary_applied=True,
            profile_saved_without_approval=True,
        )
        manifests = repository.list_manifests(conversation.conversation_id)
        evidence["manifest_subtypes"] = sorted({item.subtype for item in manifests})
        assert evidence["manifest_subtypes"] == ["answer", "summary"]
        history_before = client.threads.get_history(thread["thread_id"], limit=1000)
        try:
            response = client.threads.prune([thread["thread_id"]], strategy="keep_latest")
            history_after = client.threads.get_history(thread["thread_id"], limit=1000)
            evidence["keep_latest"] = {
                "supported": True,
                "before": len(history_before),
                "after": len(history_after),
                "result": response,
            }
            assert len(history_after) < len(history_before)
        except httpx.HTTPStatusError as exc:
            evidence["keep_latest"] = {
                "supported": False,
                "status": exc.response.status_code,
                "detail": exc.response.text,
            }
        copy = client.threads.copy(thread["thread_id"])
        deleted = client.threads.prune([copy["thread_id"]], strategy="delete")
        assert deleted["pruned_count"] == 1
        try:
            client.threads.get(copy["thread_id"])
        except httpx.HTTPStatusError as exc:
            assert exc.response.status_code == 404
        else:
            raise AssertionError("native thread deletion was not effective")
        evidence["terminal_thread_delete_supported"] = True
        evidence["embedding_calls"] = [
            json.loads(line) for line in (directory / "embedding.jsonl").read_text().splitlines()
        ]
        assert all(item["success"] for item in evidence["embedding_calls"])
        assert (
            sum(
                item["count"] for item in evidence["embedding_calls"] if item["kind"] == "documents"
            )
            == 1
        )
        assert (
            sum(item["count"] for item in evidence["embedding_calls"] if item["kind"] == "query")
            == 2
        )
        evidence["tool_loop_reuses_initial_event_query"] = True
        evidence["thread_id"] = thread["thread_id"]
        database.close()
    # Reopen the SAME isolated dev persistence directory and check native Store survival.
    with ProbeServer(directory) as restarted:
        client = get_sync_client(url=restarted.url)
        restored = client.store.get_item((*owner_namespace(context), "profile"), "language")
        assert restored["value"]["content"] == "zh-CN"
        assert client.threads.get_state(evidence["thread_id"])["values"]["messages"]
        evidence["dev_restart_store_and_checkpoint"] = True
    evidence["passed"] = True
    return evidence


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--directory", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    result = run(args.directory.resolve())
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n")
    print(json.dumps(result, ensure_ascii=False))
