"""Typer CLI client for the Praxis orchestrator API."""

from __future__ import annotations

import os
from typing import Any

import httpx
import typer
from rich.console import Console
from rich.table import Table


app = typer.Typer(name="orchestrator-cli", help="AI Agent Orchestrator CLI")
console = Console()


def _api_url() -> str:
    return os.environ.get("ORCHESTRATOR_URL", "http://localhost:8080")


def _auth_token() -> str:
    token = os.environ.get("ORCHESTRATOR_TOKEN", "")
    if not token:
        console.print("[red]Set ORCHESTRATOR_TOKEN env var[/red]")
        raise typer.Exit(1)
    return token


def _client() -> httpx.Client:
    return httpx.Client(
        base_url=_api_url(),
        headers={"Authorization": f"Bearer {_auth_token()}"},
        timeout=60.0,
    )


def _check(response: httpx.Response) -> dict[str, Any] | list[dict[str, Any]]:
    if response.status_code >= 400:
        console.print(f"[red]Error {response.status_code}:[/red] {response.text}")
        raise typer.Exit(1)
    data = response.json()
    if not isinstance(data, (dict, list)):
        raise typer.Exit(1)
    return data


def _check_dict(response: httpx.Response) -> dict[str, Any]:
    data = _check(response)
    if not isinstance(data, dict):
        raise typer.Exit(1)
    return data


def _check_list(response: httpx.Response) -> list[dict[str, Any]]:
    data = _check(response)
    if not isinstance(data, list):
        raise typer.Exit(1)
    return data


@app.command()
def projects() -> None:
    """List all projects."""

    with _client() as client:
        data = _check_list(client.get("/api/projects"))
    table = Table(title="Projects")
    table.add_column("ID", style="dim", max_width=8)
    table.add_column("Name")
    table.add_column("Repo")
    table.add_column("Model")
    table.add_column("Gate")
    for project in data:
        table.add_row(
            project["id"][:8],
            project["name"],
            project["repo_url"],
            project["model_name"],
            "ON" if project["approval_gate"] else "OFF",
        )
    console.print(table)


@app.command()
def add_project(
    name: str = typer.Argument(..., help="Project display name"),
    repo: str = typer.Argument(..., help="GitHub repo URL"),
    model: str = typer.Argument(..., help="LM Studio model name"),
) -> None:
    """Register a new GitHub repository."""

    with _client() as client:
        data = _check_dict(
            client.post(
                "/api/projects",
                json={"name": name, "repo_url": repo, "model_name": model},
            )
        )
    console.print(f"[green]Created project:[/green] {data['id']}")


@app.command()
def configure(
    project_id: str = typer.Argument(..., help="Project ID"),
    gate: bool | None = typer.Option(None, help="Approval gate on/off"),
    threshold: float | None = typer.Option(None, help="Confidence threshold"),
    retries: int | None = typer.Option(None, help="Max retries"),
) -> None:
    """Update project settings."""

    body: dict[str, Any] = {}
    if gate is not None:
        body["approval_gate"] = gate
    if threshold is not None:
        body["confidence_threshold"] = threshold
    if retries is not None:
        body["max_retries"] = retries
    if not body:
        console.print("[yellow]No settings to update[/yellow]")
        return
    with _client() as client:
        data = _check_dict(client.patch(f"/api/projects/{project_id}", json=body))
    console.print(f"[green]Updated project:[/green] {data['name']}")


@app.command()
def submit(
    project_id: str = typer.Argument(..., help="Project ID"),
    spec: str = typer.Argument(..., help="Specification text"),
) -> None:
    """Submit a specification for planning."""

    with _client() as client:
        data = _check_dict(
            client.post(f"/api/projects/{project_id}/plans", json={"spec": spec})
        )
    console.print(f"[green]Plan created:[/green] {data['id']} ({data['status']})")


@app.command()
def plans(project_id: str = typer.Argument(..., help="Project ID")) -> None:
    """List plans for a project."""

    with _client() as client:
        data = _check_list(client.get(f"/api/projects/{project_id}/plans"))
    table = Table(title="Plans")
    table.add_column("ID", style="dim", max_width=8)
    table.add_column("Spec", max_width=40)
    table.add_column("Source")
    table.add_column("Status")
    for plan in data:
        spec_display = (plan.get("spec_path") or "")[:40]
        table.add_row(plan["id"][:8], spec_display, plan["source"], plan["status"])
    console.print(table)


@app.command()
def approve(plan_id: str = typer.Argument(..., help="Plan ID")) -> None:
    """Approve an autonomous improvement plan."""

    with _client() as client:
        data = _check_dict(client.post(f"/api/plans/{plan_id}/approve"))
    console.print(f"[green]Plan approved:[/green] {data['id']}")


@app.command()
def reject(plan_id: str = typer.Argument(..., help="Plan ID")) -> None:
    """Reject an autonomous improvement plan."""

    with _client() as client:
        data = _check_dict(client.post(f"/api/plans/{plan_id}/reject"))
    console.print(f"[red]Plan rejected:[/red] {data['id']}")


@app.command()
def tasks(plan_id: str = typer.Argument(..., help="Plan ID")) -> None:
    """List tasks in a plan."""

    with _client() as client:
        data = _check_list(client.get(f"/api/plans/{plan_id}/tasks"))
    table = Table(title="Tasks")
    table.add_column("ID", style="dim", max_width=8)
    table.add_column("Title")
    table.add_column("Branch")
    table.add_column("Status")
    table.add_column("Attempt")
    for task in data:
        table.add_row(
            task["id"][:8],
            task["title"],
            task["branch_name"],
            task["status"],
            str(task["attempt"]),
        )
    console.print(table)


@app.command()
def task(task_id: str = typer.Argument(..., help="Task ID")) -> None:
    """Get task details with agent run history."""

    with _client() as client:
        data = _check_dict(client.get(f"/api/tasks/{task_id}"))
    task_data = data["task"]
    console.print(f"[bold]{task_data['title']}[/bold]")
    console.print(
        "Status: "
        f"{task_data['status']} | Branch: {task_data['branch_name']} | "
        f"Attempt: {task_data['attempt']}"
    )
    if task_data["pr_url"]:
        console.print(f"PR: {task_data['pr_url']}")
    if task_data["review_feedback"]:
        console.print(f"[yellow]Feedback:[/yellow] {task_data['review_feedback']}")
    for run in data["runs"]:
        console.print(f"  {run['id'][:8]} | {run['status']} | {run['started_at']}")


@app.command()
def stop(task_id: str = typer.Argument(..., help="Task ID")) -> None:
    """Stop a running agent."""

    with _client() as client:
        data = _check_dict(client.post(f"/api/tasks/{task_id}/stop"))
    console.print(f"[yellow]Stopped {data['stopped']} agent(s)[/yellow]")


@app.command()
def status() -> None:
    """Show orchestrator status."""

    with _client() as client:
        data = _check_dict(client.get("/api/status"))
    opus_state = data["opus_state"]
    console.print(f"Opus: [bold]{opus_state['status']}[/bold]")
    if opus_state["resume_at"]:
        console.print(f"  Resume at: {opus_state['resume_at']}")
    console.print(f"  Queued actions: {opus_state['queued_count']}")
    console.print(
        f"Active agents: {data['active_agents']} / {data['total_agents']} total"
    )


if __name__ == "__main__":
    app()
