"""Discord UI layer: Views/Modals plus the admin-dashboard embed builder.

UI classes reference the discord client through jobs.client (set once by
bot.py) and schedule auto-closes through the jobs module, keeping this package
free of bot.py imports.

Implementation lives in focused modules; this file re-exports the public API.
"""
from views_books import BookActionView, ReadingListView
from views_dashboard import AdminDashboardView, build_admin_dashboard_embed
from views_enrollment import EnrollmentView, _temporary_enrollment_messages
from views_polls import CoordinationView, NominateView, VotingView
from views_import import GoodreadsImportView
from views_queue import (
    NextBookPickerView,
    OfferNextBookView,
    RevealPrivateListView,
    SaveBookDestinationView,
)

__all__ = [
    "AdminDashboardView",
    "BookActionView",
    "CoordinationView",
    "EnrollmentView",
    "GoodreadsImportView",
    "NominateView",
    "NextBookPickerView",
    "OfferNextBookView",
    "ReadingListView",
    "RevealPrivateListView",
    "SaveBookDestinationView",
    "VotingView",
    "_temporary_enrollment_messages",
    "build_admin_dashboard_embed",
]
