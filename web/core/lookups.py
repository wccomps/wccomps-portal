"""Field lookups the project registers on Django's fields (core.apps.CoreConfig.ready imports this module)."""

from django.db import models


@models.GenericIPAddressField.register_lookup
class InNetwork(models.Lookup[str]):
    """ip__in_network=network: the address lies within the network (Postgres inet <<=)."""

    lookup_name = "in_network"

    def as_sql(self, compiler, connection):  # type: ignore[no-untyped-def]
        lhs, lhs_params = self.process_lhs(compiler, connection)
        rhs, rhs_params = self.process_rhs(compiler, connection)
        return f"{lhs} <<= {rhs}", (*lhs_params, *rhs_params)
