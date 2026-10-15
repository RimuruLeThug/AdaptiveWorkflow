"""Organisateur Claude, via Claude Code sur l'abonnement Pro (claude_cli.py).

Mêmes consignes que l'organisateur Gemini ; seul le modèle change. Les workers
restent sur Gemini. Chaque plan et chaque synthèse consomment le quota de
l'abonnement Claude Pro : ce mode n'est jamais présenté comme gratuit.
"""

from __future__ import annotations

from claude_cli import ClaudeCodeClient
from organizers.gemini import GeminiOrganizer


class ClaudeOrganizer(GeminiOrganizer):
    def __init__(self, client: ClaudeCodeClient) -> None:
        # La CLI ne règle pas la température : les valeurs transmises sont ignorées.
        super().__init__(client)
