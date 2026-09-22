"""Declarative Microsoft Foundry agent factory.

You describe agents in YAML; this applies them to a Foundry project. Agents are
versioned, so re-applying a changed spec creates a new version rather than
clobbering the old one -- which is what you want when you sell this to clients
and need to roll back a prompt.

Targets the Foundry projects (new) API via azure-ai-projects >= 2.3.
Auth is `az login` / managed identity through DefaultAzureCredential -- no keys in files.

    pip install "azure-ai-projects>=2.5.0" azure-identity pyyaml
    az login
    export FOUNDRY_PROJECT_ENDPOINT="https://<resource>.services.ai.azure.com/api/projects/<project>"
    python -m brain.cli foundry apply agents/

Spec format (see agents/*.yaml):

    name: qgis-troubleshooter
    model: gpt-5-mini
    instructions: |
      ...
    tools:
      - type: openapi            # the brain itself
        name: brain
        spec_url: http://localhost:8077/openapi-for-foundry
        description: Search archived Discord threads and GIS forum answers.
        auth: anonymous          # or: connection, managed_identity
        connection_id: brain-api-key
      - type: mcp
        server_label: ecomsp_brain
        server_url: https://brain.example.com/mcp
      - type: web_search
      - type: code_interpreter
"""
from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml


@dataclass
class AgentSpec:
    name: str
    model: str
    instructions: str
    tools: list[dict] = field(default_factory=list)
    description: str | None = None
    metadata: dict = field(default_factory=dict)

    @classmethod
    def from_file(cls, path: str | Path) -> "AgentSpec":
        data = yaml.safe_load(Path(path).read_text())
        missing = {"name", "model", "instructions"} - data.keys()
        if missing:
            raise ValueError(f"{path}: missing required keys {sorted(missing)}")
        return cls(name=data["name"], model=data["model"],
                   instructions=data["instructions"].strip(),
                   tools=data.get("tools", []) or [],
                   description=data.get("description"),
                   metadata=data.get("metadata", {}) or {})


def _load_spec_document(tool: dict) -> dict:
    """Resolve an OpenAPI document from spec_url, spec_file or inline spec."""
    if "spec" in tool:
        return tool["spec"]
    if path := tool.get("spec_file"):
        text = Path(path).read_text()
        return yaml.safe_load(text) if path.endswith((".yml", ".yaml")) else json.loads(text)
    if url := tool.get("spec_url"):
        import requests
        r = requests.get(url, timeout=30)
        r.raise_for_status()
        return r.json()
    raise ValueError(f"openapi tool '{tool.get('name')}' needs spec, spec_file or spec_url")


def build_tools(tools: list[dict]) -> list[Any]:
    """Translate plain dicts into azure.ai.projects.models tool objects."""
    from azure.ai.projects import models as m

    built: list[Any] = []
    for t in tools:
        kind = t.get("type")
        if kind == "openapi":
            auth_kind = t.get("auth", "anonymous")
            if auth_kind == "anonymous":
                auth = m.OpenApiAnonymousAuthDetails()
            elif auth_kind in ("connection", "api_key"):
                # Store the key in a Foundry project connection whose key NAME matches
                # the securityScheme header name -- X-API-Key here. The spec served by
                # /openapi-for-foundry already declares that scheme.
                auth = m.OpenApiProjectConnectionAuthDetails(
                    security_scheme=m.OpenApiProjectConnectionSecurityScheme(
                        project_connection_id=t["connection_id"]))
            elif auth_kind == "managed_identity":
                auth = m.OpenApiManagedAuthDetails(
                    security_scheme=m.OpenApiManagedSecurityScheme(audience=t["audience"]))
            else:
                raise ValueError(f"unknown openapi auth: {auth_kind}")
            built.append(m.OpenApiTool(openapi=m.OpenApiFunctionDefinition(
                name=t["name"],
                spec=_load_spec_document(t),
                description=t.get("description", ""),
                auth=auth,
            )))
        elif kind == "mcp":
            built.append(m.MCPTool(
                server_label=t["server_label"],
                server_url=t["server_url"],
                server_description=t.get("description"),
                allowed_tools=t.get("allowed_tools"),
                require_approval=t.get("require_approval", "never"),
            ))
        elif kind == "web_search":
            built.append(m.WebSearchTool())
        elif kind == "code_interpreter":
            built.append(m.CodeInterpreterTool())
        elif kind == "file_search":
            built.append(m.FileSearchTool(vector_store_ids=t.get("vector_store_ids", [])))
        else:
            raise ValueError(f"unsupported tool type: {kind}")
    return built


class FoundryFactory:
    def __init__(self, endpoint: str | None = None, credential=None):
        from azure.ai.projects import AIProjectClient
        from azure.identity import DefaultAzureCredential

        self.endpoint = endpoint or os.environ.get("FOUNDRY_PROJECT_ENDPOINT")
        if not self.endpoint:
            raise SystemExit("set FOUNDRY_PROJECT_ENDPOINT "
                             "(https://<resource>.services.ai.azure.com/api/projects/<project>)")
        self.project = AIProjectClient(endpoint=self.endpoint,
                                       credential=credential or DefaultAzureCredential())

    # ----------------------------------------------------------------- create
    def apply(self, spec: AgentSpec) -> dict:
        from azure.ai.projects.models import PromptAgentDefinition

        definition = PromptAgentDefinition(
            model=spec.model,
            instructions=spec.instructions,
            tools=build_tools(spec.tools) or None,
        )
        agent = self.project.agents.create_version(agent_name=spec.name,
                                                   definition=definition)
        return {"name": agent.name, "id": agent.id, "version": agent.version,
                "tools": [t.get("type") for t in spec.tools]}

    def apply_dir(self, directory: str | Path) -> list[dict]:
        out = []
        for path in sorted(Path(directory).glob("*.y*ml")):
            spec = AgentSpec.from_file(path)
            result = self.apply(spec)
            print(f"applied {path.name}: {result['name']} v{result['version']}")
            out.append(result)
        return out

    # ------------------------------------------------------------------ admin
    def list_agents(self) -> list[dict]:
        return [{"name": a.name, "id": getattr(a, "id", None),
                 "version": getattr(a, "version", None)}
                for a in self.project.agents.list()]

    def delete(self, name: str) -> None:
        self.project.agents.delete(agent_name=name)

    # -------------------------------------------------------------- smoke test
    def ask(self, agent_name: str, question: str) -> str:
        """One-shot sanity check that the agent and its tools actually work."""
        response = self.project.responses.create(
            extra_body={"agent": {"name": agent_name, "type": "agent_reference"}},
            input=question,
        )
        return getattr(response, "output_text", None) or str(response)
