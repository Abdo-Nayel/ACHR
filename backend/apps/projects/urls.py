"""
Projects URL registration.

``projects``, ``project-tasks`` and ``timesheets`` keep their prefixes —
``timesheets`` deliberately, even though the viewset is
``TimesheetEntryViewSet``: the frontend routes are built from the prefix.
``project-members`` and ``project-milestones`` are new.
"""

from __future__ import annotations

from apps.projects.viewsets import (
    ProjectMemberViewSet,
    ProjectMilestoneViewSet,
    ProjectTaskViewSet,
    ProjectViewSet,
    TimesheetEntryViewSet,
)


def register(router) -> None:
    """Mount this app's routes on the v1 router."""
    router.register(r"projects", ProjectViewSet, basename="projects")
    router.register(r"project-tasks", ProjectTaskViewSet, basename="project-tasks")
    router.register(r"timesheets", TimesheetEntryViewSet, basename="timesheets")
    router.register(r"project-members", ProjectMemberViewSet, basename="project-members")
    router.register(
        r"project-milestones", ProjectMilestoneViewSet, basename="project-milestones"
    )
