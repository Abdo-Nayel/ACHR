"""
HR serializers.

The single most important thing in this file is that there are **two**
employee serializers. See :class:`EmployeeSelfSerializer`.
"""

from __future__ import annotations

from rest_framework import serializers

from decimal import Decimal

from apps.core.serializers import (
    MoneyField,
    QuantityField,
    RateField,
    TenantScopedSerializer,
)
from apps.hr.models import (
    AttendanceRecord,
    Department,
    Employee,
    EmployeeDocument,
    Holiday,
    JobTitle,
    LeaveBalance,
    LeaveRequest,
    LeaveType,
    OvertimeSlip,
    OvertimeType,
    SalaryRevision,
    Shift,
    ShiftAssignment,
)


class DepartmentSerializer(TenantScopedSerializer):
    """A node in the org chart.

    ``path`` and ``depth`` are maintained by the model (``build_path``) and are
    read-only: the materialised path is what every ``department_subtree`` ABAC
    filter prefix-matches on, so a client that could write it could grant
    itself a subtree.
    """

    manager_name = serializers.CharField(source="manager.full_name", read_only=True)
    employee_count = serializers.IntegerField(source="employees.count", read_only=True)
    children_count = serializers.IntegerField(source="children.count", read_only=True)

    server_owned_fields = ("path", "depth")

    class Meta:
        model = Department
        fields = (
            "id", "code", "name", "parent", "manager", "manager_name",
            "cost_center_account", "path", "depth", "is_active",
            "employee_count", "children_count", "created_at", "updated_at",
        )


class DepartmentTreeSerializer(DepartmentSerializer):
    """Department with its children nested, for the org chart endpoint.

    Built from a single flat query and assembled in Python (see
    ``apps.hr.urls_extra``): recursing with a serializer that queries per node
    is one SELECT per department, which is fine at 12 departments and a
    four-second page load at 400.
    """

    children = serializers.SerializerMethodField()

    class Meta(DepartmentSerializer.Meta):
        fields = DepartmentSerializer.Meta.fields + ("children",)

    def get_children(self, obj: Department) -> list:
        children = self.context.get("children_by_parent", {}).get(obj.id, [])
        return DepartmentTreeSerializer(
            children, many=True, context=self.context
        ).data


class JobTitleSerializer(TenantScopedSerializer):
    """A named role with its salary band.

    The band is compensation data: this serializer is only mounted behind
    ``hr.employee.read_compensation`` for writes, and the band fields are
    dropped on read for callers without it.
    """

    min_salary = MoneyField(required=False)
    max_salary = MoneyField(required=False)

    class Meta:
        model = JobTitle
        fields = (
            "id", "code", "name", "department", "grade", "min_salary",
            "max_salary", "currency", "is_active", "created_at", "updated_at",
        )

    def to_representation(self, instance):
        data = super().to_representation(instance)
        if not self.context.get("may_read_compensation", False):
            data.pop("min_salary", None)
            data.pop("max_salary", None)
        return data


class EmployeeSerializer(TenantScopedSerializer):
    """Full HR view of an employee. **Only for callers with HR permissions.**

    Includes ``base_salary``, ``national_id``, ``tax_id``,
    ``social_insurance_number`` and ``bank_account_iban``. The viewset picks
    this serializer only when the caller holds
    ``hr.employee.read_compensation``; everyone else gets
    :class:`EmployeeSelfSerializer`.
    """

    #: Columns that are compensation or identity data. Writing them requires
    #: ``hr.employee.read_compensation`` as well as ``hr.employee.update`` —
    #: an HR assistant who may correct a phone number must not be able to
    #: award a raise, and salary changes belong in a ``SalaryRevision``
    #: anyway, where they are dated and auditable.
    COMPENSATION_FIELDS = (
        "base_salary", "salary_currency", "bank_account_iban", "bank_name",
        "national_id", "tax_id", "social_insurance_number",
    )

    base_salary = MoneyField(required=False)
    full_name = serializers.CharField(read_only=True)
    department_name = serializers.CharField(source="department.name", read_only=True)
    job_title_name = serializers.CharField(source="job_title.name", read_only=True)
    manager_name = serializers.CharField(source="manager.full_name", read_only=True)

    server_owned_fields = ("status", "termination_date", "termination_reason")

    def validate(self, attrs: dict) -> dict:
        if not self.context.get("may_read_compensation", False):
            offending = [name for name in self.COMPENSATION_FIELDS if name in attrs]
            if offending:
                raise serializers.ValidationError(
                    {
                        name: "Requires the hr.employee.read_compensation "
                              "permission."
                        for name in offending
                    }
                )
        return attrs

    class Meta:
        model = Employee
        fields = (
            "id", "employee_code", "first_name", "last_name", "full_name",
            "arabic_name", "national_id", "date_of_birth", "gender",
            "marital_status", "personal_email", "work_email", "phone",
            "address", "department", "department_name", "job_title",
            "job_title_name", "manager", "manager_name", "work_schedule",
            "employment_type", "status", "hire_date", "probation_end_date",
            "termination_date", "termination_reason", "base_salary",
            "salary_currency", "pay_frequency", "bank_account_iban",
            "bank_name", "tax_id", "social_insurance_number",
            "default_cost_center", "photo_key", "created_at", "updated_at",
        )


class EmployeeSelfSerializer(TenantScopedSerializer):
    """Redacted employee record: directory data only.

    WHY THIS CLASS EXISTS
    ---------------------
    Returning a colleague's salary to a peer is the single most common HR data
    leak in systems like this one, and it almost never happens through a
    deliberate "show me their pay" endpoint. It happens through
    ``GET /employees/`` — an endpoint everyone can reach, built once with the
    full model serializer because that was the obvious thing to write, and
    then never revisited. The leak is silent: no error, no log line, a 200 with
    a field the front-end simply does not render. It surfaces when somebody
    opens dev-tools, or when a mobile client caches the payload, or when a
    junior developer wires a new screen to the same list.

    Two independent controls are therefore in play and both are needed:

    * ABAC narrows *which rows* you see (``own_record`` for the ``employee``
      role, ``department_subtree`` for a manager);
    * this serializer narrows *which columns*, because "you may see that this
      person exists in your department" and "you may see what they earn" are
      different grants — ``hr.employee.read`` versus
      ``hr.employee.read_compensation``.

    Row-level scoping alone is not enough: a department manager legitimately
    sees their whole team's rows and must still not see their salaries.

    Omitted deliberately: ``base_salary``, ``salary_currency``,
    ``bank_account_iban``, ``national_id``, ``tax_id``,
    ``social_insurance_number``, ``date_of_birth`` and ``marital_status``.
    Confidential documents are filtered out of the documents sub-resource by
    the same rule.
    """

    full_name = serializers.CharField(read_only=True)
    department_name = serializers.CharField(source="department.name", read_only=True)
    job_title_name = serializers.CharField(source="job_title.name", read_only=True)
    manager_name = serializers.CharField(source="manager.full_name", read_only=True)

    class Meta:
        model = Employee
        fields = (
            "id", "employee_code", "first_name", "last_name", "full_name",
            "arabic_name", "work_email", "phone", "department",
            "department_name", "job_title", "job_title_name", "manager",
            "manager_name", "employment_type", "status", "hire_date",
            "photo_key",
        )
        read_only_fields = fields


class EmployeeDocumentSerializer(TenantScopedSerializer):
    """A file on an employee's record.

    ``file_key`` is an object-storage key, never a URL: access is granted by a
    short-lived signed URL issued at download time, so a leaked payload does
    not leak the document.
    """

    employee_name = serializers.CharField(source="employee.full_name", read_only=True)

    server_owned_fields = ("uploaded_by", "sha256", "size_bytes")

    class Meta:
        model = EmployeeDocument
        fields = (
            "id", "employee", "employee_name", "document_type", "title",
            "file_key", "content_type", "size_bytes", "sha256", "issue_date",
            "expiry_date", "is_confidential", "uploaded_by", "created_at",
        )


class SalaryRevisionSerializer(TenantScopedSerializer):
    """A pay change, with its before and after.

    Both salaries are stored, not just the new one: "what did this person earn
    in March?" is answered from the revision history, and re-deriving it from
    the current salary minus a chain of deltas gets one rounding wrong and then
    disagrees with the payslips forever. The model refuses ``delete()``.
    """

    previous_salary = MoneyField(read_only=True)
    new_salary = MoneyField()
    employee_name = serializers.CharField(source="employee.full_name", read_only=True)

    server_owned_fields = ("previous_salary", "approved_by", "approved_at")

    class Meta:
        model = SalaryRevision
        fields = (
            "id", "employee", "employee_name", "change_type", "effective_date",
            "previous_salary", "new_salary", "currency", "previous_job_title",
            "new_job_title", "previous_department", "new_department", "reason",
            "approved_by", "approved_at", "created_at",
        )


class ShiftSerializer(TenantScopedSerializer):
    """A working pattern attendance is scored against."""

    expected_hours_per_day = QuantityField(required=False)
    overtime_after_hours = QuantityField(required=False)

    class Meta:
        model = Shift
        fields = (
            "id", "code", "name", "start_time", "end_time", "crosses_midnight",
            "break_minutes", "expected_hours_per_day", "overtime_after_hours",
            "late_grace_minutes", "is_active", "created_at", "updated_at",
        )


class HolidaySerializer(TenantScopedSerializer):
    """A non-working day, optionally scoped to one department."""

    class Meta:
        model = Holiday
        fields = (
            "id", "name", "date", "is_recurring", "applies_to_department",
            "is_paid", "created_at", "updated_at",
        )


class AttendanceRecordSerializer(TenantScopedSerializer):
    """One employee's presence on one day.

    Worked/overtime hours and lateness are computed from the timestamps by the
    check-out action, never accepted from the client — a device that could
    post its own ``worked_hours`` could post eight of them from a car park.
    """

    worked_hours = QuantityField(read_only=True)
    overtime_hours = QuantityField(read_only=True)
    employee_name = serializers.CharField(source="employee.full_name", read_only=True)

    server_owned_fields = (
        "worked_hours", "overtime_hours", "late_minutes",
        "early_leave_minutes", "approved_by", "approved_at",
    )

    class Meta:
        model = AttendanceRecord
        fields = (
            "id", "employee", "employee_name", "work_date", "check_in_at",
            "check_out_at", "scheduled_shift", "worked_hours",
            "overtime_hours", "late_minutes", "early_leave_minutes", "status",
            "source", "check_in_location", "check_out_location", "notes",
            "approved_by", "approved_at", "created_at", "updated_at",
        )


class CheckInSerializer(serializers.Serializer):
    """Body for ``POST /attendance/check-in/`` and ``check-out/``.

    Coordinates are optional: a browser check-in from a desk has no GPS, and
    refusing it would push people to the biometric terminal queue. When they
    are present they are stored verbatim in ``check_in_location`` so a later
    dispute is answered with the data as recorded, not a derived verdict.
    """

    latitude = serializers.DecimalField(
        max_digits=9, decimal_places=6, required=False, allow_null=True,
        min_value=-90, max_value=90, coerce_to_string=True,
    )
    longitude = serializers.DecimalField(
        max_digits=9, decimal_places=6, required=False, allow_null=True,
        min_value=-180, max_value=180, coerce_to_string=True,
    )
    accuracy_metres = serializers.IntegerField(required=False, min_value=0)
    source = serializers.ChoiceField(
        choices=AttendanceRecord.Source.choices,
        default=AttendanceRecord.Source.WEB,
    )
    employee = serializers.UUIDField(
        required=False, allow_null=True,
        help_text="Only honoured for callers who may record others' attendance.",
    )
    notes = serializers.CharField(max_length=500, required=False, allow_blank=True)

    def location(self) -> dict:
        data = self.validated_data
        if data.get("latitude") is None or data.get("longitude") is None:
            return {}
        return {
            "latitude": str(data["latitude"]),
            "longitude": str(data["longitude"]),
            "accuracy_metres": data.get("accuracy_metres"),
            "source": data.get("source"),
        }


class LeaveTypeSerializer(TenantScopedSerializer):
    """A category of leave and the policy that governs it."""

    accrual_rate_days = QuantityField(required=False)
    max_balance_days = QuantityField(required=False)
    carry_over_limit_days = QuantityField(required=False)

    class Meta:
        model = LeaveType
        fields = (
            "id", "code", "name", "is_paid", "accrual_method",
            "accrual_rate_days", "max_balance_days", "allow_negative_balance",
            "carry_over_limit_days", "requires_attachment_after_days",
            "gender_restriction", "min_service_months", "min_notice_days",
            "affects_payroll", "deduction_account", "requires_hr_approval",
            "is_active", "created_at", "updated_at",
        )


class LeaveBalanceSerializer(TenantScopedSerializer):
    """One employee's entitlement for one leave type in one year.

    Every day column is read-only. Balances move through accrual, approval and
    cancellation — all in ``apps.hr.services.leave``, all under a row lock.
    A writable ``available_days`` is an employee granting themselves a holiday.
    ``hr.leave_balance.adjust`` exists for the legitimate manual correction and
    it is a sensitive permission.
    """

    opening_days = QuantityField(read_only=True)
    accrued_days = QuantityField(read_only=True)
    taken_days = QuantityField(read_only=True)
    carried_over_days = QuantityField(read_only=True)
    #: The one writable day column: it *is* the manual-correction channel,
    #: and it is gated on the sensitive ``hr.leave_balance.adjust``
    #: permission by the viewset. Every other column is derived by the
    #: service and read-only.
    adjusted_days = QuantityField(required=False)
    available_days = QuantityField(read_only=True)
    pending_days = QuantityField(read_only=True)

    employee_name = serializers.CharField(source="employee.full_name", read_only=True)
    leave_type_name = serializers.CharField(source="leave_type.name", read_only=True)

    class Meta:
        model = LeaveBalance
        fields = (
            "id", "employee", "employee_name", "leave_type", "leave_type_name",
            "year", "opening_days", "accrued_days", "taken_days",
            "carried_over_days", "adjusted_days", "available_days",
            "pending_days", "last_accrued_on", "created_at", "updated_at",
        )


class LeaveRequestSerializer(TenantScopedSerializer):
    """A request for time off.

    ``total_days`` is computed by ``working_days_between`` (which knows about
    weekends, holidays and half days) when the request is submitted, not taken
    from the client: a client that could name its own day count could book two
    weeks off against half a day of balance.
    """

    total_days = QuantityField(read_only=True)
    employee_name = serializers.CharField(source="employee.full_name", read_only=True)
    leave_type_name = serializers.CharField(source="leave_type.name", read_only=True)
    is_blocking = serializers.BooleanField(read_only=True)

    server_owned_fields = (
        "status", "total_days", "current_approver", "submitted_at",
        "decided_at", "rejection_reason", "balance_applied",
    )

    class Meta:
        model = LeaveRequest
        fields = (
            "id", "employee", "employee_name", "leave_type", "leave_type_name",
            "start_date", "end_date", "half_day_start", "half_day_end",
            "total_days", "reason", "status", "current_approver",
            "attachment_key", "submitted_at", "decided_at",
            "rejection_reason", "balance_applied", "is_blocking",
            "created_at", "updated_at",
        )


class LeaveDecisionSerializer(serializers.Serializer):
    """Body for approve/cancel. ``comment`` is written to the approval trail."""

    comment = serializers.CharField(max_length=500, required=False, allow_blank=True)


class TerminateEmployeeSerializer(serializers.Serializer):
    """Body for ``POST /employees/{id}/terminate``.

    ``termination_date`` is required: ``ck_hr_employee_terminated_has_date``
    refuses a terminated employee without one, because end-of-service
    calculation, the final payslip proration and the statutory filing all key
    off that date.
    """

    termination_date = serializers.DateField()
    reason = serializers.CharField(max_length=255)
    #: Not a writable ``status`` field — this is which of the two *terminal*
    #: outcomes the leaving action records. Every other status change on an
    #: employee remains a separate sub-resource.
    outcome = serializers.ChoiceField(
        choices=[
            (Employee.Status.TERMINATED, "Terminated"),
            (Employee.Status.RESIGNED, "Resigned"),
        ],
        default=Employee.Status.TERMINATED,
    )


class ShiftAssignmentSerializer(TenantScopedSerializer):
    """Which shift an employee works, over a date range."""

    employee_name = serializers.CharField(source="employee.full_name", read_only=True)
    shift_code = serializers.CharField(source="shift.code", read_only=True)

    class Meta:
        model = ShiftAssignment
        fields = (
            "id", "employee", "employee_name", "shift", "shift_code",
            "location", "start_date", "end_date", "notes",
            "created_at", "updated_at",
        )

    def validate(self, attrs):
        instance = getattr(self, "instance", None)
        start = attrs.get("start_date", getattr(instance, "start_date", None))
        end = attrs.get("end_date", getattr(instance, "end_date", None))
        if start and end and end < start:
            raise serializers.ValidationError(
                {"end_date": "An assignment cannot end before it starts."}
            )
        return attrs


class OvertimeTypeSerializer(TenantScopedSerializer):
    """A category of overtime and its multiplier."""

    multiplier = RateField(min_value=Decimal("0.000001"))
    component_code = serializers.CharField(source="component.code", read_only=True)

    class Meta:
        model = OvertimeType
        fields = (
            "id", "code", "name", "multiplier", "component", "component_code",
            "requires_approval", "is_active", "created_at", "updated_at",
        )


class OvertimeSlipSerializer(TenantScopedSerializer):
    """Hours worked beyond the shift.

    ``hourly_rate`` and ``amount`` are server-owned: they are computed by
    ``apps.hr.services.overtime.price_slip`` at approval, against the salary
    in force on the day worked. A client that could set them could pay itself
    any figure it liked while every total still balanced.

    ``status`` is server-owned for the same reason it is on every other
    document in this codebase — moving it is a transition with its own
    permission (``POST .../approve``), not a field write.
    """

    server_owned_fields = (
        "hourly_rate", "amount", "status", "approved_by", "approved_at",
        "payroll_run",
    )

    employee_name = serializers.CharField(source="employee.full_name", read_only=True)
    overtime_type_code = serializers.CharField(
        source="overtime_type.code", read_only=True
    )
    hours = QuantityField(min_value=Decimal("0.01"))

    class Meta:
        model = OvertimeSlip
        fields = (
            "id", "employee", "employee_name", "overtime_type",
            "overtime_type_code", "work_date", "hours", "hourly_rate",
            "amount", "currency", "status", "approved_by", "approved_at",
            "rejected_reason", "payroll_run", "notes",
            "created_at", "updated_at",
        )
        read_only_fields = (
            "hourly_rate", "amount", "status", "approved_by", "approved_at",
            "payroll_run",
        )
