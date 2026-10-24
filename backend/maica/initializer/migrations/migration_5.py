"""Retire the legacy remote vector schema migration."""

from maica.maica_utils import sync_messenger, MsgType
from .base import register_migration

target_version = "1.3.000.rc2"


async def migrate():
    """Keep the historical migration slot without touching vector storage."""
    sync_messenger(
        info="[migration-5] Legacy vector schema migration retired; no action required",
        type=MsgType.DEBUG,
    )


register_migration(target_version, migrate)
