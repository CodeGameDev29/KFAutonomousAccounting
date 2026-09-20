"""REST API router aggregation for the web app.

Mount all sub-routers into a single APIRouter, then include in app.py.
"""

from fastapi import APIRouter

from server.api.accounts import router as accounts_router
from server.api.actions import router as actions_router
from server.api.analytics import router as analytics_router
from server.api.connections import router as connections_router
from server.api.dashboard import router as dashboard_router
from server.api.gather import router as gather_router
from server.api.jobs import router as jobs_router
from server.api.onboarding import router as onboarding_router
from server.api.reasonableness import router as reasonableness_router
from server.api.receipts import router as receipts_router
from server.api.reconciliation import router as reconciliation_router
from server.api.reports import router as reports_router
from server.api.statements import router as statements_router
from server.api.transactions import router as transactions_router

api_router = APIRouter()

api_router.include_router(dashboard_router)
api_router.include_router(accounts_router)
api_router.include_router(analytics_router)
api_router.include_router(reasonableness_router)
api_router.include_router(receipts_router)
api_router.include_router(transactions_router)
api_router.include_router(statements_router)
api_router.include_router(reconciliation_router)
api_router.include_router(reports_router)
api_router.include_router(actions_router)
api_router.include_router(onboarding_router)
api_router.include_router(connections_router)
api_router.include_router(gather_router)
api_router.include_router(jobs_router)

# These routers are mounted separately in app.py because they carry their own
# prefixes and their own auth rules:
# - server.api.files  (file upload/download, signed links)
# - server.api.events (server-sent events)
# - server.api.shared (public share links, no auth required)
