"""
Example: Parallel Agent Execution

This example demonstrates running multiple agents in parallel, all sharing
the same container, instance_id, and worktrees environment.

The LLMProvider (ClaudeProvider) has an async-only interface. When multiple
tools are requested in a single turn, they are executed concurrently.
"""

import asyncio
from pathlib import Path
from llm_sandbox import SandboxRunner, Agent
from llm_sandbox.config import Config
from llm_sandbox.mcp_tools import (
    MCPServer,
    ExecuteCommandTool,
    CheckoutCommitTool,
    ReadFileTool,
    GitCommitTool,
)


def create_mcp_server_for_agent(runner: SandboxRunner) -> MCPServer:
    """
    Create an MCP server with tools for an agent.

    All tools have async interfaces and will execute concurrently when
    multiple tools are requested in a single turn.
    """
    server = MCPServer()
    server.add_tools([
        ExecuteCommandTool(runner),
        CheckoutCommitTool(runner),
        ReadFileTool(runner),
        GitCommitTool(runner),
    ])
    return server


async def main():
    # Setup
    project_path = Path.cwd()
    config = Config()  # Load from default config file

    # Runner setup happens automatically in constructor
    runner = SandboxRunner(project_path, config, verbose=True)

    # Use async context manager for proper cleanup
    async with runner:
        # Create agents
        # Each agent gets its own MCP server with tools
        # In practice, you might want different tool sets per agent

        agents = [
            Agent(
                runner=runner,
                prompt="Analyze the main Python files in the src/ directory",
                output_schema={
                    "type": "object",
                    "properties": {
                        "files_analyzed": {"type": "array", "items": {"type": "string"}},
                        "summary": {"type": "string"},
                    },
                    "required": ["files_analyzed", "summary"],
                },
                mcp_server=create_mcp_server_for_agent(runner),
            ),
            Agent(
                runner=runner,
                prompt="Find all test files and summarize the testing approach",
                output_schema={
                    "type": "object",
                    "properties": {
                        "test_files": {"type": "array", "items": {"type": "string"}},
                        "testing_approach": {"type": "string"},
                    },
                    "required": ["test_files", "testing_approach"],
                },
                mcp_server=create_mcp_server_for_agent(runner),
            ),
            Agent(
                runner=runner,
                prompt="Analyze the project's configuration files (pyproject.toml, setup.py, etc.)",
                output_schema={
                    "type": "object",
                    "properties": {
                        "config_files": {"type": "array", "items": {"type": "string"}},
                        "dependencies": {"type": "array", "items": {"type": "string"}},
                        "project_name": {"type": "string"},
                    },
                    "required": ["config_files", "dependencies", "project_name"],
                },
                mcp_server=create_mcp_server_for_agent(runner),
            ),
        ]

        # Start all agents in parallel
        print("\n" + "="*60)
        print("Running 3 agents in parallel...")
        print("="*60)

        # Execute all agents (starts them)
        for agent in agents:
            await agent.execute()

        # Wait for all agents to complete
        results = []
        for agent in agents:
            try:
                result = await agent.wait()
                results.append({"success": True, "result": result})
            except Exception as e:
                results.append({"success": False, "error": str(e)})

        # Display results
        print("\n" + "="*60)
        print("RESULTS:")
        print("="*60)

        for i, (agent, result) in enumerate(zip(agents, results), 1):
            print(f"\n[Agent {i}: {agent.agent_id}]")
            print(f"Success: {result.get('success', False)}")
            if not result.get('success'):
                print(f"Error: {result.get('error', 'Unknown error')}")
            else:
                print(f"Result: {result.get('result', {})}")

        # Cleanup happens automatically in __aexit__


if __name__ == "__main__":
    asyncio.run(main())
