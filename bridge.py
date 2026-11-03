#!/usr/bin/env python3
"""Entry point for systemd — thin launcher for claude_gateway (v2).

Keeps the historical path:
  ExecStart=.../python3 .../bridge.py
so the unit file does not need a path change.
"""
from claude_gateway.app import main

if __name__ == "__main__":
    main()
