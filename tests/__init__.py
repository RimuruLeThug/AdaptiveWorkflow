"""Garde-fou commun à toute la suite : aucun modèle réel, aucun réseau.

Depuis la V3, les rôles sont confiés par défaut à Claude Haiku (Claude Code) avec le Qwen
local (Ollama) en repli. Sur une machine où Claude Code est installé et où Ollama tourne,
un test qui oublierait d'injecter un double appellerait un vrai modèle. On neutralise donc
les points d'accès par défaut : un ClaudeCodeClient construit sans `runner` ni `executable`
ne trouve pas la commande « claude », et un OllamaBackend / LocalOpenAIBackend construit sans
`transport` reçoit un transport qui échoue comme un serveur éteint. Les tests qui injectent
leurs doubles ne sont pas concernés.
"""

import functools

import claude_cli
import local_llm

_ClaudeInit = claude_cli.ClaudeCodeClient.__init__
_OllamaInit = local_llm.OllamaBackend.__init__


def _no_server(method, url, body, timeout):
    raise local_llm.LocalLLMError("Ollama ne répond pas (tests : aucun modèle réel n'est appelé).")


@functools.wraps(_ClaudeInit)
def _claude_init(self, *args, **kw):
    if "runner" not in kw and "executable" not in kw:
        kw.setdefault("which", lambda name: None)
    _ClaudeInit(self, *args, **kw)


@functools.wraps(_OllamaInit)
def _ollama_init(self, *args, **kw):
    kw.setdefault("transport", _no_server)
    _OllamaInit(self, *args, **kw)


claude_cli.ClaudeCodeClient.__init__ = _claude_init
local_llm.OllamaBackend.__init__ = _ollama_init

try:  # backend local alternatif (agents.py) : même règle
    import agents

    _OpenAIInit = agents.LocalOpenAIBackend.__init__

    @functools.wraps(_OpenAIInit)
    def _openai_init(self, *args, **kw):
        kw.setdefault("transport", _no_server)
        _OpenAIInit(self, *args, **kw)

    agents.LocalOpenAIBackend.__init__ = _openai_init
except ImportError:  # pragma: no cover
    pass
