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
import socket
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import httpx
import uvicorn

from incypher_bridge.app import create_app
from incypher_bridge.client import BridgeClient
from incypher_bridge.settings import load_settings

from .config import SchedulerConfig, safe_component


class SchedulerError(RuntimeError):
    """Raised when the scheduler cannot start or maintain workers."""


@dataclass
class WorkerProcess:
    worker_id: str
    connector_id: str
    run_dir: Path
    process: asyncio.subprocess.Process
    log_handle: Any


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

    async def run(self) -> int:
        self.config.workers_root.mkdir(parents=True, exist_ok=True)
        await self._start_bridge()
        started = 0
        running: dict[asyncio.Task[int], WorkerProcess] = {}
        success = False
        self._log("scheduler_start", task_id=self.config.task_id, target=self.config.target)
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
        assert self._client is not None
        worker_id = f"w{index:04d}"
        worker_dir = self.config.workers_root / worker_id
        worker_dir.mkdir(parents=True, exist_ok=True)
        connector_id = safe_component(
            f"{self.config.task_id[:32]}-{worker_id}", fallback=f"connector-{index}"
        )[:64]

        await self._client.create_connector(
            connector_id=connector_id,
            target=self.config.target,
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
                raise SchedulerError(f"connector {connector_id} has no endpoint")
            command = self._worker_command(worker_id, worker_dir, endpoint)
            log_path = worker_dir / "worker.log"
            log_handle = log_path.open("ab", buffering=0)
            env = dict(os.environ)
            env["PYTHONUNBUFFERED"] = "1"
            process = await asyncio.create_subprocess_exec(
                *command,
                stdout=log_handle,
                stderr=asyncio.subprocess.STDOUT,
                cwd=str(Path.cwd()),
                env=env,
            )
        except BaseException:
            with contextlib.suppress(Exception):
                await self._client.delete_connector(connector_id)
            raise
        return WorkerProcess(
            worker_id=worker_id,
            connector_id=connector_id,
            run_dir=worker_dir,
            process=process,
            log_handle=log_handle,
        )

    def _worker_command(
        self,
        worker_id: str,
        worker_dir: Path,
        endpoint: str,
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
                "--agent-endpoint",
                endpoint,
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
                "--quiet",
            ]
        )
        if self.config.challenge_name:
            command.extend(["--challenge-name", self.config.challenge_name])
        if self.config.category:
            command.extend(["--category", self.config.category])
        command.extend(["--target", self.config.target])
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
        if self._client is not None:
            with contextlib.suppress(Exception):
                await self._client.delete_connector(worker.connector_id)

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
