"""Sentry error monitoring setup.

Initializes Sentry SDK when CK_SENTRY_DSN is set. Captures unhandled
exceptions in FastAPI, background threads, and the node agent.
Environment-aware (dev/staging/prod) via CK_ENVIRONMENT.
"""

from __future__ import annotations

import logging
import os

logger = logging.getLogger(__name__)

SENTRY_DSN = os.environ.get("CK_SENTRY_DSN", "")
ENVIRONMENT = os.environ.get("CK_ENVIRONMENT", "development")


def init_sentry() -> None:
    """Initialize Sentry if DSN is configured. No-op otherwise."""
    if not SENTRY_DSN:
        return

    try:
        import sentry_sdk
        from sentry_sdk.integrations.fastapi import FastApiIntegration
        from sentry_sdk.integrations.starlette import StarletteIntegration

        sentry_sdk.init(
            dsn=SENTRY_DSN,
            environment=ENVIRONMENT,
            integrations=[
                StarletteIntegration(transaction_style="endpoint"),
                FastApiIntegration(transaction_style="endpoint"),
            ],
            traces_sample_rate=0.1,
            send_default_pii=False,
        )
        logger.info(f"Sentry initialized (env={ENVIRONMENT})")
    except ImportError:
        logger.warning("sentry-sdk not installed, skipping Sentry init")
    except Exception as e:
        logger.warning(f"Sentry init failed: {e}")
