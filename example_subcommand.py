"""
Example custom subcommand for llm-sandbox.

This file demonstrates how to create a custom subcommand.

To use:
1. Copy to ~/.config/llm-sandbox/subcommands/analyze.py (global)
   OR
2. Copy to .llm-sandbox/subcommands/analyze.py (project-specific)

Then run: llm-sandbox analyze --depth 3

Requirements:
- llm-sandbox must be installed (uv pip install -e .)
- This file will be automatically discovered and loaded
"""

import json
import click
from llm_sandbox.subcommand import Subcommand


class AnalyzeSubcommand(Subcommand):
    """Analyze project for code quality issues."""

    name = "analyze"
    help = "Analyze the project for code quality and security issues"

    def add_arguments(self, command):
        """
        Add custom arguments for this subcommand.

        Note: Common options (--network, --verbose) are automatically
        available to all subcommands.
        """
        # Add custom options
        command.params.append(
            click.Option(
                ["--depth"],
                type=int,
                default=3,
                help="Analysis depth (1-5)",
            )
        )
        command.params.append(
            click.Option(
                ["--output"],
                type=click.Path(dir_okay=False),
                help="Save results to file",
            )
        )
        return command

    def execute(self, project_dir, runner, **kwargs):
        """
        Execute the analysis.

        Args:
            project_dir: Project directory path
            runner: SandboxRunner instance with setup(), run_prompt(), and cleanup() methods
            **kwargs: Additional arguments including:
                - network: Network mode from --network option (common option)
                - verbose: Verbose flag from --verbose option (common option)
                - depth: Custom depth argument
                - output: Custom output file argument
        """
        depth = kwargs.get("depth", 3)
        output_file = kwargs.get("output")
        network = kwargs["network"]
        verbose = kwargs["verbose"]

        click.echo(f"Analyzing project at {project_dir} (depth={depth})")

        # Define the analysis prompt
        prompt = f"""Analyze this project for code quality and security issues.

Analysis depth: {depth}/5

Please examine:
1. Security vulnerabilities
2. Code quality issues
3. Performance concerns
4. Best practice violations

Provide specific file locations and line numbers where possible."""

        # Define the output schema
        output_schema = {
            "type": "object",
            "properties": {
                "summary": {
                    "type": "string",
                    "description": "Overall summary of findings",
                },
                "issues": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "severity": {
                                "type": "string",
                                "enum": ["critical", "high", "medium", "low"],
                            },
                            "category": {
                                "type": "string",
                                "description": "Issue category (security, quality, performance, etc.)",
                            },
                            "description": {
                                "type": "string",
                                "description": "Detailed description of the issue",
                            },
                            "file": {
                                "type": "string",
                                "description": "File path where issue was found",
                            },
                            "line": {
                                "type": "integer",
                                "description": "Line number (if applicable)",
                            },
                        },
                        "required": ["severity", "category", "description"],
                    },
                },
            },
            "required": ["summary", "issues"],
        }

        # Run the sandbox
        # Note: The LLM can use checkout_commit tool to work with any commit/branch
        click.echo("Running analysis in isolated container...")
        try:
            runner.setup(
                keep_branches=[],  # Optional: specify branches to keep
                network=network,
            )
            result = runner.run_prompt(
                prompt=prompt,
                output_schema=output_schema,
                verbose=verbose,
            )
        finally:
            runner.cleanup()

        # Display results
        click.echo("\n" + "=" * 60)
        click.echo("Analysis Results")
        click.echo("=" * 60)
        click.echo(f"\nSummary: {result['summary']}\n")

        if result["issues"]:
            click.echo(f"Found {len(result['issues'])} issue(s):\n")

            # Group by severity
            by_severity = {}
            for issue in result["issues"]:
                severity = issue["severity"]
                if severity not in by_severity:
                    by_severity[severity] = []
                by_severity[severity].append(issue)

            # Display by severity
            for severity in ["critical", "high", "medium", "low"]:
                if severity in by_severity:
                    click.echo(f"\n{severity.upper()} ({len(by_severity[severity])}):")
                    for issue in by_severity[severity]:
                        location = f"{issue.get('file', 'N/A')}"
                        if issue.get("line"):
                            location += f":{issue['line']}"

                        click.echo(f"  [{issue['category']}] {location}")
                        click.echo(f"    {issue['description']}")
        else:
            click.echo("No issues found!")

        # Save to file if requested
        if output_file:
            with open(output_file, "w") as f:
                json.dump(result, f, indent=2)
            click.echo(f"\n✓ Results saved to {output_file}")
