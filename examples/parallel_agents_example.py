"""
Example: Parallel Agent Execution

This example demonstrates running multiple agents in parallel, all sharing
the same container, instance_id, and worktrees environment.

The LLMProvider (ClaudeProvider) has an async-only interface. When multiple
tools are requested in a single turn, they are executed concurrently.
"""

import asyncio
from pathlib import Path
from llm_sandbox import SandboxRunner, AgentConfig, MCPServer
from llm_sandbox.config import Config
from llm_sandbox.mcp_tools import (
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

    runner = SandboxRunner(project_path, config)
    runner.setup()

    try:
        # Create agent configurations
        # All agents will use the SAME async MCP server in this simple example
        # In practice, you might want different tool sets per agent

        agents = [
            AgentConfig(
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
                agent_id="python-analyzer",
            ),
            AgentConfig(
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
                agent_id="test-analyzer",
            ),
            AgentConfig(
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
                agent_id="config-analyzer",
            ),
        ]

        # Run agents in parallel
        print("\n" + "="*60)
        print("Running 3 agents in parallel...")
        print("="*60)

        results = await runner.run_agents(agents, verbose=True)

        # Display results
        print("\n" + "="*60)
        print("RESULTS:")
        print("="*60)

        for i, (agent, result) in enumerate(zip(agents, results)):
            print(f"\n[Agent {agent.agent_id}]")
            print(f"Success: {result.get('success', 'N/A' if 'error' not in result else False)}")
            if "error" in result:
                print(f"Error: {result['error']}")
            else:
                print(f"Result: {result}")

    finally:
        # Cleanup (removes all worktrees and branches at once)
        runner.cleanup()


if __name__ == "__main__":
    asyncio.run(main())
