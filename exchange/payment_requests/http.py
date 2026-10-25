"""Uninstalled native handler extension. No transfer/confirmation endpoint."""
from .core import ControlError
from .repository import RepositoryError
from .service import PREFIX


def with_payment_request_handler(base_handler):
    class PaymentRequestHandler(base_handler):
        def _api(self, path, query, principal, document=None):
            service = getattr(self.server, 'payment_requests', None)
            if service is None or not service.enabled or not (path == PREFIX or path.startswith(PREFIX + '/')):
                return super()._api(path, query, principal, document)
            from sourcing import DomainError
            self._authorize_route(path, principal)
            try:
                return service.dispatch(self.command, path, self._query(query, ()), principal,
                                        document, self.headers.get('Idempotency-Key'))
            except (ControlError, RepositoryError) as error:
                raise DomainError(error.code, 'Payment request held; no transfer was made.', error.status) from None
    return PaymentRequestHandler
