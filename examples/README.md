# Examples

This directory contains example code and configurations for `llm-sandbox`.

## Contents

- **[subcommands/](subcommands/)** - Example custom subcommands demonstrating how to extend llm-sandbox with your own workflows
  - `pr-review.py` - Interactive GitHub PR review that posts LLM suggestions as inline review comments

## Quick Start

### Using the PR Review Example

1. Copy the example subcommand:
   ```bash
   mkdir -p .llm-sandbox/subcommands
   cp examples/subcommands/pr-review.py .llm-sandbox/subcommands/
   ```

2. Run it on a GitHub PR:
   ```bash
   llm-sandbox pr-review --pr 123
   ```

   The LLM will analyze the PR, show you suggestions interactively, and post accepted ones as inline comments directly on the code in GitHub's "Files changed" tab.

See [subcommands/README.md](subcommands/README.md) for detailed documentation and how to create your own subcommands.
