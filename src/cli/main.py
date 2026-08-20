"""Typer CLI client for the Praxis orchestrator API."""

from __future__ import annotations

import os
from typing import Any

import httpx
import typer
from rich.console import Console
from rich.table import Table

from cli.doctor import doctor as _doctor
from cli.init import init as _init


app = typer.Typer(name="orchestrator-cli", help="AI Agent Orchestrator CLI")
console = Console()
app.command("doctor")(_doctor)
app.command("init")(_init)


def _api_url() -> str:
    return os.environ.get("ORCHESTRATOR_URL", "http://localhost:12323")


def _auth_token() -> str:
    # AUTH_TOKEN is the name .env / .env.example / the dashboard document;
    # ORCHESTRATOR_TOKEN is kept as a fallback so an existing user who only
    # set the CLI's original var name (including praxis init's own printed
    # cli_env_exports) is not broken.
    token = os.environ.get("AUTH_TOKEN") or os.environ.get("ORCHESTRATOR_TOKEN", "")
    if not token:
        console.print("[red]Set AUTH_TOKEN (or ORCHESTRATOR_TOKEN) env var[/red]")
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
    table.add_column("ID", style="dim", max_width=36, overflow="fold")
    table.add_column("Spec", max_width=40)
    table.add_column("Source")
    table.add_column("Status")
    for plan in data:
        spec_display = (plan.get("spec_path") or "")[:40]
        table.add_row(plan["id"], spec_display, plan["source"], plan["status"])
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


@app.command()
def mode(action: str = typer.Argument(..., help="on | off | status")) -> None:
    """Turn auto-delegate mode on/off or show its status."""
    action_lower = action.lower()
    if action_lower not in {"on", "off", "status"}:
        console.print("action must be one of: on, off, status")
        raise typer.Exit(code=2)
    with _client() as client:
        if action_lower == "status":
            resp = client.get("/api/settings/auto-delegate")
        else:
            resp = client.put(
                "/api/settings/auto-delegate", json={"enabled": action_lower == "on"}
            )
        data = _check_dict(resp)
    enabled_str = "ON" if data.get("enabled") else "OFF"
    worker = data.get("worker") or {}
    harness = worker.get("harness", "")
    model = worker.get("model") or "unset"
    console.print(f"auto-delegate: {enabled_str} (worker: {harness} / {model})")


@app.command()
def pending() -> None:
    """List tasks parked at the human merge gate."""
    with _client() as client:
        data = _check_dict(client.get("/api/approvals/pending"))
    if not data["count"]:
        console.print("[green]Nothing awaiting approval.[/green]")
        return
    table = Table(title=f"{data['count']} awaiting approval")
    for column in ("Age", "Task", "Branch", "PR"):
        table.add_column(column)
    for task in data["tasks"]:
        table.add_row(
            f"{int(task['age_hours'])}h",
            task["title"] or task["task_id"],
            task["branch"] or "",
            task["pr_url"] or "",
        )
    console.print(table)


@app.command()
def merge(
    task_id: str = typer.Argument(..., help="Full task ID from `praxis pending`"),
) -> None:
    """Approve and merge one review-passed task parked at the merge gate."""

    with _client() as client:
        data = _check_dict(client.post(f"/api/tasks/{task_id}/approve-merge"))
    console.print(f"[green]Merged:[/green] {data['task_id']} ({data['status']})")


@app.command("merge-plan")
def merge_plan(
    plan_id: str = typer.Argument(..., help="Plan ID from `praxis plans`"),
) -> None:
    """Approve every review-passed task parked in one plan."""

    with _client() as client:
        data = _check_dict(client.post(f"/api/plans/{plan_id}/approve-merges"))
    approved = int(data.get("approved") or 0)
    errors = data.get("errors") or []
    console.print(f"[green]Merged:[/green] {approved} task(s)")
    for failure in errors:
        console.print(
            f"[red]Failed:[/red] {failure.get('task_id', '?')}: "
            f"{failure.get('error', 'unknown error')}"
        )
    if errors:
        raise typer.Exit(1)


config_app = typer.Typer(
    name="config", help="Configure the model registry and role chains"
)
app.add_typer(config_app)


@config_app.command("show")
def config_show() -> None:
    """Show registered models, role fallback chains, and capabilities."""
    with _client() as client:
        registry = _check_list(client.get("/api/settings/registry"))
        roles = _check_dict(client.get("/api/settings/roles"))
        caps = _check_dict(client.get("/api/settings/capabilities"))
    cap_models = caps.get("models", {})

    reg_table = Table(title="Registered Models")
    for col in ("Name", "Provider", "Model", "Effort", "SWE-bench", "Speed", "$/Mtok"):
        reg_table.add_column(col)
    for m in registry:
        cap = cap_models.get(m.get("model", ""), {})
        swe = cap.get("swe_bench_verified")
        reg_table.add_row(
            m["name"],
            m["provider"],
            m.get("model") or "-",
            m.get("effort") or "-",
            f"{swe:.0%}" if isinstance(swe, (int, float)) else "-",
            str(cap.get("speed_tps", "-")),
            str(cap.get("price_per_mtok_blended", "-")),
        )
    console.print(reg_table)

    role_table = Table(title="Role Fallback Chains (first = priority)")
    role_table.add_column("Role")
    role_table.add_column("Chain")
    for role, chain in roles.items():
        role_table.add_row(role, " -> ".join(chain) if chain else "(default)")
    console.print(role_table)
    console.print(f"[dim]Capabilities as of {caps.get('as_of')}[/dim]")


@config_app.command("set-role")
def config_set_role(
    role: str = typer.Argument(..., help="plan | review | implement"),
    chain: str = typer.Argument(
        ..., help="Comma-separated model names, priority first"
    ),
) -> None:
    """Set a role's ordered fallback chain."""
    names = [n.strip() for n in chain.split(",") if n.strip()]
    with _client() as client:
        current = _check_dict(client.get("/api/settings/roles"))
        current[role] = names
        _check_dict(client.put("/api/settings/roles", json={"chains": current}))
    console.print(f"[green]Set {role}:[/green] {' -> '.join(names)}")


@config_app.command("add-model")
def config_add_model(
    name: str = typer.Argument(..., help="Registry name"),
    provider: str = typer.Argument(..., help="claude | codex | agy | local"),
    model: str = typer.Argument("", help="Provider model id"),
    effort: str = typer.Option("", help="Optional effort (e.g. high)"),
) -> None:
    """Register (or replace) a model in the registry."""
    with _client() as client:
        registry = _check_list(client.get("/api/settings/registry"))
        registry = [m for m in registry if m["name"] != name]
        registry.append(
            {
                "name": name,
                "provider": provider,
                "model": model,
                "effort": effort or None,
            }
        )
        _check_list(client.put("/api/settings/registry", json=registry))
    console.print(f"[green]Registered:[/green] {name} ({provider}/{model or '-'})")


@config_app.command("refresh-capabilities")
def config_refresh_capabilities() -> None:
    """Attempt to refresh the capability snapshot (bundled-only in v1)."""
    with _client() as client:
        data = _check_dict(client.post("/api/settings/capabilities/refresh"))
    console.print(f"[yellow]{data.get('status')}[/yellow]: {data.get('detail')}")


@app.command()
def onboard() -> None:
    """First-run helper: point the operator at model configuration."""
    console.print(
        "[bold]Welcome to Praxis.[/bold] No models configured yet.\n"
        "Run [cyan]praxis config[/cyan] to register models and set role fallback chains,\n"
        "or [cyan]praxis config show[/cyan] to view the current defaults."
    )


if __name__ == "__main__":
    app()
