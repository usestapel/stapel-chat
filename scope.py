"""scope_key provider — the scope/operator extension seam.

The library is scope-agnostic: ``Conversation.scope_key`` is an opaque string
the host owns. A ``ScopeProvider`` (dotted path in
``STAPEL_CHAT["SCOPE_PROVIDER"]``) resolves the scope_key from the current
request, filters querysets by it, and answers the one authority question the
support surface needs: *may this caller act as an operator here?*

That last question used to have no seam, and its absence was a live
escalation. ``SupportAssignView`` was first-come-served behind
``IsAuthenticated``; assigning writes a ``ConversationParticipant`` with
``role=OPERATOR``; and every later check on the thread asks the participant
table — which now answers with the row the caller minted a moment ago. One
POST, and a stranger's support conversation was readable, postable and
resolvable. The participant row is not the bug: asking about it *first* was.

:meth:`ScopeProvider.can_operate` is the question that comes before it, and
the shipped provider answers it with the mandate rather than with
``is_authenticated``: an operator is staff of some workspace. Deliberately
NOT applied to the customer half — a person opening a support ticket
typically holds no mandate at all, and refusing them would close the product
to fix the door.
"""
from __future__ import annotations

from stapel_core.django.scope import MandateScopeMixin


class ScopeProvider:
    """Contract for scope resolution/filtering + operator authority. Subclass
    and point ``STAPEL_CHAT["SCOPE_PROVIDER"]`` at it to scope the chat."""

    def resolve(self, request) -> str:
        """Return the scope_key to stamp on conversations created via ``request``."""
        raise NotImplementedError

    def filter(self, queryset, request):
        """Restrict ``queryset`` to the scope visible to ``request``."""
        raise NotImplementedError

    def can_operate(self, request, conversation=None) -> bool:
        """May ``request``'s user act as a support OPERATOR — read the queue,
        claim a thread, resolve/reopen it — optionally in the context of
        ``conversation`` (whose ``scope_key`` a workspace-aware provider will
        want to check a capability against).

        Answer False for "no". Raise
        ``stapel_core.django.api.permissions.MandateUnavailable`` (503) for
        "could not find out": admitting on a failed lookup is how this seam
        was open in the first place.
        """
        raise NotImplementedError


class DefaultScopeProvider(MandateScopeMixin, ScopeProvider):
    """Single global scope: every conversation gets ``scope_key=""`` and no
    query is filtered by scope. Suitable for single-tenant hosts and tests.

    ``can_operate`` is the exception to "no-op": it answers with the third
    principal state (``stapel_core.django.scope``), so a registered account
    holding no mandate anywhere is not an operator of anything. In a genuinely
    standalone deployment — where nothing can answer that question and so
    nobody holds a mandate — it stays permissive, and ``checks.py`` says so
    out loud rather than leaving it implied.

    Swap for a workspace-aware provider in production: this one closes the
    guest state, it does not separate one tenant's conversations from
    another's (``stapel_chat.E005``).
    """

    def resolve(self, request) -> str:
        return ""

    def filter(self, queryset, request):
        return queryset

    def can_operate(self, request, conversation=None) -> bool:
        return self.mandate_admits(request)


def get_scope_provider() -> ScopeProvider:
    """Resolve the configured provider (already import_string'd by conf)."""
    from .conf import chat_settings

    provider = chat_settings.SCOPE_PROVIDER
    return provider() if isinstance(provider, type) else provider
