"""
HR URL registration.

The four original prefixes are unchanged. The rest — job titles, shifts,
holidays, leave types, leave balances, employee documents and salary revisions
— are viewsets that already existed in :mod:`apps.hr.viewsets` with no route.

``EmployeeViewSet`` chooses its serializer per caller: anyone without
``hr.employee.read_compensation`` gets
:class:`~apps.hr.serializers.EmployeeSelfSerializer`, because row scope alone
is not enough — a department manager legitimately sees their whole team's rows
and must still not see their salaries.
"""

from __future__ import annotations

from apps.hr.viewsets import (
    AttendanceRecordViewSet,
    DepartmentViewSet,
    EmployeeDocumentViewSet,
    EmployeeViewSet,
    HolidayViewSet,
    JobTitleViewSet,
    LeaveBalanceViewSet,
    LeaveRequestViewSet,
    LeaveTypeViewSet,
    OvertimeSlipViewSet,
    OvertimeTypeViewSet,
    SalaryRevisionViewSet,
    ShiftAssignmentViewSet,
    ShiftViewSet,
)


def register(router) -> None:
    """Mount this app's routes on the v1 router."""
    router.register(r"departments", DepartmentViewSet, basename="departments")
    router.register(r"employees", EmployeeViewSet, basename="employees")
    router.register(r"attendance", AttendanceRecordViewSet, basename="attendance")
    router.register(r"leave-requests", LeaveRequestViewSet, basename="leave-requests")
    router.register(r"job-titles", JobTitleViewSet, basename="job-titles")
    router.register(r"shifts", ShiftViewSet, basename="shifts")
    router.register(r"holidays", HolidayViewSet, basename="holidays")
    router.register(r"leave-types", LeaveTypeViewSet, basename="leave-types")
    router.register(r"leave-balances", LeaveBalanceViewSet, basename="leave-balances")
    router.register(
        r"employee-documents", EmployeeDocumentViewSet, basename="employee-documents"
    )
    router.register(
        r"salary-revisions", SalaryRevisionViewSet, basename="salary-revisions"
    )
    router.register(
        r"shift-assignments", ShiftAssignmentViewSet, basename="shift-assignments"
    )
    router.register(r"overtime-types", OvertimeTypeViewSet, basename="overtime-types")
    router.register(r"overtime-slips", OvertimeSlipViewSet, basename="overtime-slips")
