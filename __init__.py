"""GNOME-first Vercel agent-desktop ComputerUseProvider for Hermes."""
from .backend import AgentDesktopProvider


def register(ctx):
    ctx.register_computer_use_provider(AgentDesktopProvider())
