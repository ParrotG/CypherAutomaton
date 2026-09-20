"""A deliberately simple task scheduler for multiple agent workers.

It is task-blind: it passes a task description and target to workers, creates
one bridge connector per worker, and replaces terminated workers until the
total-worker cap is reached.  It does not inspect progress, implement a
blackboard, or perform semantic/error detection.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
import shutil
import socket
import sys
import uuid
from datetime import datetime, timezone
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import httpx
import uvicorn

from incypher_agent.toolchain import default_tool_root, ensure_tool_root
from incypher_bridge.app import create_app
from incypher_bridge.client import BridgeClient
from incypher_bridge.settings import load_settings

from .config import SchedulerConfig, generate_attempt_id, safe_component
from .targets import TargetKind, TargetSpec, classify_target


class SchedulerError(RuntimeError):
    """Raised when the scheduler cannot start or maintain workers."""


@dataclass
class WorkerProcess:
    worker_id: str
    connector_id: str
    run_dir: Path
    process: asyncio.subprocess.Process
    log_handle: Any
    started_at: str
    outcome_recorded: bool = False


class EmbeddedBridge:
    """Run the FastAPI bridge inside the scheduler process."""

    def __init__(self, settings) -> None:
        self.settings = settings
        self.app = create_app(settings)
        self.server = uvicorn.Server(
            uvicorn.Config(
                self.app,
                host=settings.api_host,
                port=settings.api_port,
                log_level="warning",
                access_log=False,
            )
        )
        self.task: asyncio.Task[None] | None = None
        self.base_url = f"http://{settings.api_host}:{settings.api_port}"

    async def start(self) -> None:
        self.task = asyncio.create_task(self.server.serve(), name="embedded-bridge")
        await self._wait_health()

    async def _wait_health(self) -> None:
        deadline = asyncio.get_running_loop().time() + 15.0
        async with httpx.AsyncClient(timeout=1.0) as client:
            while asyncio.get_running_loop().time() < deadline:
                try:
                    response = await client.get(f"{self.base_url}/health")
                    if response.status_code == 200:
                        return
                except Exception:
                    pass
                await asyncio.sleep(0.05)
        raise SchedulerError(f"embedded bridge did not start at {self.base_url}")

    async def stop(self) -> None:
        self.server.should_exit = True
        if self.task is not None:
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await asyncio.wait_for(self.task, timeout=5.0)


class SimpleScheduler:
    """Create bridge connectors and run concurrent agent worker processes."""

    def __init__(self, config: SchedulerConfig) -> None:
        config.validate()
        self.config = config
        self._client: BridgeClient | None = None
        self._embedded: EmbeddedBridge | None = None
        self._sandbox_tool_root: Path | None = None
        self.target_spec: TargetSpec = classify_target(config.target)
        self.attempt_id = generate_attempt_id()
        self.attempt_root = config.attempts_root / self.attempt_id
        self.workers_root = self.attempt_root / "workers"
        self._blackboard_dir: Path | None = None
        self._blackboard_visible_path: str = "/blackboard"

    async def run(self) -> int:
        self.workers_root.mkdir(parents=True, exist_ok=True)
        self._blackboard_dir = self.config.task_root / "blackboard"
        (self._blackboard_dir / "records").mkdir(parents=True, exist_ok=True)
        self._blackboard_visible_path = (
            "/blackboard"
            if self.config.sandbox_backend == "bwrap"
            else str(self._blackboard_dir)
        )
        await self._ensure_sandbox_tools()
        if self.target_spec.kind == TargetKind.RAW_TCP:
            await self._start_bridge()
        started = 0
        running: dict[asyncio.Task[int], WorkerProcess] = {}
        success = False
        self._log(
            "scheduler_start",
            task_id=self.config.task_id,
            target=self.config.target,
            target_kind=self.target_spec.kind.value,
            attempt_id=self.attempt_id,
            attempt_dir=str(self.attempt_root),
        )
        try:
            while True:
                while (
                    not success
                    and len(running) < self.config.max_concurrent_workers
                    and started < self.config.max_total_workers
                ):
                    worker = await self._spawn_worker(started + 1)
                    started += 1
                    running[asyncio.create_task(worker.process.wait())] = worker
                    self._log(
                        "worker_started",
                        worker_id=worker.worker_id,
                        connector_id=worker.connector_id,
                        running=len(running),
                        started=started,
                    )

                if not running:
                    break

                done, _ = await asyncio.wait(
                    running,
                    return_when=asyncio.FIRST_COMPLETED,
                )
                for task in done:
                    worker = running.pop(task)
                    code = task.result()
                    self._log(
                        "worker_exit",
                        worker_id=worker.worker_id,
                        connector_id=worker.connector_id,
                        exit_code=code,
                    )
                    await self._cleanup_worker(worker)
                    if code == 0:
                        success = True

                if success:
                    break

            if success:
                for task, worker in list(running.items()):
                    task.cancel()
                    await self._cleanup_worker(worker, terminate=True)
                return 0

            self._log(
                "scheduler_exhausted",
                started=started,
                max_total_workers=self.config.max_total_workers,
            )
            return 10
        finally:
            for task, worker in list(running.items()):
                task.cancel()
                with contextlib.suppress(Exception):
                    await self._cleanup_worker(worker, terminate=True)
            await self._stop_bridge()

    async def _ensure_sandbox_tools(self) -> None:
        if self.config.sandbox_backend != "bwrap":
            return
        tool_root = (
            Path(self.config.sandbox_tool_root).expanduser().resolve()
            if self.config.sandbox_tool_root
            else default_tool_root()
        )
        self._sandbox_tool_root = await asyncio.to_thread(
            ensure_tool_root, tool_root
        )

    # ------------------------------------------------------------------
    # Bridge lifecycle
    # ------------------------------------------------------------------
    async def _start_bridge(self) -> None:
        if self.config.bridge_url:
            self._client = BridgeClient(self.config.bridge_url)
            return
        port = self.config.bridge_port or _find_free_port()
        settings = load_settings(
            state_dir=self.config.state_dir / "bridge",
            env_file=self.config.env_file,
            team_key_file=self.config.team_key_file,
            allow_insecure_file=self.config.allow_insecure_key_file,
            max_handshakes=self.config.max_handshakes,
            api_host=self.config.bridge_host,
            api_port=port,
        )
        self._embedded = EmbeddedBridge(settings)
        await self._embedded.start()
        self._client = BridgeClient(self._embedded.base_url)

    async def _stop_bridge(self) -> None:
        if self._client is not None:
            await self._client.aclose()
            self._client = None
        if self._embedded is not None:
            await self._embedded.stop()
            self._embedded = None

    # ------------------------------------------------------------------
    # Worker lifecycle
    # ------------------------------------------------------------------
    async def _spawn_worker(self, index: int) -> WorkerProcess:
        worker_id = f"w{index:04d}"
        worker_dir = self.workers_root / worker_id
        worker_dir.mkdir(parents=True, exist_ok=True)
        connector_id = ""
        endpoint: str | None = None
        worker_target = self.target_spec.normalized

        if self.target_spec.kind == TargetKind.RAW_TCP:
            if self._client is None:
                raise SchedulerError("bridge client is not available")
            connector_id = safe_component(
                f"{self.config.task_id[:32]}-{worker_id}",
                fallback=f"connector-{index}",
            )[:64]
            await self._client.create_connector(
                connector_id=connector_id,
                target=self.target_spec.normalized,
                auto_reconnect=True,
                metadata={
                    "task_id": self.config.task_id,
                    "worker_id": worker_id,
                },
            )
            try:
                info = await self._client.wait_ready(connector_id, timeout=30.0)
                endpoint = info.get("endpoint")
                if not endpoint:
                    raise SchedulerError(
                        f"connector {connector_id} has no endpoint"
                    )
            except BaseException:
                with contextlib.suppress(Exception):
                    await self._client.delete_connector(connector_id)
                raise
        elif self.target_spec.kind == TargetKind.FILES:
            await asyncio.to_thread(self._copy_target_files, worker_dir)
            worker_target = "/workspace/challenge_files"

        command = self._worker_command(
            worker_id,
            worker_dir,
            endpoint,
            worker_target,
        )
        log_path = worker_dir / "worker.log"
        log_handle = log_path.open("ab", buffering=0)
        env = dict(os.environ)
        env["PYTHONUNBUFFERED"] = "1"
        try:
            process = await asyncio.create_subprocess_exec(
                *command,
                stdout=log_handle,
                stderr=asyncio.subprocess.STDOUT,
                cwd=str(Path.cwd()),
                env=env,
            )
        except BaseException:
            log_handle.close()
            if connector_id and self._client is not None:
                with contextlib.suppress(Exception):
                    await self._client.delete_connector(connector_id)
            raise
        return WorkerProcess(
            worker_id=worker_id,
            connector_id=connector_id,
            run_dir=worker_dir,
            process=process,
            log_handle=log_handle,
            started_at=datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        )

    def _copy_target_files(self, worker_dir: Path) -> None:
        if not self.target_spec.local_path:
            raise SchedulerError("file target has no local path")
        source = Path(self.target_spec.local_path)
        destination = worker_dir / "agent" / "workspace" / "challenge_files"
        if destination.exists():
            shutil.rmtree(destination)
        destination.parent.mkdir(parents=True, exist_ok=True)
        if source.is_dir():
            shutil.copytree(source, destination, dirs_exist_ok=True)
        else:
            destination.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source, destination / source.name)

    def _worker_command(
        self,
        worker_id: str,
        worker_dir: Path,
        endpoint: str | None,
        worker_target: str,
    ) -> list[str]:
        command = [
            sys.executable if part == "python" else part
            for part in self.config.worker_command
        ]
        command.extend(
            [
                "--run-dir",
                str(worker_dir),
                "--challenge-id",
                self.config.task_id,
                "--run-id",
                worker_id,
            ]
        )
        if endpoint:
            command.extend(["--agent-endpoint", endpoint])
        command.extend(
            [
                "--env-file",
                str(self.config.env_file),
                "--max-seconds",
                str(self.config.worker_max_seconds),
                "--context-window-tokens",
                str(self.config.worker_context_window_tokens),
                "--context-reserve-tokens",
                str(self.config.worker_context_reserve_tokens),
                "--bash-timeout",
                str(self.config.worker_bash_timeout),
                "--max-tool-output",
                str(self.config.worker_max_tool_output),
                "--sandbox-backend",
                self.config.sandbox_backend,
                "--sandbox-bwrap",
                self.config.sandbox_bwrap,
                "--quiet",
            ]
        )
        if self.config.sandbox_network is False:
            command.append("--sandbox-no-network")
        if self.config.wait_for_verification is False:
            command.append("--no-wait-verification")
        if self._sandbox_tool_root is not None:
            command.extend(["--sandbox-tool-root", str(self._sandbox_tool_root)])
        if self._blackboard_dir is not None:
            command.extend(
                [
                    "--blackboard-dir",
                    str(self._blackboard_dir),
                    "--blackboard-path",
                    self._blackboard_visible_path,
                ]
            )
        if self.config.challenge_name:
            command.extend(["--challenge-name", self.config.challenge_name])
        if self.config.category:
            command.extend(["--category", self.config.category])
        command.extend(["--target", worker_target])
        if self.config.description_file:
            command.extend(["--description-file", str(Path(self.config.description_file).resolve())])
        elif self.config.description_text is not None:
            command.extend(["--description-text", self.config.description_text])
        if self.config.model:
            command.extend(["--model", self.config.model])
        if self.config.base_url:
            command.extend(["--base-url", self.config.base_url])
        if self.config.thinking:
            command.extend(["--thinking", self.config.thinking])
        if self.config.temperature is not None:
            command.extend(["--temperature", str(self.config.temperature)])
        return command

    async def _cleanup_worker(
        self,
        worker: WorkerProcess,
        *,
        terminate: bool = False,
    ) -> None:
        process = worker.process
        if terminate and process.returncode is None:
            process.terminate()
        if process.returncode is None:
            try:
                await asyncio.wait_for(process.wait(), timeout=5.0)
            except asyncio.TimeoutError:
                process.kill()
                with contextlib.suppress(Exception):
                    await process.wait()
        try:
            worker.log_handle.close()
        except Exception:
            pass
        if not worker.outcome_recorded:
            self._write_system_record(worker)
            worker.outcome_recorded = True
        if self._client is not None and worker.connector_id:
            with contextlib.suppress(Exception):
                await self._client.delete_connector(worker.connector_id)

    def _write_system_record(self, worker: WorkerProcess) -> None:
        if self._blackboard_dir is None:
            return
        records = self._blackboard_dir / "records"
        records.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        name = f"system-{worker.worker_id}-{stamp}-{uuid.uuid4().hex[:8]}.json"
        final_path = records / name
        temp_path = records / f".{name}.tmp"
        payload = {
            "worker_id": worker.worker_id,
            "attempt_id": self.attempt_id,
            "exit_code": worker.process.returncode,
            "started_at": worker.started_at,
            "finished_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "run_dir": str(worker.run_dir),
        }
        try:
            with temp_path.open("w", encoding="utf-8") as handle:
                json.dump(payload, handle, ensure_ascii=False, sort_keys=True)
                handle.write("\n")
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temp_path, final_path)
        finally:
            with contextlib.suppress(OSError):
                temp_path.unlink(missing_ok=True)

    # ------------------------------------------------------------------
    # Logging
    # ------------------------------------------------------------------
    @staticmethod
    def _log(event: str, **fields: Any) -> None:
        print(json.dumps({"event": event, **fields}, ensure_ascii=False), flush=True)


def _find_free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])
