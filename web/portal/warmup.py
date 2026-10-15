"""Load in the web server's master process what a worker's first page would otherwise load, so workers forked from
it start warm. Only portal.wsgi calls it: the bot, migrations and manage.py stay cold."""

import gc
import logging
from importlib import import_module
from pathlib import Path

from django.conf import settings
from django.template import engines
from django.urls import get_resolver
from django.utils.formats import get_format

logger = logging.getLogger(__name__)


def warm() -> None:
    """Import the URLconf (and so the view modules it imports), build its reverse lookups, load the date format and
    compile every .html template the template loaders can find, then freeze it all so the workers share it."""
    import_module(settings.ROOT_URLCONF)
    get_resolver().reverse_dict  # noqa: B018 - reading it builds it
    get_format("DATE_FORMAT")
    engine = engines["django"]
    directories = dict.fromkeys(
        Path(directory) for loader in engine.engine.template_loaders for directory in loader.get_dirs()
    )
    for directory in directories:
        for path in directory.rglob("*.html"):
            name = path.relative_to(directory).as_posix()
            # A broken template should fail the pages that use it, not stop the server from starting
            try:
                engine.get_template(name)
            except Exception:
                logger.exception("Could not warm template %s", name)
    # Keep the garbage collector from touching these objects, which would copy their memory into every worker
    gc.freeze()
