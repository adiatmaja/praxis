"""Docker container lifecycle management for Aider agents."""

from __future__ import annotations

import logging
from typing import Any

import docker
import docker.errors


logger = logging.getLogger(__name__)

AGENT_IMAGE = "aider-agent:latest"


class AgentManager:
    """Manage Aider agent Docker containers."""

    def __init__(self, lm_studio_url: str, github_token: str) -> None:
        self._lm_studio_url = lm_studio_url
        self._github_token = github_token
        self._client = docker.from_env()

    def spawn_agent(
        self,
        task_id: str,
        repo_url: str,
        branch: str,
        base_branch: str,
        task_prompt: str,
        model_name: str,
        callback_url: str,
    ) -> str:
        environment = {
            "REPO_URL": repo_url,
            "BRANCH": branch,
            "BASE_BRANCH": base_branch,
            "TASK_PROMPT": task_prompt,
            "OPENAI_API_BASE": f"{self._lm_studio_url}/v1",
            "AIDER_MODEL": f"openai/{model_name}",
            "GH_TOKEN": self._github_token,
            "CALLBACK_URL": callback_url,
            "TASK_ID": task_id,
        }
        container = self._client.containers.run(
            image=AGENT_IMAGE,
            name=f"aider-agent-{task_id[:8]}",
            environment=environment,
            detach=True,
            auto_remove=False,
            network_mode="host",
        )
        logger.info(
            "Spawned agent container %s for task %s on branch %s",
            container.id[:12],
            task_id,
            branch,
        )
        return str(container.id)

    def get_container_status(self, container_id: str) -> dict[str, Any] | None:
        try:
            container = self._client.containers.get(container_id)
        except docker.errors.NotFound:
            return None
        return {
            "status": container.status,
            "exit_code": container.attrs["State"]["ExitCode"],
        }

    def get_container_logs(self, container_id: str, tail: int = 500) -> str:
        try:
            container = self._client.containers.get(container_id)
        except docker.errors.NotFound:
            return ""
        return str(container.logs(tail=tail).decode())

    def stop_agent(self, container_id: str) -> None:
        try:
            container = self._client.containers.get(container_id)
        except docker.errors.NotFound:
            logger.warning("Container %s not found for stop", container_id[:12])
            return
        container.stop(timeout=30)
        logger.info("Stopped container %s", container_id[:12])

    def cleanup_container(self, container_id: str) -> None:
        try:
            container = self._client.containers.get(container_id)
        except docker.errors.NotFound:
            return
        container.remove(force=True)
        logger.info("Removed container %s", container_id[:12])

    def list_agent_containers(self) -> list[dict[str, Any]]:
        containers = self._client.containers.list(
            all=True,
            filters={"name": "aider-agent-"},
        )
        return [
            {
                "id": container.id,
                "name": container.name,
                "status": container.status,
                "exit_code": container.attrs["State"]["ExitCode"],
            }
            for container in containers
        ]
