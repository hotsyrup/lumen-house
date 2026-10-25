"""Additive host-handler factory. Nothing mounts or enables it on import.

Uses existing host bearer parsing, bounded unique-key JSON parsing, host/origin
guards, upload deadline, rate limits, no-store headers and error redaction.
"""
from .core import ControlError
from .repository import RepositoryError
from .service import exchange_path


def with_exchange_handler(base_handler):
    class ExchangeHandler(base_handler):
        def _authorize_route(self, path, principal):
            service = getattr(self.server, 'exchange', None)
            if not exchange_path(path) or service is None or not service.enabled:
                return super()._authorize_route(path, principal)
            from server import AUTHORITY_HEADERS
            from sourcing import DomainError
            if any(self.headers.get_all(name) for name in AUTHORITY_HEADERS):
                raise DomainError('invalid_authority', 'Authority rejected.', 400)
            if principal.role not in ('owner', 'buyer', 'provider'):
                raise DomainError('forbidden', 'Role rejected.', 403)
            service.authorize_scope(principal, self.command, path)

        def _api(self, path, query, principal, document=None):
            service = getattr(self.server, 'exchange', None)
            if not exchange_path(path) or service is None or not service.enabled:
                return super()._api(path, query, principal, document)
            from sourcing import DomainError
            self._authorize_route(path, principal)
            params = self._query(query, ())
            try:
                return service.dispatch(self.command, path, params, principal,
                                        document, self.headers.get('Idempotency-Key'))
            except (ControlError, RepositoryError) as error:
                # Stable codes only; never echo terms, evidence, SQL or actors.
                raise DomainError(error.code, 'Exchange request rejected.', error.status) from None

    return ExchangeHandler
