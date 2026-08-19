"""
Payroll serializers.

Everything here is derived. A payslip is not a document a client fills in: it
is the output of ``apps.payroll.services.engine.calculate_run`` and every
amount on it is computed from the employee's salary, their components, their
attendance and the tax scale in force on the pay date. The serializers are
therefore read-only over the money, and the run's status moves only through
POST sub-resources.
"""

from __future__ import annotations

from rest_framework import serializers

from apps.core.fields import ZERO
from apps.core.serializers import (
    MoneyField,
    QuantityField,
    RateField,
    ReadOnlyModelSerializer,
    TenantScopedSerializer,
)
from apps.payroll.models import (
    EmployeePayrollProfile,
    PayrollComponent,
    PayrollRun,
    Payslip,
    PayslipLine,
    SalaryStructure,
    SalaryStructureAssignment,
    SalaryStructureLine,
    TaxBracket,
)


class PayrollComponentSerializer(TenantScopedSerializer):
    """An earning, deduction or employer contribution.

    ``sequence`` is the evaluation order and it matters: a percentage-of-gross
    component evaluated before the earnings it is a percentage *of* silently
    computes a percentage of zero.
    """

    amount = MoneyField(required=False)
    rate = RateField(required=False)

    class Meta:
        model = PayrollComponent
        fields = (
            "id", "code", "name", "component_type", "calculation_type",
            "amount", "rate", "formula_expression", "currency", "is_taxable",
            "is_subject_to_social_insurance", "affects_net", "sequence",
            "expense_account", "liability_account", "is_active",
            "created_at", "updated_at",
        )


class EmployeePayrollProfileSerializer(TenantScopedSerializer):
    """Per-employee payroll settings: exemptions, insurable wage, dependants."""

    tax_exemption_amount = MoneyField(required=False)
    insurable_wage = MoneyField(required=False)
    employee_name = serializers.CharField(source="employee.full_name", read_only=True)

    class Meta:
        model = EmployeePayrollProfile
        fields = (
            "id", "employee", "employee_name", "pay_frequency",
            "is_exempt_from_tax", "is_exempt_from_social_insurance",
            "tax_exemption_amount", "dependants_count", "insurable_wage",
            "payment_method", "currency", "is_active",
            "created_at", "updated_at",
        )


class TaxBracketSerializer(TenantScopedSerializer):
    """One band of a progressive income-tax scale.

    ``effective_from``/``effective_to`` are why brackets are rows rather than
    settings: a payslip recalculated next year must use the scale that was in
    force on its own pay date, not today's.
    """

    lower_bound = MoneyField()
    upper_bound = MoneyField(required=False, allow_null=True)
    rate = RateField()
    fixed_deduction = MoneyField(required=False)

    class Meta:
        model = TaxBracket
        fields = (
            "id", "country", "effective_from", "effective_to", "lower_bound",
            "upper_bound", "rate", "fixed_deduction", "currency",
            "is_annual_basis", "sequence", "created_at", "updated_at",
        )


class PayslipLineSerializer(ReadOnlyModelSerializer):
    """One component's contribution to a payslip. Immutable.

    ``component_snapshot`` freezes the component's definition at calculation
    time: renaming "Transport allowance" next year must not rewrite what last
    year's payslip says the employee was paid for.
    """

    quantity = QuantityField(read_only=True)
    rate = RateField(read_only=True)
    amount = MoneyField(read_only=True)
    component_code = serializers.CharField(source="component.code", read_only=True)
    component_name = serializers.CharField(source="component.name", read_only=True)

    class Meta:
        model = PayslipLine
        fields = (
            "id", "payslip", "component", "component_code", "component_name",
            "component_snapshot", "sequence", "quantity", "rate", "amount",
            "is_taxable", "calculation_note",
        )


class PayslipSerializer(ReadOnlyModelSerializer):
    """An employee's pay for one period, with its lines.

    Read-only end to end. A payslip is an ``ImmutableFinancialModel``: it is
    the statement given to an employee and, in most jurisdictions, filed with
    the tax authority. Correcting one means recalculating the run (legal until
    approval) or issuing an off-cycle adjustment run — never editing the row,
    because the number the employee was shown must stay recoverable.
    """

    gross_amount = MoneyField(read_only=True)
    taxable_amount = MoneyField(read_only=True)
    income_tax_amount = MoneyField(read_only=True)
    social_insurance_employee = MoneyField(read_only=True)
    social_insurance_employer = MoneyField(read_only=True)
    other_deductions = MoneyField(read_only=True)
    net_amount = MoneyField(read_only=True)
    total_deductions = MoneyField(read_only=True)
    employer_cost = MoneyField(read_only=True)
    working_days = QuantityField(read_only=True)
    paid_days = QuantityField(read_only=True)
    leave_days_unpaid = QuantityField(read_only=True)
    overtime_hours = QuantityField(read_only=True)

    employee_name = serializers.CharField(source="employee.full_name", read_only=True)
    employee_code = serializers.CharField(
        source="employee.employee_code", read_only=True
    )
    run_name = serializers.CharField(source="run.name", read_only=True)
    period_start = serializers.DateField(source="run.period_start", read_only=True)
    period_end = serializers.DateField(source="run.period_end", read_only=True)
    lines = PayslipLineSerializer(many=True, read_only=True)

    class Meta:
        model = Payslip
        fields = (
            "id", "run", "run_name", "period_start", "period_end", "employee",
            "employee_code", "employee_name", "employee_snapshot",
            "working_days", "paid_days", "leave_days_unpaid", "overtime_hours",
            "gross_amount", "taxable_amount", "income_tax_amount",
            "social_insurance_employee", "social_insurance_employer",
            "other_deductions", "net_amount", "total_deductions",
            "employer_cost", "currency", "payment_status",
            "payment_reference", "paid_at", "pdf_key", "lines", "created_at",
        )


class PayrollRunSerializer(TenantScopedSerializer):
    """A pay period for a set of employees.

    Every control total is read-only and every status change is a POST
    sub-resource. In particular ``approve`` runs the service, which enforces
    segregation of duties: the person who calculated a run may not be the
    person who approves it. That check lives in the service and not here on
    purpose — the same rule then applies to a run approved from a management
    command or a Celery task.
    """

    total_gross = MoneyField(read_only=True)
    total_deductions = MoneyField(read_only=True)
    total_employer_cost = MoneyField(read_only=True)
    total_net = MoneyField(read_only=True)
    department_name = serializers.CharField(source="department.name", read_only=True)
    payslip_count = serializers.IntegerField(source="payslips.count", read_only=True)

    server_owned_fields = (
        "status", "employee_count", "total_gross", "total_deductions",
        "total_employer_cost", "total_net", "journal_entry", "calculated_at",
        "calculated_by", "approved_by", "approved_at", "posted_at", "paid_at",
        "locked",
    )

    class Meta:
        model = PayrollRun
        fields = (
            "id", "name", "period_start", "period_end", "pay_date",
            "frequency", "status", "department", "department_name",
            "employee_count", "payslip_count", "total_gross",
            "total_deductions", "total_employer_cost", "total_net", "currency",
            "journal_entry", "calculated_at", "calculated_by", "approved_by",
            "approved_at", "posted_at", "paid_at", "locked", "notes",
            "created_at", "updated_at", "created_by",
        )


class MarkPaidSerializer(serializers.Serializer):
    """Body for ``POST /payroll-runs/{id}/mark-paid``.

    ``payment_date`` defaults to today in the service. It is settable because
    a bank file sent on Friday may settle on Monday, and the cash entry must
    carry the date the money actually left.
    """

    payment_date = serializers.DateField(required=False, allow_null=True)
    bank_account_system_key = serializers.CharField(
        max_length=50, required=False, allow_blank=True
    )


class SalaryStructureLineSerializer(TenantScopedSerializer):
    """One component inside a structure, and how it is sized."""

    component_code = serializers.CharField(source="component.code", read_only=True)
    component_name = serializers.CharField(source="component.name", read_only=True)

    class Meta:
        model = SalaryStructureLine
        fields = (
            "id", "structure", "component", "component_code", "component_name",
            "amount", "percentage_of_base", "sequence",
        )

    def validate(self, attrs):
        instance = getattr(self, "instance", None)
        amount = attrs.get("amount", getattr(instance, "amount", ZERO)) or ZERO
        pct = attrs.get(
            "percentage_of_base", getattr(instance, "percentage_of_base", ZERO)
        ) or ZERO
        # ck_payroll_structure_line_one_sizing says the same thing in SQL. The
        # duplicate here exists to answer with a field-level 400 naming both
        # inputs, rather than a 500 from a constraint the caller cannot see.
        if amount > ZERO and pct > ZERO:
            raise serializers.ValidationError(
                "A line is sized either by a flat amount or by a percentage of "
                "base, not both — there is no rule for which would win."
            )
        if amount == ZERO and pct == ZERO:
            raise serializers.ValidationError(
                "A line must carry either an amount or a percentage of base; "
                "one worth nothing is a data-entry slip, not an intention."
            )
        return attrs


class SalaryStructureSerializer(TenantScopedSerializer):
    """A reusable package of components. Nested lines, read-only here.

    Lines are written through their own endpoint rather than nested-writable:
    a structure is shared by every employee assigned to it, so editing one is
    a change to many people's pay. Making that a side effect of PATCHing the
    parent hides it.
    """

    lines = SalaryStructureLineSerializer(many=True, read_only=True)
    assignment_count = serializers.SerializerMethodField()

    class Meta:
        model = SalaryStructure
        fields = (
            "id", "code", "name", "description", "currency", "is_active",
            "lines", "assignment_count", "created_at", "updated_at",
        )

    def get_assignment_count(self, obj) -> int:
        """How many people this package is currently on — the number that
        says how consequential an edit here is."""
        return obj.assignments.count()


class SalaryStructureAssignmentSerializer(TenantScopedSerializer):
    """An employee on a structure, from a date, at a base salary."""

    employee_name = serializers.CharField(source="employee.full_name", read_only=True)
    structure_code = serializers.CharField(source="structure.code", read_only=True)

    class Meta:
        model = SalaryStructureAssignment
        fields = (
            "id", "employee", "employee_name", "structure", "structure_code",
            "from_date", "to_date", "base_salary", "currency", "notes",
            "created_at", "updated_at",
        )

    def validate(self, attrs):
        instance = getattr(self, "instance", None)
        start = attrs.get("from_date", getattr(instance, "from_date", None))
        end = attrs.get("to_date", getattr(instance, "to_date", None))
        if start and end and end < start:
            raise serializers.ValidationError(
                {"to_date": "An assignment cannot end before it starts."}
            )
        return attrs
