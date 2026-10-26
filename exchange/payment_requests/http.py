"""Uninstalled native handler extension. No transfer/confirmation endpoint."""
import re
from .core import ControlError
from .repository import RepositoryError
from .service import PREFIX

ACCEPTANCE = re.compile(r'/api/owner/fulfillment/([A-Za-z0-9_-]{1,100})/delivery-review')


def with_payment_request_handler(base_handler):
    class PaymentRequestHandler(base_handler):
        def _api(self, path, query, principal, document=None):
            service = getattr(self.server, 'payment_requests', None)
            accepted = ACCEPTANCE.fullmatch(path) if isinstance(path, str) else None
            if service is not None and service.enabled and self.command == 'POST' and accepted:
                # Preserve native acceptance even when the independent request
                # budget is stopped/full. No earned obligation is erased and no
                # original financial permission is created by this derivative.
                status, result, replay = super()._api(path, query, principal, document)
                if type(result) is dict and result.get('decision') == 'accept' and result.get('earned_obligation') is True:
                    result = dict(result)
                    try:
                        result['paymentRequest'] = service.prepare_for_accepted_job(principal, accepted.group(1))
                    except (ControlError, RepositoryError) as error:
                        result['paymentRequest'] = {'status': 'held', 'reason': error.code,
                                                    'executionEnabled': False, 'handoffAvailable': False}
                    except Exception:
                        # Native identity/clock/store exceptions can occur after
                        # acceptance committed. Preserve that authoritative result
                        # and disclose only a redacted derivative hold. Replaying
                        # the native acceptance retries this idempotent derivation.
                        result['paymentRequest'] = {'status': 'held', 'reason': 'request_preparation_unavailable',
                                                    'executionEnabled': False, 'handoffAvailable': False}
                return status, result, replay
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
